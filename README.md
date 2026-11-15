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

Notes:
- Redaction removes only the text. Images and vector graphics (including a
  coloured background block) are left untouched.
- Replacement text uses the standard Helvetica family (the PDF's original
  embedded font cannot be reused for new text). Size, weight and colour come
  from the `spec`, so match them to the original text.
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

`ebaypro-client-service-agreement.pdf`, page 0: removed the word `DRAFT`
(at 63, 80.3, size 12, bold) and inserted `FINAL`. Verified with PyMuPDF:
old word gone, replacement present, neighbouring text untouched, page count
unchanged, file opens normally.
