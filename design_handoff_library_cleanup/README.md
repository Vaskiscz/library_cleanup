# Handoff: Library Cleanup — on-device photo library curation (macOS)

## Overview
**Library Cleanup** is an on-device macOS app that helps declutter Apple Photos: it
finds near-duplicate photoshoots, repeated videos, and work screenshots, **pre-picks
the best of each group**, and lets the user correct the suggestions and confirm
removal. Nothing is uploaded — all processing is local; the UI runs in a WKWebView
served from `127.0.0.1`.

This bundle covers the v1 flow:
**Home (analyze + choose categories) → Scanning → Review grid (per-category sections) → Finalize (confirm → progress → done)**,
plus the brand/app-icon spec.

## About the Design Files
The files in this bundle are **design references created in HTML** — interactive
prototypes that show the intended look, copy, and behavior. They are **not production
code to copy directly**. Each `*.dc.html` is a self-contained “Design Component”: open
it in a browser to explore (it loads the runtime `support.js`; the logic lives in a
`<script type="text/x-dc">` block at the bottom of each file, the markup is the
`<x-dc>` body).

Your task is to **recreate these designs in the target codebase’s environment** using
its established patterns. The real product renders HTML/CSS/JS inside a macOS WKWebView
served by a local FastAPI server, so a vanilla or lightweight-framework front-end that
talks only to `127.0.0.1` is the natural target. Keep the **hard constraints** below.

### Hard constraints (from the product brief — non-negotiable)
- **No external network.** No web fonts, CDNs, remote images, or analytics. Use the
  **system font stack**: `-apple-system, BlinkMacSystemFont, "SF Pro", system-ui, sans-serif`.
- **Icons are inline SVG.** Any raster must be a `data:` URI.
- **Light *and* dark.** Use `color-scheme: light dark` + CSS custom properties (the
  token sheet provides both). The prototypes are shown in **light mode only** so far —
  dark values exist in `photocleanup-tokens.css` and must be wired up.
- **Thumbnails** come from the API as ~240px JPEGs at `/api/thumb/{uuid}`; treat as
  roughly square, `object-fit: cover`. **In the prototypes, thumbnails are CSS-gradient
  placeholders** standing in for real photos — replace with real `<img>` thumbs.
- **Desktop, resizable.** Optimize ~900–1400px; degrade to ~700px. Trackpad + keyboard,
  not touch.
- **Density + volume.** A session can render hundreds of items — design for dense grids
  and lazy image loading.

## Fidelity
**High-fidelity.** Final colors, typography, spacing, copy, and interactions are
intended as shown. Recreate pixel-accurately using the codebase’s libraries, pulling
exact values from `photocleanup-tokens.css`. (The only deliberately non-final element is
the photo/video imagery — gradient placeholders stand in for API thumbnails.)

## Real API shape (drives the data model)
Endpoints already exist: `health`, `scan`, `candidates?layer=dedup|screenshots`,
`thumb/{uuid}`, `decisions`, `finalize`, `learn`. A `GET /api/candidates?layer=dedup`
returns `{ layer, since, until, groups: [{ group_key, size, suggested_keep,
suggested_discard, photos: [{ uuid, filename, width, height, favorite, suggested_keep,
score, focus, timestamp, subtitle, thumb, decided }] }] }`. For `layer=screenshots`,
photos are flat (one pseudo-group), all `suggested_keep:false`, and `subtitle` carries
the reason + OCR snippet, e.g. `work-app:slack · "stand-up notes…"`.

> Terminology note: the brief used “clear”; **the product copy now says “remove”**
> (keep = green, remove = red). Keep that wording.

---

## Screens / Views

### 1. Home — start a review  (`Home.dc.html`)
A single centered macOS window (max-width 860px) with three phases driven by one state
machine (`phase: 'idle' | 'scanning' | 'results'`).

**Window chrome (all phases):** traffic-light bar (38px, `#f0f0f2`, dots
`#ff5f57/#febc2e/#28c840`, centered title “Library Cleanup”), then a 50px app top bar:
left = bold “Library Cleanup” (14px/600); right = status line
`Library connected · 24,118 photos · 1,204 videos · 318 GB` (12px `#6e6e73`) preceded by
a 7px green dot `#2e7d32`.

**Idle:** centered column — the full-color app icon at 92px (see Brand spec), title
**“Tidy your photo library”** (25px/600, `-0.02em`), subtitle (15px `#6e6e73`, max 440px):
“We scan on your Mac, pre-pick the best of every burst and flag clutter. You just review
the suggestions and confirm.”, primary button **“Analyze my library”** (15px/600 white on
`#1f9e86`, padding 13×30, radius 11, shadow `0 2px 8px rgba(31,158,134,.32)`), small line
“1,331 kept · 784 removed in past reviews” (12px `#a0a0a5`). Footer strip (top-border,
`#fbfbfd`): a small lock glyph + “Everything runs on your Mac. Nothing is uploaded, ever.”

**Scanning (screen B):** centered — a 56px spinner (4px ring, `#1f9e8622` track,
`#1f9e86` top, `spin .8s linear infinite`), title **“Analyzing your library…”** (21px/600),
a rotating step line (14px `#6e6e73`) cycling: “Reading your library…”, “Computing
on-device similarity…”, “Grouping photoshoots…”, “Scanning for screenshots…”,
“Almost done…”, a 340px determinate progress bar (7px, track `#ececef`, fill gradient
`90deg,#15a89a,#2f9e5f`), reassurance line, and an underlined text **Cancel** → returns to
idle. (Prototype auto-advances each step ~850ms; real app drives this from `scan` progress
and must be **cancellable**.)

**Results:** title **“Here’s what we found”** + subtitle, then a vertical list of
**multi-select category cards** (default all selectable ones checked):
| Category | Count | Sub | Save | Subtitle copy |
|---|---|---|---|---|
| Duplicate photoshoots | 1,015 | photos · across 341 bursts | up to 6.4 GB | “Fired the shutter 50 times at the same spot? No problem.” |
| Screenshots | 784 | screenshots · flagged to remove | up to 0.4 GB | “Work pings you screenshotted and never reopened.” |
| Duplicate videos | 212 | videos · across 58 sets | up to 9.2 GB | “Ten takes of the same wave? We keep the steady one.” |
| Expired utility photos | — (Soon, disabled) | — | — | “That parking-spot photo from a garage you left in 2022.” |

Each card: 1.5px border + tint when selected (border `#1f9e86`, bg `#1f9e860a`) vs unselected
(border `#dcdce0`, bg `#fff`); disabled = `opacity .6`, “Soon” pill. Left: a 24px rounded
checkbox (filled `#1f9e86` + white check when selected, else 2px `#cfcfd4` outline). Right:
count (15px/600), sub (11px `#a0a0a5`), and **“Save up to X GB”** (11px/600 `#2e7d32`).
Footer bar: text **Re-scan** (left), then `N categories · save up to 16.0 GB`
(`#8a8a8f`) + primary CTA **“Review N categories”** (disabled grey when 0 selected).
The CTA navigates to `Review Grid.dc.html?layers=<comma-separated ids>` (e.g.
`?layers=dedup,videos,screenshots`).

### 2. Review grid — the core screen  (`Review Grid.dc.html`)
Reads selected layers from the URL `?layers=` (defaults to all). Renders the same window
chrome, a sticky progress strip, **one section per selected layer in order
dedup → videos → screenshots**, and a sticky bottom action bar.

**Sticky progress strip** (`#fbfbfd`, bottom border): left = `N items in M categories`;
center = 5px progress bar (fill `#1f9e86`); right = `Keeping X` (`#2e7d32`/600) · `Removing Y`
(`#c62828`/600).

**Layer section header:** title (16px/600) + count summary (12px `#9a9aa0`), e.g.
“Duplicate photoshoots — 3 bursts · keeping 8 · removing 22”.

**Media layers (dedup, videos):** one rounded card (1px `#ededf0`, radius 14) per group.
Group header (`#fbfbfd`): **location title** (14px/600) + meta
`Dec 24, 2023 · 12 shots · keep 3 · remove 9` (videos say “clips”), and right-aligned
buttons **Keep all**, **Remove all**, and a collapse chevron (▾/▸). Body = a wrapping flex
grid (`gap:12px`) of square **photo cards** (default 116px; density tweak →
98/116/138px):
- Thumbnail: rounded 9px, `object-fit:cover`. **Keeper** = full color + accent ring
  `box-shadow: 0 0 0 2.5px #1f9e86, 0 3px 10px rgba(0,0,0,.14)` + a 22px round check badge
  top-right (`#1f9e86`, white ✓). **Removed** = `opacity .7`, slight grayscale, + a white
  round badge top-right with a red ✕ (`#c62828`).
- ♥ favorite badge top-left (white heart, drop-shadow) when `favorite`.
- **Video** items: centered play triangle in a dark circle + a duration pill bottom-left
  (`0:12`).
- On hover: a bottom gradient caption with `score · focus · WxH` (photos) or `duration · WxH`
  (videos). Filename shown under each card (10.5px).
- **Tap a card toggles keep ⇄ remove.** Keyboard: card is focusable, **Space/Enter** toggles,
  **arrow keys** move focus across the grid.
- Collapsed group shows a one-line summary + “expand”.

**Screenshots layer:** a single rounded container; header row “All flagged to remove — tap
any to keep” + **Keep all / Remove all**. Body = wrapping flex grid (`gap:14px`) of
**landscape screenshot cards** (156×112): a faux screenshot (white body + an app-colored
header bar with the app name + grey faux text lines), same ring/✕ selection language, and a
2-line caption below = the reason + OCR snippet (`work-app:slack · "stand-up notes…"`).
App header colors: Slack `#4a154b`, Teams `#4b53bc`, Gmail `#c5221f`, Notion `#2f2f2f`,
Zoom `#2d8cff`. All default to **remove**; tapping **rescues** (keep).

**Bottom action bar:** summary `Keeping 10 · Removing 39 · Frees 1.1 GB` + primary
**“Review & Finalize”**.

### 3. Finalize dialog (confirm → working → done)  (in `Review Grid.dc.html`)
A centered modal over a dimmed/blurred backdrop (`rgba(20,20,22,.36)`, `backdrop-filter:
blur(3px)`), card 444px, radius 16, shadow `0 28px 80px rgba(0,0,0,.36)`. **Do not gate
visibility on a CSS entry animation** (it can render at opacity 0 in some capture/runtime
paths) — appear instantly or use a fill-safe animation.
- **Confirm:** title **“Review & Finalize”**, headline `Keeping X · removing Y items ·
  frees Z GB`, three reassurance rows (green ✓): macOS confirms before removal; kept items
  marked reviewed (won’t be shown again); removed items go to Recently Deleted (recoverable
  30 days), nothing leaves the Mac. Buttons: **Go back** (secondary) · **Remove Y** (primary).
- **Working:** title “Removing…”, calm copy, determinate bar (`width = progressPct`),
  spinner + `Removed N of Y`.
- **Done:** 60px green check disc (`#2e7d32`), **“All done”**, `Kept X · removed Y · freed
  Z GB.`, note “Removed items stay in Recently Deleted for 30 days.”, single primary CTA
  **“Start a new review”** → navigates back to `Home.dc.html`.

### 4. Brand & app icon  (`Brand & Icon.dc.html`)
Spec sheet for the **“stack → keeper”** app icon: a Big-Sur squircle with a teal→green
gradient, a fanned stack of photo frames, one solid “chosen” frame, and a keep-green check
badge. Includes the 1024 master, the size set (1024→32), Dock context (light/dark),
the three concept directions explored, **checkmark-colour options** (keep-green
`#2E7D32` recommended, brand teal, deep forest, white badge), and the horizontal
**wordmark lockup** (light/dark). The full icon SVG is inline in this file — lift the
`#appicon` symbol (and its gradients) for production. Export PNGs at 16/32/64/128/256/512/1024
(@1x/@2x) for the `.icns`. *(Note: an earlier tiny monochrome top-bar mark was dropped at
the client’s request — the top bar uses the app name as text only.)*

---

## Interactions & Behavior
- **Suggestion-first principle:** picks are pre-applied (green = keep). The user only
  corrects. Toggling is instant and reversible.
- **Toggle:** click/tap a card, or focus + Space/Enter. Arrow keys move focus within a grid.
- **Group actions:** Keep all / Remove all per group; collapse/expand per group.
- **Navigation:** Home CTA → `Review Grid?layers=…`; Done CTA → `Home`.
- **Scanning:** indeterminate-but-reassuring, cancellable; wire to real `scan` progress.
- **Finalize:** confirm → progress (from `finalize`) → done; macOS shows its own delete
  confirmation; kept items are marked reviewed (call `learn` so the picker improves).
- **Transitions:** card selection `box-shadow .13s ease`; active press scales inner to .96;
  progress bar width `.12s linear`; spinner `.7–.8s linear infinite`.

## State Management
- `selectedLayers: string[]` — from Home selection / URL.
- `decisions: Record<uuid, 'keep' | 'remove'>` — seeded from each item’s `suggested_keep`.
- `collapsed: Record<groupId, boolean>`.
- `hover: uuid | null` (meta overlay); optional `alwaysMeta` flag.
- Finalize: `phase: null|'confirm'|'working'|'done'`, `progress`, `doneCount`,
  snapshot `finalKeep/finalRemove/finalGb`.
- Home: `phase: 'idle'|'scanning'|'results'`, `scanStep`, per-category `selected`.
- Derived: per-group keep/remove counts, totals, and **freed GB** = sum of `sizeMb` of
  removed items / 1024 (prototype estimates size per item; real app uses file sizes).

## Design Tokens
Use **`photocleanup-tokens.css`** verbatim — it defines `:root` light values + a
`@media (prefers-color-scheme: dark)` override block for: brand (`--pc-brand` `#1f9e86`
light / `#34c2a3` dark; gradient teal `#15a89a` → green `#2f9e5f`), semantic keep
`#2e7d32` / remove `#c62828` / warn, surfaces, text, borders, a 4px spacing scale, type
scale (system font), radii (6/10/14px), shadows, and motion easings/durations. Hard-coded
hexes in the prototypes map to these tokens.

## Assets
- **App icon:** inline SVG (`#appicon` symbol + gradients) in `Brand & Icon.dc.html`.
  Export to `.icns` PNG set for briefcase (target
  `app/src/photocleanup/resources/photocleanup.icns` — confirm with build config).
- **UI icons** (check, ✕, heart, play, lock, spinner): inline SVG in the screen files.
- **Photo/video/screenshot imagery:** placeholders only — replace with `/api/thumb/{uuid}`.

## Files
- `Home.dc.html` — screens A (Home) + B (Scanning) + results/category picker.
- `Review Grid.dc.html` — screen C (review grid, per-layer sections) + finalize dialog (E/F).
- `Brand & Icon.dc.html` — icon, size set, wordmark, checkmark-colour options.
- `photocleanup-tokens.css` — the design-token sheet (light + dark). Drop-in.
- `support.js` — the prototype runtime (lets the `.dc.html` files open in a browser; **not**
  needed in production).

To preview: open any `*.dc.html` in a browser. Start at `Home.dc.html`, click
**Analyze my library**, choose categories, and continue through to Finalize.
