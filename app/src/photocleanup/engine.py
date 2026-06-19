"""Backend engine: a thin orchestration layer over the reused photo_cleanup
package. Mirrors the CLI's flows but returns plain dicts the service can
serialise, caches per-layer candidate payloads from an `analyze` pass, and keeps
an in-memory uuid->Record index for thumbnails.

No curation logic lives here — it all stays in photo_cleanup/.
"""
from __future__ import annotations

import io
import os
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
    def dedup_groups(self, records):
        from photo_cleanup.cluster import find_duplicate_groups, time_gps_clusters
        from photo_cleanup.embedding import EmbeddingCache, embed_records
        from photo_cleanup.feedback import inject_face_quality
        cand = [r for c in time_gps_clusters(records, self.cfg) if len(c) >= 2 for r in c]
        ec = EmbeddingCache(self.emb_cache)
        if embed_records(cand, ec):
            ec.save()
        inject_face_quality(cand)
        return find_duplicate_groups(records, self.cfg, embeddings=ec)

    def video_groups(self, videos):
        from photo_cleanup.embedding import EmbeddingCache, embed_records
        from photo_cleanup.video import duplicate_takes
        ec = EmbeddingCache(self.emb_cache)
        if embed_records(videos, ec):
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
            members = (sorted(g.keepers, key=lambda r: r.timestamp or 0)
                       + sorted(g.discards, key=lambda r: r.timestamp or 0))
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
    def analyze(self, since=None, until=None, layers=None, excluded: Optional[set] = None):
        """Compute candidates for each requested layer, cache the payloads, and
        return a per-layer summary (counts + reclaimable bytes) for the picker."""
        layers = [l for l in (layers or ALL_LAYERS) if l in ALL_LAYERS]
        excluded = excluded or set()
        self._candidates = {}
        summary = {}

        if any(l in layers for l in ("dedup", "screenshots", "expired")):
            photos = self.load_records(since, until, excluded=excluded)
            builders = {
                "dedup": lambda: self.dedup_payload(self.dedup_groups(photos)),
                "screenshots": lambda: self.screenshot_payload(self.screenshot_items(photos)),
                "expired": lambda: self.expired_payload(self.expired_items(photos)),
            }
            for layer in ("dedup", "screenshots", "expired"):
                if layer in layers:
                    self._candidates[layer] = builders[layer]()
        if "videos" in layers:
            vids = self.load_videos(since, until, excluded=excluded)
            self._candidates["videos"] = self.video_payload(self.video_groups(vids))

        for layer in layers:
            summary[layer] = self._summarize(self._candidates.get(layer, []))
        return {"since": since, "until": until, "summary": summary}

    @staticmethod
    def _summarize(payload) -> dict:
        removable = [p for g in payload for p in g["photos"] if not p["suggested_keep"]]
        return {
            "groups": len(payload),
            "items": sum(len(g["photos"]) for g in payload),
            "removable": len(removable),
            "reclaimable_bytes": sum(p["bytes"] for p in removable),
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

    def library_stats(self) -> dict:
        """Cheap-ish library totals for the status line (videos counted once)."""
        from photo_cleanup.scan import ensure_records, scan_library
        photos = len(ensure_records(self.cache, self.dbpath))
        if self._video_count is None:
            self._video_count = len(scan_library(self.dbpath, movies_only=True))
        return {"photos": photos, "videos": self._video_count}

    # ---- thumbnails --------------------------------------------------------
    def thumb_bytes(self, uuid: str, px: int = 240) -> Optional[bytes]:
        rec = self._index.get(uuid)
        if rec is None:
            return None
        try:
            from PIL import Image
        except Exception:
            return None
        candidates = list(rec.derivatives)
        if rec.path:
            candidates.append(rec.path)
        for p in sorted(candidates, key=_size):  # smallest existing first = fastest
            try:
                with Image.open(p) as im:
                    im = im.convert("RGB")
                    im.thumbnail((px, px))
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=72)
                return buf.getvalue()
            except Exception:
                continue
        return None


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 1 << 62
