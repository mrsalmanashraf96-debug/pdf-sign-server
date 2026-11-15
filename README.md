# SignPDF Server — real PDF text removal (Sejda-style)

A tiny Python service (FastAPI + PyMuPDF) that **really removes text from a
PDF** — it does not paint a white box over it. The matched words are deleted
from the page content stream with redaction, so the page background (colours,
shapes, images) stays exactly as it was, and the replacement text is drawn
in their place. This is the server half of the SignPDF app's Edit PDF Text:
the app sends the PDF + edit, the server returns the edited PDF.

## Endpoints

### `GET /health`
Returns `{"ok": true}`. Used by the host as a health check.

### `POST /edit-text`
Multipart form, two parts:

| Part   | Type   | Description                              |
|--------|--------|------------------------------------------|
| `file` | file   | The PDF to edit                          |
| `spec` | string | JSON object (see below)                  |

`spec` JSON:

```json
{
  "page": 0,
  "text": "DRAFT",
  "replacement": "FINAL",
  "x": 63.0,
  "y": 80.3,
  "fontsize": 12,
  "color": [0, 0, 0],
  "bold": true
}
```

| Field         | Meaning                                                                 |
|---------------|-------------------------------------------------------------------------|
| `page`        | 0-based page number                                                     |
| `text`        | Exact text to remove (every occurrence on that page is removed)         |
| `replacement` | Text to write in its place (empty = just delete)                        |
| `x`, `y`      | Baseline position of the replacement, in PDF points, top-left origin — the app sends page-fraction × page size |
| `fontsize`    | Replacement size in points                                             |
| `color`       | `[r, g, b]`, each 0–1 (e.g. `[1, 1, 1]` white on a dark background)     |
| `bold`        | `true` = Helvetica-Bold, `false` = Helvetica                           |

Response: the edited PDF (`application/pdf`). Errors: `400` bad spec/page,
`404` text not found on that page.

The response also carries headers saying which font the replacement was
drawn in:

| Header | Values |
|--------|--------|
| `X-Edit-Font` | `original` (removed text's own font) · `fallback-helvetica` · `none` (no replacement given) |
| `X-Edit-Font-Name` | Font actually used, e.g. `LiberationSans-Bold` or `Helvetica` |
| `X-Edit-Font-Original` | Font of the removed text |
| `X-Edit-Font-Reason` | `ok` · `base14` · `missing-glyphs:Q,U,Z` · `font-not-extractable` · `no-span-match` · `insert-failed` |

Notes:
- Redaction removes only the text. Images and vector graphics (including a
  coloured background block) are left untouched.
- **Original-font reuse:** the replacement is drawn in the SAME font the
  removed text used. The server finds the text's span, extracts the
  embedded font program and re-embeds it for the replacement. Base-14
  fonts (Helvetica, Times, Courier…) are reused by name — that is the
  same font. The spec's `bold` flag only matters for the Helvetica
  fallback; an original font already carries its own weight.
- **Fallback rule (honest limit):** embedded fonts in a PDF are usually
  SUBSETS — they only contain the glyphs the document already uses. The
  server checks every letter of the replacement against the subset's real
  glyph table. If any letter is missing, the WHOLE replacement falls back
  to Helvetica (a half-original / half-Helvetica word would look worse)
  and the headers say so (`X-Edit-Font: fallback-helvetica`,
  `X-Edit-Font-Reason: missing-glyphs:…`). So a replacement works in the
  original font exactly when its letters already appear somewhere in the
  document in that font.
- The app must call this over the internet — the edit needs the server. The
  rest of the app (sign, scan, merge, etc.) still works fully offline.

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
# test: curl http://127.0.0.1:8000/health
```

## Deploy (free) — Render

No server of ours is running yet. Deploy steps for the user:

1. **GitHub account** (free, github.com) — create a repo called
   `pdf-sign-server`, upload these files (`main.py`, `requirements.txt`,
   `Dockerfile`, `render.yaml`, `README.md`).
2. **Render account** (free, render.com) — sign up with GitHub.
3. Render dashboard → **New +** → **Web Service** → connect the
   `pdf-sign-server` repo.
4. Render detects the `Dockerfile` (or the `render.yaml` Blueprint) —
   leave the settings as they are: Environment **Docker**, Plan **Free**,
   Health Check Path **/health**. Click **Create Web Service**.
5. Wait for the build (a few minutes). Render gives a URL like
   `https://pdf-sign-server.onrender.com`.
6. Check it: open `https://pdf-sign-server.onrender.com/health` — it must
   show `{"ok": true}`.
7. Put that URL into the SignPDF app as the edit-server address.

Free-tier notes: the service sleeps after ~15 minutes with no traffic, so
the first edit after a pause takes ~50 seconds to wake up. There is no
monthly fee and no card is needed for the free plan. If Render ever stops
the free tier, the same Docker image runs unchanged on Railway or Fly.io.

## Local test (done 15 Nov 2026)

`ebaypro-client-service-agreement.pdf`, page 0 (word `DRAFT`, font
LiberationSans-Bold, at 63, 80.3, size 11). All three verified with PyMuPDF
(span font in `get_text("dict")` + response headers + rendered pixels):

| Test | Replacement | Result |
|------|-------------|--------|
| Same letters | `DRAFT` → `DART` | **Original font** — span font `LiberationSans-Bold`, header `X-Edit-Font: original` |
| New letters | `DRAFT` → `QUIZ` | **Fallback Helvetica** — span font `Helvetica`, header `fallback-helvetica`, reason `missing-glyphs:Q,U,Z` |
| Regression | `DRAFT` → `FINAL` | **Original font** — span font `LiberationSans-Bold` (F, I, N, A, L all exist in the subset) |

Every case: old word gone, replacement present and visibly rendered,
neighbouring text untouched, page count unchanged (5/5), background
graphics untouched (32/32 vector drawings on page 0).
