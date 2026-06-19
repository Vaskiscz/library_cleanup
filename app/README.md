# Photo Cleanup — Mac app

A thin Mac app shell around the existing on-device `photo_cleanup` backend.
The backend is **reused as a library** (built from the repo root), not forked —
`photo_cleanup/` stays the single source of truth for all curation logic.

> Status: **packaging spike**. Proves `briefcase` can bundle the backend
> (osxphotos + Apple Vision + numpy + Pillow + photoscript) into a launchable,
> ad-hoc-signed `.app` and that every module imports under the embedded
> interpreter at runtime. UI is a placeholder WebView reporting bundle health.

## Build & run

```sh
cd app
uvx briefcase create macOS    # bundle the backend + deps into build/
uvx briefcase build  macOS    # compile + ad-hoc sign
uvx briefcase run    macOS    # launch
```

## Packaging notes (learned from the spike)

- **No scipy.** The backend's only scipy use (Laplacian sharpness) is now a
  pure-numpy computation, so scipy is dropped entirely — it had no installable
  wheel in briefcase's bundle context and bloated the app.
- **Vendored `bitmath` wheel** (`wheels/`). osxphotos pins `bitmath<2.0.0`,
  which is sdist-only on PyPI; briefcase installs wheels-only, so we ship a
  locally-built wheel and reference it from `pyproject.toml`.
- All processing stays **on device** — no uploads, consistent with the project's
  hard constraint.
