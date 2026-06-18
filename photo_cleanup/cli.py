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


DEFAULT_EMB_CACHE = os.path.expanduser("~/.cache/photo-cleanup/embeddings.npz")
DEFAULT_DEDUP_REPORT = os.path.abspath("./dedup-report.html")


def _filter_by_date(records, since, until):
    """Keep records whose local date is within [since, until] (YYYY-MM-DD, inclusive)."""
    from datetime import datetime
    if not since and not until:
        return records
    lo = datetime.strptime(since, "%Y-%m-%d").date() if since else None
    hi = datetime.strptime(until, "%Y-%m-%d").date() if until else None
    out = []
    for r in records:
        if r.timestamp is None:
            continue
        d = datetime.fromtimestamp(r.timestamp).date()
        if (lo is None or d >= lo) and (hi is None or d <= hi):
            out.append(r)
    return out


def _active_records(cache, since, until, include_reviewed=False):
    """Load records for a scope, excluding the Hidden album and (unless
    include_reviewed) anything already marked reviewed:keep."""
    recs = _filter_by_date(load_records(cache), since, until)
    return [r for r in recs if not r.is_hidden
            and (include_reviewed or apply_mod.KW_REVIEWED not in (r.keywords or []))]


def _cluster_candidates(records, cfg):
    """Photos that share a multi-shot time/GPS cluster — the only dedup candidates."""
    from .cluster import time_gps_clusters
    cand = []
    for c in time_gps_clusters(records, cfg):
        if len(c) >= 2:
            cand.extend(c)
    return cand


@cli.command()
@click.option("--cache", default=DEFAULT_CACHE, show_default=True)
@click.option("--emb-cache", default=DEFAULT_EMB_CACHE, show_default=True)
@click.option("--since", default=None, help="Only photos on/after YYYY-MM-DD.")
@click.option("--until", default=None, help="Only photos on/before YYYY-MM-DD.")
def embed(cache, emb_cache, since, until):
    """Precompute & cache on-device Vision embeddings for dedup candidates.
    Read-only w.r.t. Photos (only writes the embedding cache). Safe to run here."""
    from .embedding import EmbeddingCache, embed_records
    records = _active_records(cache, since, until)
    cfg = Config()
    cand = _cluster_candidates(records, cfg)
    ec = EmbeddingCache(emb_cache)
    have = sum(1 for r in cand if r.uuid in ec)
    click.echo(f"{len(cand)} dedup candidates in scope; {have} already embedded.")

    def prog(i, n):
        if i % 100 == 0 or i == n:
            click.echo(f"  embedding {i}/{n}")

    n = embed_records(cand, ec, progress=prog)
    ec.save()
    click.echo(f"computed {n} new embeddings; cache now holds {len(ec)} -> {emb_cache}")


@cli.command()
@click.option("--cache", default=DEFAULT_CACHE, show_default=True)
@click.option("--emb-cache", default=DEFAULT_EMB_CACHE, show_default=True)
@click.option("--since", default=None, help="Only photos on/after YYYY-MM-DD.")
@click.option("--until", default=None, help="Only photos on/before YYYY-MM-DD.")
@click.option("--report", "report_path", default=DEFAULT_DEDUP_REPORT, show_default=True)
@click.option("--apply", "do_apply", is_flag=True,
              help="Tag discards cleanup:duplicate (default: dry run + report only).")
@click.option("--include-reviewed", is_flag=True,
              help="Re-examine photos already marked reviewed:keep (e.g. to re-dedup "
                   "an event processed under older, stricter settings).")
@click.option("--open", "open_report", is_flag=True)
def dedup(cache, emb_cache, since, until, report_path, do_apply, include_reviewed, open_report):
    """Find near-duplicate photoshoots in scope and (dry-run) report keep/discard.
    Embeds any missing candidates on the fly. Use --since/--until to stage."""
    from .cluster import find_duplicate_groups
    from .embedding import EmbeddingCache, embed_records
    from .report import render_dedup_html

    records = _active_records(cache, since, until, include_reviewed=include_reviewed)
    cfg = Config()
    cand = _cluster_candidates(records, cfg)
    ec = EmbeddingCache(emb_cache)
    missing = sum(1 for r in cand if r.uuid not in ec)
    if missing:
        click.echo(f"embedding {missing} candidates not yet cached…")
        embed_records(cand, ec, progress=lambda i, n: None)
        ec.save()

    # Give the learned keeper model its face-quality feature for these candidates.
    from .feedback import inject_face_quality
    inject_face_quality(cand)

    groups = find_duplicate_groups(records, cfg, embeddings=ec)
    keepers = [r for g in groups for r in g.keepers]
    discards = [r for g in groups for r in g.discards]
    members = keepers + discards   # every photo that belongs to a burst

    label = f"{since or '…'} → {until or '…'}" if (since or until) else "(whole library)"
    out = os.path.abspath(report_path)
    with open(out, "w") as fh:
        fh.write(render_dedup_html(groups, len(records), cfg, label))
    click.echo(f"scope {label}: {len(records)} photos, {len(groups)} bursts, "
               f"{len(keepers)} suggested keepers, {len(discards)} discard candidates")
    click.echo(f"report: {out}")

    if not do_apply:
        click.echo("Dry run — nothing tagged. Review the report; add --apply to tag.")
    else:
        # Dedup workflow: tag the WHOLE burst cleanup:duplicate, and Favorite the
        # suggested keepers — so the Smart Album shows full bursts with picks
        # pre-marked. You add Favorites; delete = tagged AND not Favorite.
        click.echo(f"Tagging {len(members)} burst photos -> {apply_mod.KW_DUPLICATE}, "
                   f"favoriting {len(keepers)} suggested keepers …")

        def prog(i, n):
            if i % 25 == 0 or i == n:
                click.echo(f"  {i}/{n}")
        try:
            # Snapshot pre-existing favorites among the burst photos BEFORE we
            # favorite any keeper, so the later un-favorite step preserves them.
            # Read from the (freshly scanned) cache — instant vs per-photo Photos
            # reads, which matters for large scopes. Rescan before --apply if the
            # favorite state may have changed since the last scan.
            import json
            baseline = [r.uuid for r in members if r.favorite]
            with open(FAV_BASELINE_FILE, "w") as f:
                json.dump(baseline, f)
            click.echo(f"  baseline: {len(baseline)} pre-existing favorite(s) recorded "
                       f"(preserved when un-favoriting later) -> {FAV_BASELINE_FILE}")

            r1 = apply_mod.add_keyword([r.uuid for r in members], apply_mod.KW_DUPLICATE,
                                       apply=True, progress=prog)
            r2 = apply_mod.favorite([r.uuid for r in keepers], apply=True, progress=prog)
        except Exception as e:
            _hint_automation(e)
            sys.exit(1)
        click.echo(f"  tagged {r1.tagged} (already {r1.skipped}), "
                   f"favorited {r2.favorited} (already {r2.skipped}), "
                   f"errors {r1.errors + r2.errors}")
        # Record this iteration so the learning engine can train from your
        # eventual keep/discard decisions (even after you delete the discards).
        from .feedback import log_apply
        log_apply(groups, since, until)
        click.echo("Review: Smart Album [Keyword is cleanup:duplicate]. Favorite any "
                   "extra keepers. Then delete via [cleanup:duplicate AND Favorite is No].")
    if open_report:
        os.system(f'open "{out}"')


DEFAULT_EXPIRED_REPORT = os.path.abspath("./expired-report.html")


@cli.command()
@click.option("--cache", default=DEFAULT_CACHE, show_default=True)
@click.option("--since", default=None, help="Only photos on/after YYYY-MM-DD.")
@click.option("--until", default=None, help="Only photos on/before YYYY-MM-DD.")
@click.option("--min-age-years", type=float, default=None,
              help="Override: only flag photos older than this (default 2).")
@click.option("--report", "report_path", default=DEFAULT_EXPIRED_REPORT, show_default=True)
@click.option("--apply", "do_apply", is_flag=True,
              help="Tag flagged photos cleanup:expired (default: dry run + report).")
@click.option("--open", "open_report", is_flag=True)
def expired(cache, since, until, min_age_years, report_path, do_apply, open_report):
    """Flag aged single-purpose utility photos (receipts/wifi/parking/tickets…).
    Review model = same as screenshots: tag candidates, Favorite to rescue, delete rest."""
    from .expired import classify_expired
    from .report import render_expired_html

    cfg = Config()
    if min_age_years is not None:
        cfg.expired_min_age_years = min_age_years
    records = _active_records(cache, since, until)
    flagged = []
    for rec in records:
        v = classify_expired(rec, cfg)
        if v.is_expired:
            flagged.append((rec, v))

    label = f"{since or '…'} → {until or '…'}" if (since or until) else "(whole library)"
    out = os.path.abspath(report_path)
    with open(out, "w") as fh:
        fh.write(render_expired_html(flagged, len(records), cfg, label))
    click.echo(f"scope {label}: {len(records)} photos, {len(flagged)} flagged expired "
               f"(age ≥ {cfg.expired_min_age_years}y)")
    click.echo(f"report: {out}")

    if not do_apply:
        click.echo("Dry run — nothing tagged. Review the report; add --apply to tag.")
    else:
        click.echo(f"Tagging {len(flagged)} -> {apply_mod.KW_EXPIRED} …")

        def prog(i, n):
            if i % 25 == 0 or i == n:
                click.echo(f"  {i}/{n}")
        try:
            res = apply_mod.add_keyword([r.uuid for r, _ in flagged], apply_mod.KW_EXPIRED,
                                        apply=True, progress=prog)
        except Exception as e:
            _hint_automation(e)
            sys.exit(1)
        from .feedback import log_expired
        log_expired(flagged, since, until)   # for learning from your corrections
        click.echo(f"  tagged {res.tagged} (already {res.skipped}), errors {res.errors}")
        click.echo("Review: Smart Album [Keyword is cleanup:expired]. Favorite any to keep, "
                   "then delete via [cleanup:expired AND Favorite is No].")
    if open_report:
        os.system(f'open "{out}"')


DEFAULT_VIDEO_REPORT = os.path.abspath("./videos-report.html")


@cli.command()
@click.option("--since", default=None, help="Only videos on/after YYYY-MM-DD.")
@click.option("--until", default=None, help="Only videos on/before YYYY-MM-DD.")
@click.option("--large-mb", type=float, default=None, help="Oversized threshold (default 200).")
@click.option("--emb-cache", default=DEFAULT_EMB_CACHE, show_default=True)
@click.option("--report", "report_path", default=DEFAULT_VIDEO_REPORT, show_default=True)
@click.option("--apply", "do_apply", is_flag=True,
              help="Tag near-dup takes cleanup:video + oversized cleanup:large.")
@click.option("--open", "open_report", is_flag=True)
def videos(since, until, large_mb, emb_cache, report_path, do_apply, open_report):
    """Find near-duplicate video takes (keep the largest) and oversized videos.
    Apple Photos does neither. Same review/rescue/delete flow as the rest."""
    import os as _os
    from .scan import scan_library
    from .embedding import EmbeddingCache, embed_records
    from .video import duplicate_takes, large_videos, video_size
    from .report import render_videos_html

    cfg = Config()
    if large_mb is not None:
        cfg.large_video_mb = large_mb

    click.echo("Scanning videos…")
    recs = scan_library(movies_only=True)                      # excludes hidden + shared
    recs = _filter_by_date(recs, since, until)
    recs = [r for r in recs if apply_mod.KW_REVIEWED not in (r.keywords or [])
            and r.path and _os.path.exists(r.path)]

    ec = EmbeddingCache(emb_cache)
    missing = sum(1 for r in recs if r.uuid not in ec)
    if missing:
        click.echo(f"  embedding {missing} video poster frames…")
        embed_records(recs, ec)
        ec.save()

    dup_groups = duplicate_takes(recs, ec, cfg)
    larges = large_videos(recs, cfg)
    dup_discards = [r for g in dup_groups for r in g.discards]

    label = f"{since or '…'} → {until or '…'}" if (since or until) else "(whole library)"
    out = _os.path.abspath(report_path)
    with open(out, "w") as fh:
        fh.write(render_videos_html(dup_groups, larges, len(recs), cfg, label))
    gb = 1024 ** 3
    click.echo(f"scope {label}: {len(recs)} videos · {len(dup_groups)} take-groups "
               f"({len(dup_discards)} extra takes) · {len(larges)} oversized")
    click.echo(f"report: {out}")

    # Unified review model: everything tagged cleanup:video. Favorited = keep —
    # dup-group keepers (best size/quality ratio) and ALL large videos (you
    # un-favorite the large ones you decide to drop). Delete = tagged AND not fav.
    dup_members = [r for g in dup_groups for r in (g.keepers + g.discards)]
    dup_keepers = [r for g in dup_groups for r in g.keepers]
    large_recs = [lv.rec for lv in larges]
    to_tag = {r.uuid for r in dup_members} | {r.uuid for r in large_recs}
    to_fav = {r.uuid for r in dup_keepers} | {r.uuid for r in large_recs}

    if not do_apply:
        click.echo("Dry run — nothing tagged. Review the report; add --apply to tag.")
    else:
        import json
        baseline = sorted({r.uuid for r in (dup_members + large_recs) if r.favorite})
        with open(FAV_BASELINE_FILE, "w") as f:
            json.dump(baseline, f)
        click.echo(f"  baseline: {len(baseline)} pre-existing favorite(s) recorded")

        def prog(i, n):
            if i % 25 == 0 or i == n:
                click.echo(f"  {i}/{n}")
        try:
            t = apply_mod.add_keyword(sorted(to_tag), apply_mod.KW_VIDEO, apply=True, progress=prog)
            fav = apply_mod.favorite(sorted(to_fav), apply=True, progress=prog)
        except Exception as e:
            _hint_automation(e)
            sys.exit(1)
        click.echo(f"  tagged {t.tagged} cleanup:video, favorited {fav.favorited} keepers"
                   f"+large (already {fav.skipped}), errors {t.errors + fav.errors}")
        click.echo("Review Smart Album [Keyword is cleanup:video]: extra takes are un-♥ "
                   "(delete candidates); keepers + large videos are ♥. Un-♥ any large one "
                   "you want to drop, then delete [cleanup:video AND Favorite is No].")
    if open_report:
        _os.system(f'open "{out}"')


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


@cli.command(name="mark-reviewed")
@click.option("--uuids-file", default=RESCUE_FILE, show_default=True,
              help="Mark the photos listed in this JSON file (e.g. finalize keepers).")
@click.option("--since", default=None, help="Instead: mark every photo on/after YYYY-MM-DD.")
@click.option("--until", default=None, help="...and on/before YYYY-MM-DD (lock a whole event).")
@click.option("--cache", default=DEFAULT_CACHE, show_default=True)
@click.option("--apply", "do_apply", is_flag=True, help="Actually write the reviewed:keep tag.")
def mark_reviewed(uuids_file, since, until, cache, do_apply):
    """Tag photos `reviewed:keep` — permanently excluded from future review.
    Use --uuids-file for a finalize's keepers, or --since/--until to lock an event."""
    import json
    if since or until:
        uuids = [r.uuid for r in _filter_by_date(load_records(cache), since, until)]
        src = f"{since or '…'} → {until or '…'}"
    else:
        with open(uuids_file) as f:
            uuids = json.load(f)
        src = uuids_file
    mode = "APPLY" if do_apply else "DRY RUN"
    click.echo(f"[{mode}] marking {len(uuids)} photos {apply_mod.KW_REVIEWED} (from {src})")

    def prog(i, n):
        if i % 50 == 0 or i == n:
            click.echo(f"  {i}/{n}")
    try:
        res = apply_mod.add_keyword(uuids, apply_mod.KW_REVIEWED,
                                    apply=do_apply, progress=prog if do_apply else None)
    except Exception as e:
        _hint_automation(e)
        sys.exit(1)
    verb = "marked" if do_apply else "would mark"
    click.echo(f"  {verb} {res.tagged}, already {res.skipped}, errors {res.errors}")


@cli.command()
def learn():
    """Train the keeper model from your keep/discard choices in past iterations.
    Read-only: reads feedback logs + the current library; updates the local model."""
    import osxphotos
    from .feedback import learn_and_save, learn_expired
    click.echo("Reading library to see which suggestions you kept vs discarded…")
    db = osxphotos.PhotosDB()
    present = {p.uuid for p in db.photos()}

    m = learn_and_save(present)
    if m.get("pairs"):
        base = m.get("baseline_accuracy")
        lift = f" (heuristic alone {base*100:.1f}%)" if base is not None else ""
        click.echo(f"Dedup keeper model: trained on {m['pairs']} keep>discard pairs from "
                   f"{m['bursts']} bursts; reproduces your choices on {m['accuracy']*100:.1f}%"
                   f"{lift}. Within-shoot choice is largely subjective, so the model "
                   f"stays anchored to the heuristic and only nudges.")
    else:
        click.echo("Dedup: no keeper pairs yet — run a dedup --apply iteration first.")

    e = learn_expired(present)
    if e["types"]:
        rates = ", ".join(f"{k} {e['keep_rate'][k]*100:.0f}%kept" for k in sorted(e["types"]))
        click.echo(f"Expired layer: per-type keep-rates — {rates}")
        if e["suppressed"]:
            click.echo(f"  learned to STOP flagging (you keep these): {', '.join(e['suppressed'])}")
    else:
        click.echo("Expired: no flagged history yet — run an expired --apply iteration first.")


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
