# Library Cleanup — design prototype

- **`prototype-standalone.html`** — a single self-contained clickable prototype
  of the whole app (all screens + the updater and Ko-fi flows). It inlines its
  own CSS/JS, so it opens in any browser with no dependencies. Kept in sync with
  the production WebView after each app build.

The production UI lives in `app/src/photocleanup/web/` and is the source of
truth for shipped styles (`static/tokens.css`, `static/app.css`, `static/app.js`).
