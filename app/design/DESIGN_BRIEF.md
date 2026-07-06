# Library Cleanup — Design Brief (logo + app UI)

A brief for designing **(1)** the app logo / macOS icon and **(2)** the app's
review UI. Read §3 (hard constraints) first — they bound every decision.

---

## 1. What it is / who it's for

**Library Cleanup** is an on-device macOS app that helps you declutter Apple
Photos:

- **Deduplicate photoshoots** — when you fired the shutter 30 times, keep the
  best few genuinely-different frames, suggest the rest for deletion.
- **Triage work screenshots** — surface Slack/Teams/work screenshots for removal
  while keeping private chats, memes, and photos of people.

It runs **entirely on the device** — nothing is uploaded, ever. The user works
in focused "review sessions": pick a scope (e.g. a year), see candidates with
the best picks **pre-selected**, correct anything, then commit.

**Audience:** the owner (a busy professional with a large, personal library)
plus a handful of friends they share the app with. Not an enterprise tool.

## 2. Brand personality

Calm · trustworthy · precise · quietly delightful.

The app touches **personal memories**, so it must feel **safe and respectful**:
it *suggests*, it never destroys without explicit consent, and deletion always
goes through macOS's own confirmation. Avoid anything that feels aggressive,
gimmicky, or "enterprise-cold."

Anti-patterns: trash-can/explosion/delete metaphors, harsh reds as the primary
brand color, busy gradients, stock "AI" clichés.

## 3. Hard constraints (these are non-negotiable)

The UI is **self-contained HTML/CSS/JS rendered in a macOS WKWebView**, served
from a local FastAPI server on `127.0.0.1`. Therefore:

- **No external network.** No Google Fonts / CDNs / remote images / web fonts /
  analytics. This is both the product's privacy promise *and* a practical
  offline requirement. → Use the **system font stack** (`-apple-system,
  BlinkMacSystemFont, "SF Pro", system-ui, sans-serif`). Icons must be **inline
  SVG**. Any raster must be a `data:` URI.
- **Light *and* dark.** macOS users switch appearance; design **both**. Use
  `color-scheme: light dark` and CSS custom properties (design tokens).
- **Thumbnails** are served by the API as ~240px JPEGs at `/api/thumb/{uuid}`.
  Treat them as roughly square, content-cropped (`object-fit: cover`).
- **Desktop window**, resizable. Optimize for ~900–1400px wide; degrade
  gracefully to ~700px. **Not** a mobile/touch app (but support trackpad +
  keyboard).
- **Density + volume.** A session can render **hundreds** of thumbnails. Design
  for dense grids, lazy image loading, and fast scanning of many groups.

## 4. Logo / app icon

**Concept space:** "photos" ∩ "choosing the best / tidying." Communicate *keep
the gem, clear the rest* — **not** deletion.

Concept directions (pick/most-explore the first):

1. **Stack → keeper** *(recommended)*: a small stack of photo frames with the
   top frame highlighted (a glow or a check) — directly reads as "many similar,
   one chosen." Clearest tie to dedup.
2. **Find the gem**: a sparkle/4-point star nested in a photo corner.
3. **Aperture → one**: overlapping squares/aperture blades resolving into a
   single clean frame.

**Style:** modern macOS app-icon language (Big Sur+ rounded-rectangle canvas,
content on the 1024 grid with soft ambient shadow). Flat with a *gentle* single
gradient, friendly geometry, 1–2 accent colors. Not skeuomorphic.

**Color:** recommend a calm **teal→green** accent — it ties to the app's
existing "keep = green" semantic (see §7). Indigo is an acceptable alternative.
Provide the icon reading well on both light and dark desktops.

**Deliverables:**
- App-icon **master as layered SVG**, plus exported PNGs at
  16/32/64/128/256/512/1024 (@1x **and** @2x). Briefcase consumes an icon set —
  target `app/src/photocleanup/resources/photocleanup.icns` (or PNG set briefcase
  can assemble); confirm path with the build config.
- A **horizontal wordmark lockup** ("Library Cleanup", icon + text) for the app
  header — light, dark, and **monochrome** variants.
- A small **inline-SVG mark** (single color, currentColor) for the in-app top bar.

## 5. Screens & states

Derived from the real API (`health`, `scan`, `candidates?layer=dedup|screenshots`,
`thumb`, `decisions`, `finalize`, `learn`). v1 covers two **layers**: *Duplicate
photoshoots* and *Work screenshots*. The layout must leave room to add more
layers later (Expired utility photos, Videos already exist in the engine).

**A. Home / Start a review**
- Pick **scope**: presets (This year · Last year · All time) + custom date range.
- Pick **layer**: Duplicate photoshoots / Work screenshots.
- Primary CTA: **Scan**.
- Show **status**: library connected / Full Disk Access needed; a quiet line of
  reassurance ("Everything runs on your Mac. Nothing is uploaded.").
- A small **activity summary** (e.g. "1,331 kept · 784 cleared so far").
- **First-run / empty** variant that explains the privacy promise and how
  suggestions work ("we pre-pick the best; you just correct").

**B. Scanning / loading**
- Indeterminate-but-reassuring progress while the app embeds + groups (can take
  seconds to minutes). On-device, calm copy. Cancellable.

**C. Review grid — the core screen**
- *Dedup:* candidates come as **groups** (bursts). Render each group as a
  section: header = "Burst · 12 shots · keep 3 / clear 9" + group-level actions
  (**Accept suggestions**, Keep all, Clear all, collapse). Below: a wrapping row
  of **photo cards**. Suggested keepers are **pre-selected** (accent ring +
  check); suggested discards are visually demoted (muted + a "clear" affordance).
  **Tapping a card toggles keep ⇄ clear.**
- *Screenshots:* a **flat grid** (no groups), every card suggested-to-clear; the
  user taps to **rescue** (keep). Show a short reason + OCR snippet (the API
  provides `subtitle`).
- **Sticky progress** ("8 / 48 groups reviewed") and a sticky primary action.
- Per-photo meta shown lightly: a ♥ badge when `favorite`; filename + focus/score
  on hover (don't clutter the default view).
- **Keyboard:** arrow keys to move, **space** to toggle, **enter** to open
  compare. (Power-user nicety; large library = lots of tapping.)

**D. Compare / detail (optional but valuable)**
- Enlarge a card, or compare 2–3 near-identical frames side by side to decide
  which smile/angle to keep. This is where dedup decisions actually get made.

**E. Finalize / confirm**
- Summary: "Keeping **N**, clearing **M** across **G** groups."
- Primary: **Lock in & delete cleared** — make clear that macOS will show its own
  delete confirmation and that kept photos are marked reviewed (won't be shown
  again). Reassure: nothing leaves the device; deletes are recoverable from
  Recently Deleted.

**F. Done**
- "Saved — and the picker learned a little from your choices." CTA to start
  another scope.

**Cross-cutting states to design:** empty ("Nothing to clean here ✨"),
no-Full-Disk-Access (clear how-to-grant card), error, in-progress.

## 6. Key interaction principle

**The suggestion is pre-applied; the user only corrects it.** The single most
important thing the design must convey: *these green picks are our suggestion —
change any of them with one tap.* Selection state must be unmistakable at a
glance across a dense grid, and toggling must feel instant and reversible.

## 7. Visual system (evolve, don't reinvent)

The CLI already ships an HTML report with a working visual language — keep
continuity:

- **Semantic colors:** keep = green (~`#2e7d32`), clear/discard = red
  (~`#c62828`), neutral grays for chrome. The designer may refine hues, but
  **keep these semantics** (green = safe/keep, red = will-be-cleared).
- **Accent / brand** color from the logo (recommend teal→green) for primary
  actions and the keeper selection ring.
- System font; `color-scheme: light dark`; subtle separators; compact macOS
  density.

**Define and deliver as CSS custom properties** (a token sheet I can drop into
the build): color (brand, keep, clear, warning, surface, text, border for light
*and* dark), type scale, spacing scale, radius, elevation/shadow.

## 8. Deliverables checklist

- [ ] App-icon master SVG + PNG set + `.icns` guidance.
- [ ] Wordmark lockup (light / dark / mono) + inline top-bar mark.
- [ ] **CSS design-token sheet** (`:root` + dark overrides) — directly usable.
- [ ] High-fidelity mockups for screens **A–F**, in **light and dark**
      (HTML/CSS preferred since that's the delivery medium; images acceptable).
- [ ] Component sheet: photo card (all selection/favorite/hover states), group
      header + actions, buttons, scope/range picker, sticky progress, empty /
      error / no-FDA states.
- [ ] Implementation notes / redlines (spacing, sizes, motion) — the UI will be
      hand-built in HTML/JS in Phase 3.

## 9. Out of scope for v1

Expired-utility and Video review layers (engine supports them; UI later),
multi-user/accounts, heavy animation, settings beyond rescan + library path.

## 10. Reference: real data shape

A `GET /api/candidates?layer=dedup` response — use these real fields/labels so
mockups aren't generic:

```json
{
  "layer": "dedup",
  "since": "2023-12-20", "until": "2023-12-31",
  "groups": [
    {
      "group_key": "9F3A…",
      "size": 12,
      "suggested_keep": 3,
      "suggested_discard": 9,
      "photos": [
        {
          "uuid": "9F3A…",
          "filename": "IMG_3753.HEIC",
          "width": 4032, "height": 3024,
          "favorite": true,
          "suggested_keep": true,
          "score": 3.278,
          "focus": 184.0,
          "timestamp": 1703185200.0,
          "subtitle": "",
          "thumb": "/api/thumb/9F3A…",
          "decided": null
        }
      ]
    }
  ]
}
```

For `layer=screenshots`, photos are flat (one pseudo-group), all
`suggested_keep:false`, and `subtitle` carries the reason + an OCR snippet, e.g.
`"work-app:slack · keyword — \"stand-up notes…\""`.

---

*All processing is on-device; the UI talks only to `127.0.0.1`. Design as if the
machine is offline — because it should work that way.*
