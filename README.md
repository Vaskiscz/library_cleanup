# photo-cleanup

On-device deduplication & work-screenshot triage for the Apple Photos library.

Built for two jobs:
1. **Dedup photoshoots** — many near-identical shots of the same place/person → keep the best 1–3, flag the rest.
2. **Remove work screenshots** — Slack/code/docs/spreadsheets/tickets → flag for deletion. Memes, recipes, maps, photos-of-people are kept.

**Guiding rule: when in doubt, keep.** A one-of-a-kind photo is *never* flagged. The tool only proposes removing something that has a better near-duplicate, or a high-confidence work screenshot.

📖 **[USAGE.md](USAGE.md) — exact step-by-step runbook** (setup, the full tag → review → rescue → delete sequence, tuning, troubleshooting).

## Everything stays on your Mac

No network, no cloud APIs, no uploads, no telemetry. Specifically:

- `osxphotos` reads the local library database & files directly.
- Perceptual hashing & sharpness (`Pillow`, `numpy`, `scipy`, `imagehash`) run in local memory.
- Screenshot classification uses **Apple's own on-device data** already stored in the library — Vision OCR text and ML scene labels. Nothing is re-uploaded for analysis.
- The HTML report is a single local file (thumbnails embedded as base64); it is opened from disk, never served.

The only "cloud" anywhere near this is *your existing* iCloud Photos sync. This tool never enables or uses it. (Stage 2 write-back sets a Favorite/keyword, which Apple may later sync as a metadata change — the same as hearting a photo by hand. Pause iCloud if you want to avoid even that.)

## Install

```sh
uv sync
```

## Usage (stage 1 — read-only)

```sh
uv run photo-cleanup scan --open
```

This reads the library, finds work screenshots and near-duplicate groups, and writes a local `cleanup-report.html` for review. **It changes nothing.**

Useful flags: `--limit 2000` (test on a subset), `--rescan` (refresh the cache), `--db /path/to/Library.photoslibrary`.

### macOS permission (required)

Reading the Photos library needs **Full Disk Access** for your terminal:
*System Settings → Privacy & Security → Full Disk Access* → enable your terminal app → fully quit and reopen it.

## Stage 2 — tagging, review & rescue

Write-back uses `photoscript` (local AppleScript → Photos app).

```sh
uv run photo-cleanup apply              # dry run: how many work screenshots
uv run photo-cleanup apply --apply      # tag them all with cleanup:screenshot
uv run photo-cleanup apply --limit 5 --apply   # safe test batch
uv run photo-cleanup undo --apply       # remove every cleanup:* keyword
```

**Review & rescue workflow** (mark keepers without losing them):

1. `uv run photo-cleanup fav-baseline` — snapshot which candidates are *already* Favorited (so genuine favorites are never un-favorited later).
2. In Photos, make a Smart Album on keyword `cleanup:screenshot`, review it, and **Favorite (♥) anything you want to keep**.
3. `uv run photo-cleanup rescue-plan` — finds your favorited keepers (read-only).
4. `uv run photo-cleanup clear-tags --apply` — un-tags those keepers (they leave the album).
5. `uv run photo-cleanup unfavorite --apply` — removes the heart from only the keepers *you* added (pre-existing favorites are preserved).
6. Delete everything still carrying `cleanup:screenshot`.

### Writing requires Automation permission — run write-back from Terminal

The read path (`scan`, `rescue-plan`, `fav-baseline`) works anywhere with Full Disk Access. The **write** commands (`apply`, `clear-tags`, `unfavorite`, `undo`) send Apple events to Photos, which needs **Automation** access (*System Settings → Privacy & Security → Automation → enable Photos*). macOS only shows that consent prompt for an **interactive Terminal**, so run the `--apply` commands from Terminal.app. The write-only commands (`apply`, `clear-tags`, `unfavorite`) don't read the library, so Terminal needs only the Photos prompt — not Full Disk Access.

## Status

- [x] Stage 1 — read-only scan + analysis + HTML review report
- [x] Stage 2 — `apply`/`undo` write-back, plus `fav-baseline`/`rescue-plan`/`clear-tags`/`unfavorite` review-and-rescue workflow
- [x] Photoshoot dedup — `embed` (Vision-embedding precompute) + `dedup` (staged near-duplicate report; `--apply` tags discards `cleanup:duplicate`), with adaptive, diversity-aware keepers
- [x] `reviewed:keep` — permanent "don't re-review" lock (per-keeper or per-event), excluded from all passes
- [x] Expired-utility layer (`expired`) — flags aged single-purpose photos (receipts/wifi/parking/tickets) via Apple labels + OCR + age; tags `cleanup:expired`
- [x] Learning engine (`learn`) — trains an on-device keeper model from your keep/discard choices over Apple's aesthetic sub-scores + `VNDetectFaceCaptureQuality`; anchored to the heuristic, improves each iteration

## Layout

| Module | Responsibility |
|---|---|
| `model.py` | `Config` (thresholds) + `Record` (serializable per-photo data) |
| `scan.py` | read library → Records (metadata only, no decoding) + JSON cache |
| `screenshots.py` | high-confidence work-screenshot classifier |
| `cluster.py` | time/GPS clustering, pHash near-dup confirmation, keeper selection |
| `quality.py` | Laplacian sharpness + Apple-score keeper ranking |
| `analyze.py` | orchestrates findings |
| `report.py` | self-contained local HTML report |
| `cli.py` | `photo-cleanup` command |
