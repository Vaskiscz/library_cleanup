"""Command-line interface. Stage 1 is read-only: scan -> analyze -> HTML report.

Nothing writes back to the Photos library yet (that's `apply`, stage 2).
"""

from __future__ import annotations

import os
import sys
import time

import click

from . import apply as apply_mod
from .analyze import analyze
from .model import Config
from .report import write_report
from .scan import load_records, save_records, scan_library

DEFAULT_CACHE = os.path.expanduser("~/.cache/photo-cleanup/records.json")
DEFAULT_REPORT = os.path.abspath("./cleanup-report.html")


@click.group()
def cli():
    """On-device dedup & work-screenshot triage for Apple Photos."""


@cli.command()
@click.option("--db", "dbpath", default=None, help="Photos library path (default: system library).")
@click.option("--cache", default=DEFAULT_CACHE, show_default=True, help="Records cache file.")
@click.option("--rescan", is_flag=True, help="Re-read the library even if a cache exists.")
@click.option("--report", "report_path", default=DEFAULT_REPORT, show_default=True)
@click.option("--limit", type=int, default=0, help="Analyze only the first N photos (testing).")
@click.option("--open", "open_report", is_flag=True, help="Open the report when done.")
def scan(dbpath, cache, rescan, report_path, limit, open_report):
    """Read-only scan + analysis. Produces a local HTML review report."""
    t0 = time.time()

    if rescan or not os.path.exists(cache):
        click.echo("Reading Photos library (read-only)…")
        try:
            records = scan_library(dbpath)
        except Exception as e:
            _hint_permission(e)
            sys.exit(1)
        save_records(records, cache)
        click.echo(f"  cached {len(records)} photos -> {cache}")
    else:
        records = load_records(cache)
        click.echo(f"Loaded {len(records)} photos from cache ({cache}). Use --rescan to refresh.")

    if limit:
        records = records[:limit]
        click.echo(f"  (limited to {len(records)} for this run)")

    click.echo("Analyzing (screenshots + near-duplicates)… this decodes thumbnails, may take a bit.")
    cfg = Config()
    findings = analyze(records, cfg)

    out = write_report(findings, cfg, report_path)
    dt = time.time() - t0
    click.echo("")
    click.echo(f"  scanned ............. {findings.total_scanned}")
    click.echo(f"  work screenshots .... {len(findings.work_screenshots)}")
    click.echo(f"  duplicate groups .... {len(findings.duplicate_groups)}")
    click.echo(f"  discard candidates .. {findings.n_discards}")
    click.echo(f"  keepers to favorite . {findings.n_keepers_marked}")
    click.echo("")
    click.echo(f"Report: {out}   ({dt:.1f}s)")
    click.echo("Nothing changed. Review the report, then (stage 2) run `apply` to tag.")

    if open_report:
        os.system(f'open "{out}"')


def _load_or_scan(cache, dbpath, rescan):
    if rescan or not os.path.exists(cache):
        records = scan_library(dbpath)
        save_records(records, cache)
        return records
    return load_records(cache)


@cli.command()
@click.option("--cache", default=DEFAULT_CACHE, show_default=True)
@click.option("--rescan", is_flag=True, help="Re-read the library before tagging.")
@click.option("--apply", "do_apply", is_flag=True,
              help="Actually write to Photos. Without this it's a dry run.")
@click.option("--limit", type=int, default=0, help="Tag only the first N (test batch).")
def apply(cache, rescan, do_apply, limit):
    """Tag flagged work screenshots with `cleanup:screenshot` (dry-run by default)."""
    records = _load_or_scan(cache, None, rescan)
    findings = analyze(records, Config())
    uuids = [rec.uuid for rec, _ in findings.work_screenshots]
    if limit:
        uuids = uuids[:limit]

    mode = "APPLY (writing to Photos)" if do_apply else "DRY RUN (no changes)"
    click.echo(f"[{mode}] {len(uuids)} work screenshots -> {apply_mod.KW_SCREENSHOT}")
    if not uuids:
        return
    if do_apply:
        click.echo("Driving the Photos app via AppleScript… (Photos will open)")

    def prog(i, n):
        if i % 25 == 0 or i == n:
            click.echo(f"  {i}/{n}", nl=True)

    try:
        res = apply_mod.add_keyword(uuids, apply_mod.KW_SCREENSHOT,
                                    apply=do_apply, progress=prog if do_apply else None)
    except Exception as e:
        _hint_automation(e)
        sys.exit(1)

    if do_apply:
        click.echo(f"  tagged {res.tagged}, already-tagged {res.skipped}, errors {res.errors}")
        click.echo("Done. In Photos, make a Smart Album on keyword "
                   f"'{apply_mod.KW_SCREENSHOT}' to review and delete. Revert with `undo`.")
    else:
        click.echo(f"  would tag {res.tagged}. Re-run with --apply to write.")


@cli.command()
@click.option("--apply", "do_apply", is_flag=True, help="Actually remove the keywords.")
@click.option("--prefix", default=apply_mod.KEYWORD_PREFIX, show_default=True)
def undo(do_apply, prefix):
    """Remove all `cleanup:*` keywords this tool added (reversible cleanup)."""
    mode = "APPLY" if do_apply else "DRY RUN"
    click.echo(f"[{mode}] clearing keywords starting with '{prefix}'…")

    def prog(i, n):
        if i % 25 == 0 or i == n:
            click.echo(f"  {i}/{n}")

    try:
        res = apply_mod.undo_keywords(prefix, apply=do_apply,
                                      progress=prog if do_apply else None)
    except Exception as e:
        _hint_automation(e)
        sys.exit(1)
    verb = "cleared" if do_apply else "would clear"
    click.echo(f"  {verb} {res.tagged} photos (errors {res.errors}).")


RESCUE_FILE = "/tmp/photo_cleanup_rescue.json"          # tagged favorites -> un-tag
UNFAV_FILE = "/tmp/photo_cleanup_unfavorite.json"       # newly favorited -> un-favorite
FAV_BASELINE_FILE = "/tmp/photo_cleanup_fav_baseline.json"  # pre-existing favorites


@cli.command(name="fav-baseline")
@click.option("--prefix", default=apply_mod.KEYWORD_PREFIX, show_default=True)
@click.option("--out", default=FAV_BASELINE_FILE, show_default=True)
def fav_baseline(prefix, out):
    """Snapshot which candidates are ALREADY Favorited (genuine favorites), so a
    later rescue won't un-favorite them. Run this BEFORE you start hearting keepers."""
    import json
    uuids = apply_mod.find_rescue_uuids(prefix, use_favorites=True)
    with open(out, "w") as f:
        json.dump(uuids, f)
    click.echo(f"baseline: {len(uuids)} candidate(s) already Favorited (will be preserved).")
    click.echo(f"written to {out}")


@cli.command(name="rescue-plan")
@click.option("--by", type=click.Choice(["favorite", "album"]), default="favorite",
              show_default=True, help="How you flagged keepers in Photos.")
@click.option("--album", default="Keep", show_default=True,
              help="Album name when --by album.")
@click.option("--prefix", default=apply_mod.KEYWORD_PREFIX, show_default=True)
@click.option("--baseline", default=FAV_BASELINE_FILE, show_default=True)
def rescue_plan(by, album, prefix, baseline):
    """Read-only: find tagged photos you flagged to KEEP. Writes two lists:
    everything to UN-TAG, and (excluding pre-existing favorites) what to UN-FAVORITE."""
    import json, os as _os
    rescued = apply_mod.find_rescue_uuids(
        prefix, use_favorites=(by == "favorite"), album=(album if by == "album" else None))
    with open(RESCUE_FILE, "w") as f:
        json.dump(rescued, f)

    base = []
    if by == "favorite" and _os.path.exists(baseline):
        with open(baseline) as f:
            base = json.load(f)
    to_unfav = [u for u in rescued if u not in set(base)] if by == "favorite" else []
    with open(UNFAV_FILE, "w") as f:
        json.dump(to_unfav, f)

    flag = "Favorited" if by == "favorite" else f"in album '{album}'"
    click.echo(f"{len(rescued)} tagged photos are {flag} -> will be UN-TAGGED (kept).")
    if by == "favorite":
        click.echo(f"  of those, {len(to_unfav)} were newly favorited -> will be UN-FAVORITED")
        click.echo(f"  ({len(base)} pre-existing favorite(s) preserved).")
    click.echo(f"lists: {RESCUE_FILE}, {UNFAV_FILE}")


@cli.command(name="clear-tags")
@click.option("--uuids-file", default=RESCUE_FILE, show_default=True)
@click.option("--prefix", default=apply_mod.KEYWORD_PREFIX, show_default=True)
@click.option("--apply", "do_apply", is_flag=True, help="Actually remove the keywords.")
def clear_tags(uuids_file, prefix, do_apply):
    """Write-only: remove `prefix` keywords from the uuids in a file. Needs only
    Photos automation (no library read), so it runs fine from Terminal."""
    import json
    with open(uuids_file) as f:
        uuids = json.load(f)
    mode = "APPLY" if do_apply else "DRY RUN"
    click.echo(f"[{mode}] clearing '{prefix}*' from {len(uuids)} photos")

    def prog(i, n):
        if i % 25 == 0 or i == n:
            click.echo(f"  {i}/{n}")

    try:
        res = apply_mod.clear_keywords_for_uuids(
            uuids, prefix, apply=do_apply, progress=prog if do_apply else None)
    except Exception as e:
        _hint_automation(e)
        sys.exit(1)
    verb = "cleared" if do_apply else "would clear"
    click.echo(f"  {verb} {res.tagged}, unchanged {res.skipped}, errors {res.errors}")


@cli.command(name="unfavorite")
@click.option("--uuids-file", default=UNFAV_FILE, show_default=True)
@click.option("--apply", "do_apply", is_flag=True, help="Actually remove the Favorite flag.")
def unfavorite(uuids_file, do_apply):
    """Write-only: remove the Favorite flag from the uuids in a file (the keepers
    you favorited only as a rescue marker). Runs fine from Terminal."""
    import json
    with open(uuids_file) as f:
        uuids = json.load(f)
    mode = "APPLY" if do_apply else "DRY RUN"
    click.echo(f"[{mode}] un-favoriting {len(uuids)} photos")

    def prog(i, n):
        if i % 25 == 0 or i == n:
            click.echo(f"  {i}/{n}")

    try:
        res = apply_mod.unfavorite_uuids(uuids, apply=do_apply, progress=prog if do_apply else None)
    except Exception as e:
        _hint_automation(e)
        sys.exit(1)
    verb = "un-favorited" if do_apply else "would un-favorite"
    click.echo(f"  {verb} {res.favorited}, already-not {res.skipped}, errors {res.errors}")


def _hint_automation(e: Exception):
    click.echo(f"\nERROR writing to Photos: {e}\n", err=True)
    click.echo(
        "Writing needs the Photos app and Automation permission. Grant it:\n"
        "  System Settings → Privacy & Security → Automation → Claude → enable 'Photos'.\n"
        "(Also keep Accessibility enabled for Claude.) Then retry.",
        err=True,
    )


def _hint_permission(e: Exception):
    click.echo(f"\nERROR reading the library: {e}\n", err=True)
    click.echo(
        "If this says 'Operation not permitted', macOS is blocking access.\n"
        "Grant Full Disk Access to your terminal:\n"
        "  System Settings → Privacy & Security → Full Disk Access → enable your terminal,\n"
        "  then fully quit and reopen it and re-run.",
        err=True,
    )


if __name__ == "__main__":
    cli()
