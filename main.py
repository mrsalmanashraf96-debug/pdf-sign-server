"""
SignPDF server — server-side PDF text removal (Sejda-style).

Unlike a white cover drawn on top of the page, this service REALLY removes
the text from the PDF: it redacts the matched words out of the page content
stream (PyMuPDF add_redact_annot + apply_redactions), so the page background
(colours, shapes, images) stays exactly as it was. The replacement text is
then drawn at the requested position.

ORIGINAL-FONT REUSE (the point of this service): the replacement is drawn
with the SAME font the removed text used, not Helvetica. Before redacting,
the server finds the text's span (page.get_text("dict")), extracts the
embedded font program (doc.extract_font) and re-embeds it for the
replacement. Two honest limits:

* Base-14 fonts (Helvetica, Times, Courier...) are not embedded — they are
  reused by name, which is the same font.
* Embedded fonts in a PDF are usually SUBSETS: they only contain the glyphs
  the document already uses. If the replacement needs a letter the subset
  does not have, the server falls back to Helvetica for the whole
  replacement (a half-original / half-Helvetica word would look worse) and
  says so in the response headers.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000

Endpoints:
    GET  /health     -> {"ok": true}
    POST /edit-text  -> multipart: file (PDF) + spec (JSON string, see EditSpec)
                         returns the edited PDF bytes (application/pdf).
                         Response headers report the font actually used:
                           X-Edit-Font:        original | fallback-helvetica | none
                           X-Edit-Font-Name:   font used for the replacement
                           X-Edit-Font-Original: font of the removed text
                           X-Edit-Font-Reason:  why (ok, base14, missing-glyphs:..., ...)
"""

import io
import json
import re

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="SignPDF Server — PDF text removal")

# Map of the PDF base-14 font names to the names PyMuPDF's insert_text
# understands. Base-14 fonts are never embedded, so "reuse" = use by name.
BASE14 = {
    "helvetica": "helv",
    "helvetica-bold": "hebo",
    "helvetica-oblique": "heit",
    "helvetica-boldoblique": "hebi",
    "times-roman": "tiro",
    "times-bold": "tibo",
    "times-italic": "tiit",
    "times-bolditalic": "tibi",
    "courier": "cour",
    "courier-bold": "cobo",
    "courier-oblique": "coit",
    "courier-boldoblique": "cobi",
    "symbol": "symb",
    "zapfdingbats": "zadb",
}

# Name PyMuPDF registers a re-embedded original font under.
ORIG_FONTNAME = "SignPDFOrig"


class EditSpec(BaseModel):
    """One text-removal + replacement on one page.

    Coordinates use PDF points with a TOP-LEFT origin (the same system
    pdf.js uses in the browser, so the app can send page fractions
    multiplied by the page width/height). x, y is the BASELINE start of
    the replacement text. fontsize is in points. color is [r, g, b]
    with each value 0-1.
    """

    page: int = Field(0, ge=0, description="0-based page number")
    text: str = Field(..., min_length=1, description="Exact text to remove")
    replacement: str = Field("", description="Text to write in its place")
    x: float = Field(..., description="Baseline x (points, from left)")
    y: float = Field(..., description="Baseline y (points, from top)")
    fontsize: float = Field(12, gt=0, description="Replacement font size")
    color: list[float] = Field(
        default_factory=lambda: [0, 0, 0],
        description="RGB, each 0-1, e.g. [0, 0, 0] black",
    )
    bold: bool = False


def _strip_subset(name: str) -> str:
    """Remove a subset prefix like 'AAAAAA+' from a font name."""
    return re.sub(r"^[A-Z]{6}\+", "", name or "")


def _norm(name: str) -> str:
    """Normalise a font name for comparison (no subset prefix/spaces/dashes)."""
    return _strip_subset(name).replace(" ", "").replace("-", "").lower()


def find_target_span(page: "pymupdf.Page", text: str):
    """Find the span that holds `text` on the page (before any redaction).

    Pass 1: a span whose text contains the requested text.
    Pass 2: a line whose concatenated span texts contain it (text split
    across spans) — returns the first span of that line.
    Returns the span dict, or None.
    """
    text_dict = page.get_text("dict")
    for block in text_dict["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if text in span["text"]:
                    return span
    for block in text_dict["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            joined = "".join(s["text"] for s in line["spans"])
            if text in joined and line["spans"]:
                return line["spans"][0]
    return None


def resolve_original_font(doc, page, span, replacement):
    """Work out how to draw `replacement` in the removed text's own font.

    Returns (kind, fontname, fontbuffer, display_name, reason) where kind is
    "original" (draw with the original font) or "fallback" (draw Helvetica).
    fontname is what to pass to insert_text; fontbuffer (bytes) must be
    registered first with page.insert_font when not None.
    """
    span_font = span["font"]
    display = _strip_subset(span_font)

    # Type3 / bitmap fonts cannot be extracted or reused.
    if span_font.startswith("Type3") or not display:
        return ("fallback", None, None, span_font, "font-not-extractable")

    # Case (a): a base-14 font — reuse by name, nothing to extract.
    base14 = BASE14.get(_norm(span_font))
    if base14:
        return ("original", base14, None, display, "base14")

    # Case (b): embedded font — find its xref on this page.
    xref = None
    for f in page.get_fonts(full=True):
        # f = (xref, ext, type, basefont, name, encoding, referencer)
        if _norm(f[3]) == _norm(span_font):
            xref = f[0]
            break
    if not xref:
        return ("fallback", None, None, display, "font-not-found-on-page")

    try:
        _name, ext, _ftype, buf = doc.extract_font(xref)
    except Exception as exc:  # noqa: BLE001
        return ("fallback", None, None, display, f"extract-failed: {exc}")
    if not buf or ext in ("n/a", ""):
        return ("fallback", None, None, display, "font-not-extractable")

    # The extracted font is a SUBSET: it only has the glyphs the document
    # already uses with this font. Check every (non-space) character of the
    # replacement against the subset's real glyph table — has_glyph is the
    # ground truth, not a guess from the visible text.
    try:
        probe = pymupdf.Font(fontbuffer=buf)
    except Exception as exc:  # noqa: BLE001
        return ("fallback", None, None, display, f"font-unusable: {exc}")
    missing = sorted({c for c in replacement if c != " " and not probe.has_glyph(ord(c))})
    if missing:
        return (
            "fallback",
            None,
            None,
            display,
            "missing-glyphs:" + ",".join(missing),
        )

    return ("original", ORIG_FONTNAME, buf, display, "ok")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/edit-text")
async def edit_text(
    file: UploadFile = File(..., description="PDF file to edit"),
    spec: str = Form(..., description="EditSpec as a JSON string"),
):
    try:
        data = EditSpec.model_validate_json(spec)
    except Exception as exc:  # noqa: BLE001 - report as a clean 400
        raise HTTPException(status_code=400, detail=f"Invalid spec JSON: {exc}")

    pdf_bytes = await file.read()
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not open PDF: {exc}")

    if data.page >= len(doc):
        doc.close()
        raise HTTPException(
            status_code=400,
            detail=f"page {data.page} out of range (PDF has {len(doc)} pages)",
        )

    page = doc[data.page]

    # 1. Find every occurrence of the text on the page.
    rects = page.search_for(data.text)
    if not rects:
        doc.close()
        raise HTTPException(
            status_code=404,
            detail=f"Text not found on page {data.page}: {data.text!r}",
        )

    # 2. BEFORE redacting, work out the removed text's font. The span and
    #    the embedded font program must be captured now — after redaction
    #    the text (and possibly its font) is gone from the page.
    font_mode = "none"
    font_used = ""
    font_original = ""
    font_reason = "no-replacement"
    fontname = None
    fontbuffer = None
    if data.replacement:
        span = find_target_span(page, data.text)
        if span is None:
            font_mode, font_reason = "fallback-helvetica", "no-span-match"
            fontname = "hebo" if data.bold else "helv"
            font_used = "Helvetica-Bold" if data.bold else "Helvetica"
        else:
            font_original = _strip_subset(span["font"])
            kind, fname, fbuf, display, reason = resolve_original_font(
                doc, page, span, data.replacement
            )
            font_reason = reason
            if kind == "original":
                font_mode = "original"
                fontname, fontbuffer = fname, fbuf
                font_used = display
            else:
                font_mode = "fallback-helvetica"
                fontname = "hebo" if data.bold else "helv"
                font_used = "Helvetica-Bold" if data.bold else "Helvetica"

    # 3. REALLY remove it: redaction deletes the glyphs from the content
    #    stream. Defaults leave images and vector graphics untouched, so a
    #    coloured background (e.g. a navy block) survives the edit.
    for rect in rects:
        page.add_redact_annot(rect)
    page.apply_redactions()

    # 4. Write the replacement text at the requested position, in the
    #    original font when possible, else Helvetica.
    if data.replacement:
        color = tuple(data.color) if len(data.color) == 3 else (0, 0, 0)
        try:
            if fontbuffer is not None:
                page.insert_font(fontname=fontname, fontbuffer=fontbuffer)
            page.insert_text(
                (data.x, data.y),
                data.replacement,
                fontsize=data.fontsize,
                fontname=fontname,
                color=color,
            )
        except Exception:
            # If the original font fails at insert time (e.g. an exotic
            # embedded format), fall back to Helvetica rather than failing
            # the whole edit — the old text is already gone.
            font_mode = "fallback-helvetica"
            font_used = "Helvetica-Bold" if data.bold else "Helvetica"
            font_reason = "insert-failed"
            page.insert_text(
                (data.x, data.y),
                data.replacement,
                fontsize=data.fontsize,
                fontname="hebo" if data.bold else "helv",
                color=color,
            )

    out = io.BytesIO()
    doc.save(out, garbage=3, deflate=True)
    doc.close()
    return Response(
        content=out.getvalue(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="edited.pdf"',
            "X-Edit-Font": font_mode,
            "X-Edit-Font-Name": font_used,
            "X-Edit-Font-Original": font_original,
            "X-Edit-Font-Reason": font_reason,
        },
    )
