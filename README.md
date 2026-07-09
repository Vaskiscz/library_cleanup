# Library Cleanup

[![CI](https://github.com/Vaskiscz/library_cleanup/actions/workflows/ci.yml/badge.svg)](https://github.com/Vaskiscz/library_cleanup/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Vaskiscz/library_cleanup?label=download&color=success)](https://github.com/Vaskiscz/library_cleanup/releases/latest)
[![License: PolyForm NC 1.0.0](https://img.shields.io/badge/license-PolyForm%20NC%201.0.0-blue)](LICENSE)

On-device cleanup for your Apple Photos library. It finds the clutter and hands back the storage, without anything leaving your Mac.

Two jobs:

1. **Dedup photoshoots and videos.** Many near-identical shots of one moment: keep the sharpest one or two, flag the rest.
2. **Clear the junk drawer.** Work screenshots (Slack, code, docs, tickets) and aged single-purpose photos (receipts, wifi codes, parking spots). Memes, recipes, maps, and photos of people are kept.

**Guiding rule: when in doubt, keep.** A one-of-a-kind photo is never flagged. The tool only proposes removing something that has a better near-duplicate or is high-confidence clutter, and nothing is deleted until you confirm.

## Get it

**Mac app (recommended).** Download the latest `Library-Cleanup.dmg` from the [releases page](https://github.com/Vaskiscz/library_cleanup/releases/latest), open it, and drag the app to Applications. Then: scan, pick categories, review a grid, delete through macOS. It keeps itself up to date from here.

**Command line.** `uv sync`, then `uv run library-cleanup scan --open` for a read-only HTML report. See [USAGE.md](USAGE.md) for the full runbook (tag, review, rescue, delete, tuning, troubleshooting).

## Is it safe? Privacy and signing

**Nothing leaves your Mac.** No cloud, no uploads, no telemetry, no analytics. All processing is local:

- `osxphotos` reads the local Photos database and files directly.
- Near-duplicate detection uses on-device **Apple Vision feature prints**; sharpness is a local Laplacian estimate (Pillow + numpy). Screenshot and expired-photo detection reuse **Apple's own Vision OCR and scene labels** already stored in your library.
- The only bytes that ever leave the device are the app's anonymous version check against GitHub and, when you choose to update, the download of the new release. Both are disclosed in [SECURITY.md](SECURITY.md). Nothing about your photos is ever sent.

The one exception is *your own* iCloud Photos sync, which this tool never enables or uses. Setting a Favorite or keyword during write-back may sync as a metadata change, exactly like hearting a photo by hand. Pause iCloud if you want to avoid even that.

**First launch.** The app is code-signed with a stable identity but not yet Apple-notarized, so macOS shows an "unidentified developer" prompt the first time. Right-click the app and choose **Open** once; after that it launches normally. It runs under the hardened runtime and asks only for Photos access. Every deletion is confirmed by macOS itself and goes to Recently Deleted, so nothing is unrecoverable.

**Updates are verified before they install.** The built-in updater only downloads over HTTPS from this repository's Releases, then checks the downloaded app's code signature and pins the expected signing identity before it replaces anything. An update that has been tampered with, or signed by anyone else, is refused. See [SECURITY.md](SECURITY.md) for the full threat model and how to report a vulnerability privately.

## What it finds

- **Photoshoot dedup** with adaptive, diversity-aware keepers (the best 1 to 3 of a burst).
- **Duplicate video takes** (near-identical clips, keep the largest) and oversized videos. Apple Photos does neither.
- **Work screenshots**: Slack, code, docs, spreadsheets, tickets.
- **Expired utility photos**: aged receipts, wifi codes, parking spots and tickets, via Apple labels, OCR, and age.
- **A "don't re-review" lock** so anything you have decided to keep is excluded from every future pass.
- **A learning engine** that trains an on-device keeper model from your own keep and discard choices, anchored to the built-in heuristic so it only improves.

Everything is reversible until you delete: the CLI tags candidates with `cleanup:*` keywords you can clear, and the app never removes anything without your confirmation.

## Development

```sh
uv sync
uv run ruff check && uv run pytest -q   # exactly what CI runs
```

| Area | Where |
|---|---|
| Scan, classify, cluster, score (the CLI engine) | `photo_cleanup/` |
| Mac app: local service + WebView UI + PhotoKit delete + self-updater | `app/` |
| Step-by-step runbook | [USAGE.md](USAGE.md) |
| Distribution and signing | [app/DISTRIBUTION.md](app/DISTRIBUTION.md) |
| Security model and reporting | [SECURITY.md](SECURITY.md) |

## License

[PolyForm Noncommercial 1.0.0](LICENSE): **free for any noncommercial use**, including personal use, study, hobby projects, research, and use by nonprofits, schools, and government. You may use, modify, and share it, provided you keep the attribution notice.

**Commercial use, including selling it or running it as a paid product or service, is not permitted** without a separate license. For a commercial license, contact the author (Václav Trnka).

Contributions are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). Submitting a contribution grants the author the right to relicense it (including commercially), so the project's licensing stays flexible.
