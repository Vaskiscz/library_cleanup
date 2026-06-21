# Library Cleanup — design component library

A syncable component library for **claude.ai/design**, kept in sync with the
app via `DesignSync` / the `/design-sync` skill (one component at a time).

## Contract
- **`tokens.css`** is the single source of truth for color, type, spacing,
  radius, motion (light + dark). The production app loads the *same* file
  (`app/src/photocleanup/web/static/tokens.css`), so a token change in the
  design system flows straight into the app.
- **`app.css`** holds the component styles, shared with production.
- Each `*.html` is a self-contained **`@dsCard`** preview (first line
  `<!-- @dsCard group="…" -->`) that links `tokens.css` + `app.css` and renders
  one component in its states. These become the cards in the Design System pane.

## Cards
- `foundations.html` — colors, type scale, radius (Foundations)
- `chrome-status-scanning.html` — top bar + status dot states, progress strip, scanning
- `photo-cards.html` — keeper / remove / favourite / video review cards
- `category-rows-and-buttons.html` — results category rows + button styles
- `finalize-modal.html` — finalize confirm + done

## Round-trip
design (claude.ai/design) ⇄ `DesignSync` ⇄ this folder → I regenerate the
production WebView (`app/src/photocleanup/web/`) from the synced components +
tokens. Edit tokens/components here; the app consumes them.
