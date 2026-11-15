"""
SignPDF server — server-side PDF text removal (Sejda-style).

Unlike a white cover drawn on top of the page, this service REALLY removes
the text from the PDF: it redacts the matched words out of the page content
stream (PyMuPDF add_redact_annot + apply_redactions), so the page background
(colours, shapes, images) stays exactly as it was. The replacement text is
then drawn at the requested position.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000

Endpoints:
    GET  /health     -> {"ok": true}
    POST /edit-text  -> multipart: file (PDF) + spec (JSON string, see EditSpec)
                         returns the edited PDF bytes (application/pdf).
"""

import io
import json

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="SignPDF Server — PDF text removal")


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

    # 2. REALLY remove it: redaction deletes the glyphs from the content
    #    stream. Defaults leave images and vector graphics untouched, so a
    #    coloured background (e.g. a navy block) survives the edit.
    for rect in rects:
        page.add_redact_annot(rect)
    page.apply_redactions()

    # 3. Write the replacement text at the requested position.
    if data.replacement:
        color = tuple(data.color) if len(data.color) == 3 else (0, 0, 0)
        fontname = "hebo" if data.bold else "helv"  # Helvetica / Helvetica-Bold
        page.insert_text(
            (data.x, data.y),
            data.replacement,
            fontsize=data.fontsize,
            fontname=fontname,
            color=color,
        )

    out = io.BytesIO()
    doc.save(out, garbage=3, deflate=True)
    doc.close()
    return Response(
        content=out.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="edited.pdf"'},
    )
