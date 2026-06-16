# USAGE — step-by-step

A practical runbook for `photo-cleanup`. Commands assume the project lives at
`/Users/vaclavtrnka/Projects/apple_photo_cleanup` and that you run them with
`uv` (no manual venv activation needed).

> **Keep this file current.** Whenever a command, flag, or workflow step
> changes, update the relevant section here in the same change.

---

## 0. What it does (today)

- **Work-screenshot triage** — finds work screenshots (Slack/Teams, documents,
  dev/business text) and tags them `cleanup:screenshot` for you to review and
  delete. Private content is kept (WhatsApp/Instagram/Facebook, chats, memes,
  photos of people, the Hidden album).
- **Photoshoot dedup** — finds near-duplicate bursts (multiple shots of the same
  moment) using on-device **Apple Vision feature-print embeddings** (content
  similarity, robust to reframing/angle), and keeps the best, most *diverse*
  1–4 per burst (more shots in a burst → more keepers). Analysis is validated;
  the CLI command + write-back (`cleanup:duplicate` on discards) is being wired
  into the main flow. Tunables in `Config`: `embedding_max_distance` (0.25),
  `keeper_tiers`, `keepers_max`, `keeper_diversity_min`.

Everything runs on-device. No uploads, no cloud APIs.

---

## 1. One-time setup

### 1a. Install dependencies
```sh
cd /Users/vaclavtrnka/Projects/apple_photo_cleanup
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
| `apply` | uses cache | yes | tag work screenshots `cleanup:screenshot` |
| `fav-baseline` | yes | no | snapshot pre-existing favorites |
| `rescue-plan` | yes | no | compute keepers to un-tag / un-favorite |
| `clear-tags` | no | yes | remove `cleanup:*` from rescued uuids |
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
