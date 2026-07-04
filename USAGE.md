# USAGE — step-by-step

A practical runbook for `photo-cleanup`. Commands assume the project lives at
`~/Projects/library_cleanup` (adjust to wherever you cloned it) and that
you run them with `uv` (no manual venv activation needed).

> **Keep this file current.** Whenever a command, flag, or workflow step
> changes, update the relevant section here in the same change.

---

## Running the Mac app (Library Cleanup)

The graphical way to do everything below — no Terminal needed. The app lives in
[app/](app/) (Briefcase/Toga shell + local FastAPI + WebView; everything stays
on-device).

**Build & install**

```sh
bash app/scripts/setup-signing.sh      # once: create the self-signed identity
bash app/scripts/build-signed-dmg.sh   # bumps the patch version, signs, builds the DMG
open "app/dist/Library Cleanup-<version>.dmg"   # drag to Applications
```

**Permissions (one-time)** — the app asks for **Photos** access at first launch
(needed to delete). It also needs **Full Disk Access** to read the library
database: System Settings ▸ Privacy & Security ▸ Full Disk Access ▸ enable
*Library Cleanup*. If a scan fails, the home screen shows which permission is
missing and an "Open log" button (`~/Library/Logs/Library Cleanup/`).

**Flow**

1. **Analyze Library** — one weighted progress bar across phases (reading,
   analyzing photos, faces, grouping, videos, takes). Cancel genuinely stops
   the scan (server-side), not just the progress screen.
2. **Categories** — pick layers (burst clones / repeat takes / screenshots /
   expired), optionally narrow the time period with the histogram sliders
   (or click a bar to focus that month).
3. **Review** — grid of true-aspect cards grouped per photoshoot. Click or use
   ←/→/↑/↓ to move, **Space** to toggle keep/remove; per-group and global
   Keep all / Remove all; draggable high-res preview panel with video playback.
4. **Review & Finalize** — records your keeps (marked `reviewed:keep`, hidden
   from future scans), then deletes via PhotoKit (macOS confirms; items go to
   Recently Deleted for 30 days).

**Review manually** (secondary button on Categories) shows *everything* in the
range — including singles and already-kept items — as one chronological feed,
all pre-selected keep; finalizing only removes what you unmarked (nothing is
locked as reviewed).

Decisions are mirrored to browser storage as you review: if the app quits
mid-review, the home screen offers **Resume review** on next launch.

Every finalized review retrains the learning loops in the background (same
models as the CLI `learn` command): dedup keeps refining the keeper model, and
the screenshots/expired layers learn per-signal keep-rates — a kind you
consistently keep (say, `parking/boarding` photos or `document` screenshots)
stops being flagged after ~5 consistent keeps.

---

## Running it yourself (no Claude needed)

Everything is a normal CLI you run in **Terminal.app** (already authorized to
control Photos). A whole year of dedup is just two commands + your review:

```sh
cd ~/Projects/library_cleanup

# 1) tag the year's near-duplicate photoshoots (keepers pre-♥)
uv run photo-cleanup dedup --since 2023-01-01 --until 2023-12-31 --apply

# 2) in Photos: review the `cleanup:duplicate` Smart Album, ♥ extra keepers,
#    then delete [cleanup:duplicate AND Favorite is No]

# 3) finalize + lock the year in ONE command
uv run photo-cleanup finalize --since 2023-01-01 --until 2023-12-31 --apply
```

`finalize --since/--until` un-tags your keepers, un-favorites only the hearts
added this round (genuine favorites preserved), marks them `reviewed:keep`, and
locks the rest of the year — and it deliberately leaves any still-tagged (not yet
deleted) photos unlocked so you can still delete them. Run `learn` whenever you
like to fold your choices into the keeper model. Dry-run anything by omitting
`--apply`.

## 0. What it does (today)

- **Work-screenshot triage** — finds work screenshots (Slack/Teams, documents,
  dev/business text) and tags them `cleanup:screenshot` for you to review and
  delete. Private content is kept (WhatsApp/Instagram/Facebook, chats, memes,
  photos of people, the Hidden album).
- **Photoshoot dedup** — groups a whole **session** (same place, one visit:
  ≤10 min gaps / ≤150 m) and keeps the best, most *diverse* **1–4** shots of it
  (on-device Apple Vision embeddings + farthest-point selection), discarding the
  rest of the shoot. So a 40-shot photoshoot at one spot collapses to ~4 varied
  keepers (the view, the couple, each person…), not 15. Discards →
  `cleanup:duplicate`. Knobs in `Config`: `cluster_gap_seconds`/`cluster_gps_meters`
  (session size), `keeper_tiers`/`keepers_max` (how many to keep),
  `keeper_diversity_min` (how different keepers must be). Favorites are never
  discarded. `dedup --include-reviewed` re-examines `reviewed:keep` photos.

Everything runs on-device. No uploads, no cloud APIs.

---

## 1. One-time setup

### 1a. Install dependencies
```sh
cd ~/Projects/library_cleanup
uv sync
```

### 1b. Full Disk Access (needed to READ the Photos library)
System Settings → Privacy & Security → **Full Disk Access** → enable the app you
run commands from (your **Terminal**, and **Claude** if running read commands
from there). Fully quit and reopen the app afterward.

### 1c. Automation → Photos (needed to WRITE: tag / favorite / undo)
The first time you run a write command **from Terminal**, macOS shows
*"Terminal wants access to control Photos"* → click **OK**. If it doesn't appear,
see Troubleshooting.

---

## 2. Screenshot workflow (full sequence)

### Step 1 — Scan & preview (read-only, safe)
```sh
uv run photo-cleanup scan --open
```
Reads the library, writes/loads a metadata cache, builds `cleanup-report.html`
and opens it. Nothing is changed. Re-run with `--rescan` to refresh the cache.

### Step 2 — See how many work screenshots would be tagged (dry run)
```sh
uv run photo-cleanup apply
```

### Step 3 — Snapshot existing favorites BEFORE you start reviewing
```sh
uv run photo-cleanup fav-baseline
```
Records candidates that are *already* Favorited so the rescue step never strips
the heart off a genuine favorite.

### Step 4 — Tag the work screenshots  ⚠️ run from Terminal
```sh
uv run photo-cleanup apply --apply
```
Tip: test first with `--limit 5 --apply`, verify, then run the full command.

### Step 5 — Review in Photos
Photos app → **File → New Smart Album** → condition **Keyword is `cleanup:screenshot`**.
Browse it. **Favorite (♥) any screenshot you want to KEEP** (select + tap the
heart, or press `.`).

### Step 6 — Rescue the keepers
```sh
# read-only: see what you flagged
uv run photo-cleanup rescue-plan

# ⚠️ run from Terminal — un-tag the keepers, then un-favorite the ones you just added
uv run photo-cleanup clear-tags --apply
uv run photo-cleanup unfavorite --apply
```

### Step 7 — Delete
In the Smart Album (now only true discards): `⌘A` → Delete.

---

## 2b. Photoshoot dedup workflow (staged)

Find near-duplicate bursts and keep the best, most diverse 1–4 per moment.

Dedup uses a **different review model** than screenshots: the whole burst is
tagged and the suggested keepers are **Favorited**, so you see each full burst
with picks pre-marked and decide what (if anything) to add before deleting.

```sh
# 1) Precompute embeddings once for the whole library (read-only; safe here or in
#    Terminal). Long first pass; cached afterward (~/.cache/photo-cleanup/embeddings.npz).
uv run photo-cleanup embed

# 2) Review a date-range stage (dry run -> HTML report, no changes):
uv run photo-cleanup dedup --since 2026-05-01 --until 2026-05-31 --open

# 3) Tag the whole burst + Favorite suggested keepers  ⚠️ run from Terminal
#    (this AUTOMATICALLY snapshots your pre-existing favorites first, so step 7
#     never un-hearts a genuine favorite).
uv run photo-cleanup dedup --since 2026-05-01 --until 2026-05-31 --apply
```

5. In Photos, make a Smart Album **[Keyword is `cleanup:duplicate`]**. Each burst
   shows in full, suggested keepers already ♥. **Favorite any additional frames
   you want to keep** (you have the whole burst for context).
6. Make a Smart Album **[Keyword is `cleanup:duplicate`] AND [Photo is not
   Favorite]** → that's the delete set → select all → delete.
7. Finalize the survivors in one shot (un-tag, un-favorite the tool's hearts,
   mark them `reviewed:keep`):
   `rescue-plan --prefix cleanup:duplicate` (read-only) → `finalize --apply`
   (⚠️ Terminal; does all three writes in a single Photos session).
8. (optional) Lock the whole event so nothing from it is ever reconsidered:
   `mark-reviewed --since 2026-05-01 --until 2026-05-31 --apply`.

**`reviewed:keep`** is a permanent keyword, excluded from every future `scan`/
`dedup` pass (like the Hidden album). It lives outside the `cleanup:` namespace,
so `undo`/`clear-tags` never remove it.

`embed` and the dry-run `dedup` are read-only w.r.t. Photos; only `dedup --apply`
(and the cleanup writes) touch Photos and must run from Terminal.

## 2c. Learning engine (keeper suggestions improve over time)

The tool learns which frame to keep from *your* choices. Each `dedup --apply`
logs the burst (every member's Apple feature vector + the suggested keepers).
After you finish an iteration (reviewed + deleted), run:

```sh
uv run photo-cleanup learn
```

It compares suggestions vs what you actually kept (kept = still in the library;
discarded = deleted) and trains a small on-device model over Apple's aesthetic
sub-scores **plus `VNDetectFaceCaptureQuality`** (eyes-open / smile / sharp face
— the "small details" that decide a burst). Future `dedup` suggestions use it.

The model is **anchored to the proven heuristic** and only nudged by your data,
so a noisy iteration can't make it worse. It sharpens as you accumulate more
iterations — especially the cases where you override a suggestion.

## 2d. Expired single-purpose photos (receipts/wifi/parking…)

Flag aged utility shots that had a use at the time but not years later. Same
review model as screenshots (tag candidates → Favorite to rescue → delete rest).

```sh
uv run photo-cleanup expired --open                 # dry-run report
uv run photo-cleanup expired --apply                # ⚠️ Terminal — tag cleanup:expired
```

Age is **per type** (`Config.expired_age_by_type`) — wifi ~3 months,
parking/boarding ~5 weeks, tickets/orders ~6 months, receipts 2 years, ID photos
5 years (kept on purpose). Only flags photos with a specific utility label
(receipt/QR/barcode/ID) or real utility text (wifi/password/receipt/parking/…);
anything with people/pets/food/scenery is never flagged. Review the `cleanup:expired` Smart Album, ♥ to keep,
delete `[cleanup:expired AND Favorite is No]`, then finalize/lock as usual.

## 2e. Video cleanup (Apple does none of this)

```sh
uv run photo-cleanup videos --open                  # dry-run report
uv run photo-cleanup videos --large-mb 300 --open   # custom oversized threshold
uv run photo-cleanup videos --apply                 # ⚠️ Terminal — tag
```

Both checks share one tag, `cleanup:video`, with a favorite-driven review:
- **Near-duplicate takes** (same thing shot close together — poster-frame
  embeddings): the keeper is the take with the **most original metadata** (GPS +
  camera EXIF → prefers the device/AirDrop original over a metadata-stripped
  messaging copy), tie-broken by best **size/quality ratio**. The keeper is ♥;
  extra takes stay un-♥ (delete candidates).
- **Oversized videos** (≥200 MB, `--large-mb`): **all Favorited** (kept by
  default) — you un-♥ the ones you decide to drop.

Then in Smart Album **[Keyword is `cleanup:video`]**, delete
**[`cleanup:video` AND Favorite is No]**. Finalize/lock as usual (un-♥ baseline
preserved). Excludes Hidden/Shared/`reviewed:keep`.

## 3. Bail out / revert
```sh
uv run photo-cleanup undo --apply      # ⚠️ Terminal; removes ALL cleanup:* keywords
```
`undo` reads the library, so its Terminal also needs Full Disk Access.

---

## 4. Tuning accuracy

All on-device; edit and re-run `scan`/`apply` (dry run) to see the effect.

- **Work / private word lists:** `photo_cleanup/lexicon.py`
  - `WORK_APPS` (weight 3), `WORK_DEV`, `WORK_BIZ` (weight 1)
  - `WORK_CHAT_APPS` → always work (Slack/Teams)
  - `PRIVATE_APPS` → always kept (WhatsApp/Instagram/Facebook/…)
  - `PRIVATE_UI`, `PRIVATE_CASUAL` (EN + CZ)
- **Thresholds & labels:** `photo_cleanup/model.py` → `Config`
  - `work_min_score` (default 3), `enable_doc_fallback`, `keep_labels`,
    clustering/pHash settings for dedup.

Rule of thumb: a work screenshot wrongly kept → add its distinctive word to a
WORK list; a private one wrongly flagged → add its word to a PRIVATE list.

---

## 5. Command reference

| Command | Reads library | Writes Photos | Purpose |
|---|---|---|---|
| `scan` | yes | no | analysis + HTML report |
| `embed` | yes | no | precompute Vision embeddings for dedup |
| `dedup` | yes | only with `--apply` | near-dup report; tag discards `cleanup:duplicate` |
| `expired` | yes | only with `--apply` | flag aged single-purpose photos `cleanup:expired` |
| `videos` | yes | only with `--apply` | near-dup takes + oversized videos → `cleanup:video` (keepers/large favorited) |
| `learn` | yes | no | train the keeper model from your past keep/discard choices |
| `apply` | uses cache | yes | tag work screenshots `cleanup:screenshot` |
| `fav-baseline` | yes | no | snapshot pre-existing favorites |
| `rescue-plan` | yes | no | compute keepers to un-tag / un-favorite |
| `clear-tags` | no | yes | remove `cleanup:*` from rescued uuids |
| `finalize` | yes (with `--since`) | yes | one-shot: un-tag + unfavorite + mark-reviewed + lock range |
| `unfavorite` | no | yes | un-favorite the rescue-only hearts |
| `undo` | yes | yes | remove all `cleanup:*` keywords |

All write commands are **dry-run by default**; add `--apply` to commit.

---

## 6. Troubleshooting

### `Not authorized to send Apple events to Photos. (-1743)`
The write command isn't authorized to control Photos. Run it from an
**interactive Terminal.app** (not from an editor/automation context) so macOS
shows the consent prompt; click **OK**. If the prompt never appears, reset just
this app's Apple-event grants and retry from Terminal:
```sh
tccutil reset AppleEvents com.apple.Terminal
```

### `Operation not permitted` reading the library
Full Disk Access isn't granted for the app running the command. Grant it
(Step 1b) and fully restart that app.

### Running write-back without opening Terminal yourself
Because the consent prompt only appears for an interactive Terminal, the helper
scripts `run_apply.command` / `run_rescue.command` (git-ignored, local only) are
launched with `open -a Terminal …`; the Apple events then come from the
already-authorized Terminal. Regenerate them as small zsh scripts that `cd` here
and run the desired `uv run photo-cleanup … --apply` command.

### Hidden album
Photos in the macOS **Hidden** album are always excluded — never scanned,
tagged, or reviewed.
