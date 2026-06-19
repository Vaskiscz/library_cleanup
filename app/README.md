# Library Cleanup — Mac app

A Mac app around the existing on-device `photo_cleanup` backend.
The backend is **reused as a library** (built from the repo root), not forked —
`photo_cleanup/` stays the single source of truth for all curation logic.

## Architecture

```
Toga WebView app  ──>  local FastAPI service (localhost only)  ──>  photo_cleanup BE
       (app.py)              (server.py / engine.py)                (osxphotos + Vision)
                                     │
                                     └── app-owned review state in SQLite (store.py)
```

- **`store.py`** — app-owned review state in SQLite (decisions + a `reviewed`
  exclusion set). The app does *not* write `cleanup:*` keywords back to Photos
  (the CLI's mechanism); it keeps its own state and additionally honours the
  library's `reviewed:keep` so anything the CLI locked is never re-shown.
- **`engine.py`** — orchestration over the BE (mirrors `cli dedup`): scoped
  record loading, candidate grouping, JSON serialisation, thumbnails. No
  curation logic lives here.
- **`server.py`** — FastAPI service bound to `127.0.0.1`. Endpoints:
  `GET /api/health`, `POST /api/scan`, `GET /api/candidates?layer=`,
  `GET /api/thumb/{uuid}`, `POST /api/decisions`, `POST /api/finalize`,
  `POST /api/learn`.
- **`learning.py`** — turns the app's explicit keep/discard decisions into an
  "explicit-labels" feedback file the existing learning pipeline already reads.

> Status: **service layer complete (Phase 2)**. The review UI (Phase 3) and
> in-app PhotoKit delete (Phase 4) are next. `finalize` records reviewed-state +
> learning and returns the discard uuids; it does not delete yet.

## Build & run (bundled app)

```sh
cd app
uvx briefcase create macOS    # bundle the backend + service + deps
uvx briefcase build  macOS    # compile + ad-hoc sign
uvx briefcase run    macOS    # launch (WebView -> localhost service)
```

## Develop / test

The service is fully testable without packaging. From the repo root:

```sh
uv run pytest -q               # backend + app tests (one suite)
```

## Packaging notes (from the spike)

- **No scipy.** The backend's only scipy use (Laplacian sharpness) is pure numpy
  now — scipy had no installable wheel in briefcase's bundle context.
- **Vendored `bitmath` wheel** (`wheels/`): osxphotos pins `bitmath<2.0.0`, which
  is sdist-only on PyPI; briefcase installs wheels-only.
- All processing stays **on device** — the service binds to localhost; no uploads.
