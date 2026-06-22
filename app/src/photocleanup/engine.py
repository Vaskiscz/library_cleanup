"""Backend engine: a thin orchestration layer over the reused photo_cleanup
package. Mirrors the CLI's flows but returns plain dicts the service can
serialise, caches per-layer candidate payloads from an `analyze` pass, and keeps
an in-memory uuid->Record index for thumbnails.

No curation logic lives here — it all stays in photo_cleanup/.
"""
from __future__ import annotations

import io
import os
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Optional

from photo_cleanup.apply import KW_REVIEWED
from photo_cleanup.model import Config, Record

# Reuse the CLI's caches so the app inherits already-computed records/embeddings.
DEFAULT_CACHE = os.path.expanduser("~/.cache/photo-cleanup/records.json")
DEFAULT_EMB_CACHE = os.path.expanduser("~/.cache/photo-cleanup/embeddings.npz")

ALL_LAYERS = ("dedup", "videos", "screenshots", "expired")
# Group-based layers (keep best of a set) vs flat layers (all flagged to remove).
GROUPED_LAYERS = ("dedup", "videos")


def _filter_by_date(records, since: Optional[str], until: Optional[str]):
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


def _date_label(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%b %-d, %Y")
    except Exception:
        return ""


def _item_bytes(rec: Record) -> int:
    """Best-effort reclaimable size for a record (videos: real file; photos:
    original file, else a rough estimate from dimensions for iCloud-only assets)."""
    if rec.is_movie:
        from photo_cleanup.video import video_size
        return video_size(rec)
    if rec.path:
        try:
            return os.path.getsize(rec.path)
        except OSError:
            pass
    return int((rec.width or 0) * (rec.height or 0) * 0.20)  # ~HEIC bytes/pixel


class Engine:
    def __init__(self, cfg: Optional[Config] = None,
                 cache: str = DEFAULT_CACHE, emb_cache: str = DEFAULT_EMB_CACHE,
                 dbpath: Optional[str] = None):
        self.cfg = cfg or Config()
        self.cache = cache
        self.emb_cache = emb_cache
        self.dbpath = dbpath
        self._index: dict[str, Record] = {}
        self._candidates: dict[str, list] = {}   # layer -> payload (from analyze)
        self._video_count: Optional[int] = None
        # Rendered thumbnails/previews live ONLY here, in RAM — never written to
        # disk. Bounded LRU (evicts oldest past the byte budget); cleared on quit.
        self._thumb_cache: "OrderedDict[tuple[str, int], bytes]" = OrderedDict()
        self._thumb_lock = threading.Lock()
        self._thumb_used = 0
        self._thumb_budget = 192 * 1024 * 1024
        self._warming = False

    # ---- record loading ----------------------------------------------------
    def load_records(self, since=None, until=None, excluded: Optional[set] = None,
                     force_rescan: bool = False):
        """Scoped, review-eligible photos. Excludes the Hidden album, anything the
        library marks reviewed:keep, and (app-owned) any uuid in `excluded`."""
        from photo_cleanup.scan import ensure_records
        excluded = excluded or set()
        recs = _filter_by_date(ensure_records(self.cache, self.dbpath, force=force_rescan),
                               since, until)
        recs = [r for r in recs
                if not r.is_hidden
                and KW_REVIEWED not in (r.keywords or [])
                and r.uuid not in excluded]
        for r in recs:
            self._index[r.uuid] = r
        return recs

    def load_videos(self, since=None, until=None, excluded: Optional[set] = None):
        """Scoped, review-eligible videos (same exclusions as photos)."""
        from photo_cleanup.scan import scan_library
        excluded = excluded or set()
        recs = _filter_by_date(scan_library(self.dbpath, movies_only=True), since, until)
        recs = [r for r in recs
                if KW_REVIEWED not in (r.keywords or [])
                and r.uuid not in excluded
                and r.path and os.path.exists(r.path)]
        for r in recs:
            self._index[r.uuid] = r
        return recs

    def record(self, uuid: str) -> Optional[Record]:
        return self._index.get(uuid)

    # ---- candidate builders (delegate to photo_cleanup) --------------------
    def dedup_groups(self, records, progress=None):
        from photo_cleanup.cluster import find_duplicate_groups, time_gps_clusters
        from photo_cleanup.embedding import EmbeddingCache, embed_records
        from photo_cleanup.feedback import inject_face_quality
        cand = [r for c in time_gps_clusters(records, self.cfg) if len(c) >= 2 for r in c]
        ec = EmbeddingCache(self.emb_cache)
        emb_prog = (lambda i, n: progress("Comparing photos", i, n)) if progress else None
        if embed_records(cand, ec, progress=emb_prog):
            ec.save()
        inject_face_quality(cand)
        return find_duplicate_groups(records, self.cfg, embeddings=ec)

    def video_groups(self, videos, progress=None):
        from photo_cleanup.embedding import EmbeddingCache, embed_records
        from photo_cleanup.video import duplicate_takes
        ec = EmbeddingCache(self.emb_cache)
        emb_prog = (lambda i, n: progress("Comparing videos", i, n)) if progress else None
        if embed_records(videos, ec, progress=emb_prog):
            ec.save()
        return duplicate_takes(videos, ec, self.cfg)

    def screenshot_items(self, records):
        from photo_cleanup.screenshots import classify_screenshot
        return [(r, v) for r in records
                if (v := classify_screenshot(r, self.cfg)).is_work]

    def expired_items(self, records):
        from photo_cleanup.expired import classify_expired
        return [(r, v) for r in records
                if (v := classify_expired(r, self.cfg)).is_expired]

    # ---- serialisation -----------------------------------------------------
    def photo_dict(self, rec: Record, *, suggested_keep: bool, subtitle: str = "") -> dict:
        from photo_cleanup.quality import keeper_score
        b = _item_bytes(rec)
        return {
            "uuid": rec.uuid,
            "filename": rec.original_filename,
            "width": rec.width,
            "height": rec.height,
            "favorite": rec.favorite,
            "is_video": rec.is_movie,
            "duration": rec.duration,
            "suggested_keep": suggested_keep,
            "score": round(keeper_score(rec, self.cfg), 3),
            "focus": rec.laplacian,
            "timestamp": rec.timestamp,
            "subtitle": subtitle,
            "bytes": b,
            "size_mb": round(b / (1024 * 1024), 1),
            "thumb": f"/api/thumb/{rec.uuid}",
        }

    def _group_payload(self, groups) -> list[dict]:
        """Shared shape for grouped layers (dedup, videos)."""
        out = []
        for g in sorted(groups, key=lambda g: g.size, reverse=True):
            # Show the whole burst in capture order — keepers aren't floated to the
            # front, just marked — so neighbouring frames sit side by side.
            members = sorted(g.keepers + g.discards, key=lambda r: r.timestamp or 0)
            keep_ids = {r.uuid for r in g.keepers}
            ts = min((r.timestamp for r in members if r.timestamp), default=None)
            out.append({
                "group_key": min((r.uuid for r in members), default=""),
                "title": _date_label(ts) or "Group",
                "date_label": _date_label(ts),
                "size": g.size,
                "suggested_keep": len(g.keepers),
                "suggested_discard": len(g.discards),
                "photos": [self.photo_dict(r, suggested_keep=r.uuid in keep_ids)
                           for r in members],
            })
        return out

    dedup_payload = _group_payload  # alias: same shape
    video_payload = _group_payload

    def _flat_payload(self, items, key: str, title: str, snippet_len: int) -> list[dict]:
        """Shared shape for flat layers (screenshots, expired): all flagged remove."""
        photos = []
        for rec, verdict in items:
            snippet = (rec.detected_text or "").strip().replace("\n", " ")[:snippet_len]
            sub = " · ".join(verdict.reasons) + (f" — “{snippet}…”" if snippet else "")
            photos.append(self.photo_dict(rec, suggested_keep=False, subtitle=sub))
        if not photos:
            return []
        return [{"group_key": key, "title": title, "date_label": "",
                 "size": len(photos), "suggested_keep": 0,
                 "suggested_discard": len(photos), "photos": photos}]

    def screenshot_payload(self, items):
        return self._flat_payload(items, "screenshots", "Work screenshots", 120)

    def expired_payload(self, items):
        return self._flat_payload(items, "expired", "Expired utility photos", 90)

    # ---- analyze (the heavy pass) + summary --------------------------------
    def analyze(self, since=None, until=None, layers=None, excluded: Optional[set] = None,
                progress=None):
        """Compute candidates for each requested layer, cache the payloads, and
        return a per-layer summary (counts + reclaimable bytes) for the picker.

        `progress(message, done, total)` is called throughout so the UI can show
        the access request, library connection, and counted/total processing.
        """
        from photo_cleanup.cluster import find_duplicate_groups, time_gps_clusters
        from photo_cleanup.embedding import EmbeddingCache, embed_records
        from photo_cleanup.expired import classify_expired
        from photo_cleanup.feedback import inject_face_quality
        from photo_cleanup.screenshots import classify_screenshot
        from photo_cleanup.video import duplicate_takes

        layers = [l for l in (layers or ALL_LAYERS) if l in ALL_LAYERS]
        excluded = excluded or set()
        self._candidates = {}
        photo_layers = [l for l in layers if l in ("dedup", "screenshots", "expired")]

        # The on-screen count is eligible items scanned (photos + videos). The bar
        # is driven by a larger internal "work" budget so it keeps moving through the
        # heavy post-passes (faces, grouping, video takes) without inflating the count.
        scanned = 0      # eligible items processed  -> the "X / Y" number
        work = 0.0       # internal work units       -> the bar fill
        items_total = 0
        work_total = 0

        def emit(msg):
            if not progress:
                return
            progress(msg,
                     min(scanned, items_total) if items_total else None,
                     items_total or None,
                     (min(work, work_total) / work_total) if work_total else None)

        # 1) privileges — surface the Photos access request up front
        emit("Requesting photo access…")
        try:
            from .delete import ensure_access
            ensure_access()
        except Exception:
            pass

        # 2) connect + load the scope
        emit("Connecting to your photo library…")
        photos = self.load_records(since, until, excluded=excluded) if photo_layers else []
        videos = self.load_videos(since, until, excluded=excluded) if "videos" in layers else []
        dedup_on, videos_on = "dedup" in layers, "videos" in layers
        nphotos, nvideos = len(photos), len(videos)

        # dedup candidates (photos in a multi-shot time/GPS cluster)
        ec = EmbeddingCache(self.emb_cache)
        cand = set()
        if dedup_on:
            for c in time_gps_clusters(photos, self.cfg):
                if len(c) >= 2:
                    cand.update(r.uuid for r in c)
        cand_n = len(cand)

        items_total = nphotos + nvideos                       # the count = eligible photos + videos
        work_total = (nphotos + nvideos
                      + (2 * cand_n if dedup_on else 0)        # face-quality + photo grouping
                      + (nvideos if videos_on else 0))         # video takes
        emit(f"Found {nphotos:,} photos and {nvideos:,} videos")

        def work_phase(units, msg):
            """Return a progress(i, n) callback advancing the bar (not the count) by
            `units` as a post-pass reports 0..1."""
            base = work
            def cb(i, n):
                nonlocal work
                work = base + (units * (i / n) if n else units)
                emit(msg)
            return base, cb

        # 3) scan every photo: embed dedup candidates + classify (advances count + bar)
        shots, exp, cand_recs = [], [], []
        for rec in photos:
            if rec.uuid in cand:
                embed_records([rec], ec)
                cand_recs.append(rec)
            if "screenshots" in layers and (sv := classify_screenshot(rec, self.cfg)).is_work:
                shots.append((rec, sv))
            if "expired" in layers and (ev := classify_expired(rec, self.cfg)).is_expired:
                exp.append((rec, ev))
            scanned += 1; work += 1
            if scanned % 50 == 0 or scanned == nphotos:
                emit("Analyzing photos…")

        # 4) scan every video (poster-frame embed) — finishes the item count
        if videos_on:
            for k, rec in enumerate(videos, 1):
                embed_records([rec], ec)
                scanned += 1; work += 1
                if k % 20 == 0 or k == nvideos:
                    emit("Analyzing videos…")
        ec.save()

        # 5) post-passes: every item is scanned now, so only the bar advances
        if dedup_on:
            base, cb = work_phase(cand_n, "Checking faces…"); emit("Checking faces…")
            inject_face_quality(cand_recs, progress=cb); work = base + cand_n
            base, cb = work_phase(cand_n, "Grouping photoshoots…"); emit("Grouping photoshoots…")
            groups = find_duplicate_groups(photos, self.cfg, embeddings=ec, progress=cb); work = base + cand_n
            self._candidates["dedup"] = self.dedup_payload(groups)
        if "screenshots" in layers:
            self._candidates["screenshots"] = self.screenshot_payload(shots)
        if "expired" in layers:
            self._candidates["expired"] = self.expired_payload(exp)
        if videos_on:
            base, cb = work_phase(nvideos, "Comparing video takes…"); emit("Comparing video takes…")
            vgroups = duplicate_takes(videos, ec, self.cfg, progress=cb); work = base + nvideos
            self._candidates["videos"] = self.video_payload(vgroups)

        scanned, work = items_total, work_total
        emit("Finishing up…")
        return {"since": since, "until": until,
                "summary": {l: self._summarize(self._candidates.get(l, []), grouped=(l in ("dedup", "videos")))
                            for l in layers}}

    @staticmethod
    def _summarize(payload, grouped: bool = True) -> dict:
        """Counts + reclaimable bytes for the picker, plus a per-month histogram
        (`months`) the UI uses to filter by time period without a re-scan.

        Grouped layers (dedup/videos) bucket a whole cluster into its date; flat
        layers (screenshots/expired) bucket each item by its own date."""
        removable = [p for g in payload for p in g["photos"] if not p["suggested_keep"]]
        months: dict[str, dict] = {}

        def bucket(m, items, rbytes, groups):
            if not m:
                return
            e = months.setdefault(m, {"items": 0, "bytes": 0, "groups": 0})
            e["items"] += items
            e["bytes"] += rbytes
            e["groups"] += groups

        def month_of(ts):
            return datetime.fromtimestamp(ts).strftime("%Y-%m") if ts else None

        if grouped:
            for g in payload:
                ts = min((p["timestamp"] for p in g["photos"] if p.get("timestamp")), default=None)
                rbytes = sum(p["bytes"] for p in g["photos"] if not p["suggested_keep"])
                bucket(month_of(ts), len(g["photos"]), rbytes, 1)
        else:
            for p in (q for g in payload for q in g["photos"]):
                rbytes = p["bytes"] if not p["suggested_keep"] else 0
                bucket(month_of(p.get("timestamp")), 1, rbytes, 0)

        return {
            "groups": len(payload),
            "items": sum(len(g["photos"]) for g in payload),
            "removable": len(removable),
            "reclaimable_bytes": sum(p["bytes"] for p in removable),
            "months": [{"m": k, **v} for k, v in sorted(months.items())],
        }

    def candidates(self, layer, since=None, until=None, excluded: Optional[set] = None):
        """Return cached candidates for a layer; compute that one layer if the
        analyze pass didn't cover it."""
        if layer in self._candidates:
            return self._candidates[layer]
        excluded = excluded or set()
        if layer == "videos":
            pl = self.video_payload(self.video_groups(self.load_videos(since, until, excluded)))
        else:
            photos = self.load_records(since, until, excluded=excluded)
            if layer == "dedup":
                pl = self.dedup_payload(self.dedup_groups(photos))
            elif layer == "screenshots":
                pl = self.screenshot_payload(self.screenshot_items(photos))
            elif layer == "expired":
                pl = self.expired_payload(self.expired_items(photos))
            else:
                raise ValueError(f"unknown layer {layer!r}")
        self._candidates[layer] = pl
        return pl

    def all_items(self, since=None, until=None, excluded: Optional[set] = None) -> list[dict]:
        """Every eligible photo + video in range as one chronological feed, all
        suggested to keep — for the manual review flow. Same exclusions as the scan
        (Hidden, reviewed:keep, app-excluded)."""
        excluded = excluded or set()
        recs = self.load_records(since, until, excluded=excluded) + \
               self.load_videos(since, until, excluded=excluded)
        recs.sort(key=lambda r: r.timestamp or 0)
        photos = [self.photo_dict(r, suggested_keep=True) for r in recs]
        return [{"group_key": "all", "title": "All photos & videos", "date_label": "",
                 "size": len(photos), "suggested_keep": len(photos),
                 "suggested_discard": 0, "photos": photos}]

    def library_stats(self) -> dict:
        """Cheap-ish library totals for the status line (videos counted once)."""
        from photo_cleanup.scan import ensure_records, scan_library
        photos = len(ensure_records(self.cache, self.dbpath))
        if self._video_count is None:
            self._video_count = len(scan_library(self.dbpath, movies_only=True))
        return {"photos": photos, "videos": self._video_count}

    # ---- thumbnails --------------------------------------------------------
    def thumb_bytes(self, uuid: str, px: int = 240) -> Optional[bytes]:
        """Rendered bytes for a uuid at a target size, served from an in-memory
        LRU so a given thumb/preview is only ever rendered once. Nothing is
        written to disk — the cache lives in RAM and dies with the process."""
        key = (uuid, px)
        with self._thumb_lock:
            hit = self._thumb_cache.get(key)
            if hit is not None:
                self._thumb_cache.move_to_end(key)
                return hit
        data = self._render_thumb(uuid, px)   # render outside the lock (PIL is slow)
        if data is not None:
            with self._thumb_lock:
                if key not in self._thumb_cache:
                    self._thumb_cache[key] = data
                    self._thumb_used += len(data)
                    while self._thumb_used > self._thumb_budget and len(self._thumb_cache) > 1:
                        _, old = self._thumb_cache.popitem(last=False)
                        self._thumb_used -= len(old)
        return data

    def _render_thumb(self, uuid: str, px: int) -> Optional[bytes]:
        rec = self._index.get(uuid)
        if rec is None:
            return None
        try:
            from PIL import Image, ImageOps
        except Exception:
            return None
        candidates = list(rec.derivatives)
        if rec.path:
            candidates.append(rec.path)
        # Grid thumbs (small px) take the smallest source — fastest. The detail
        # preview (large px) takes the largest/original so fine differences between
        # near-identical shots actually survive; `thumbnail()` only downscales, so a
        # small source could never produce a sharp preview. Higher JPEG quality too.
        hi = px > 512
        order = sorted(set(candidates), key=_size, reverse=hi)
        quality = 90 if hi else 72
        for p in order:
            try:
                with Image.open(p) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")  # honour camera rotation
                    im.thumbnail((px, px))
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=quality)
                return buf.getvalue()
            except Exception:
                continue
        return None

    def warm_thumbnails(self, px: int = 240) -> None:
        """Pre-render grid thumbs for every current candidate into the RAM cache,
        so Review scrolls instantly (like Photos' pre-baked thumbnails). Runs in a
        background thread; one at a time so it never blocks request threads for
        long. Safe to call repeatedly — already-cached items are skipped."""
        if self._warming:
            return
        self._warming = True
        try:
            seen = set()
            for payload in list(self._candidates.values()):
                for g in payload:
                    for p in g["photos"]:
                        u = p["uuid"]
                        if u in seen:
                            continue
                        seen.add(u)
                        with self._thumb_lock:
                            if (u, px) in self._thumb_cache:
                                continue
                        self.thumb_bytes(u, px=px)
        finally:
            self._warming = False


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 1 << 62
