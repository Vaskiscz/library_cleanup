"""Backend engine: a thin orchestration layer over the reused photo_cleanup
package. Mirrors the CLI's dedup/screenshot flow but returns plain dicts the
service can serialise, and keeps an in-memory uuid->Record index for thumbnails.

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


class Engine:
    def __init__(self, cfg: Optional[Config] = None,
                 cache: str = DEFAULT_CACHE, emb_cache: str = DEFAULT_EMB_CACHE,
                 dbpath: Optional[str] = None):
        self.cfg = cfg or Config()
        self.cache = cache
        self.emb_cache = emb_cache
        self.dbpath = dbpath
        self._index: dict[str, Record] = {}

    # ---- record loading ----------------------------------------------------
    def load_records(self, since=None, until=None, excluded: Optional[set] = None,
                     force_rescan: bool = False):
        """Scoped, review-eligible records. Excludes the Hidden album, anything
        the library marks reviewed:keep, and (app-owned) any uuid in `excluded`."""
        from photo_cleanup.scan import ensure_records
        excluded = excluded or set()
        recs = _filter_by_date(ensure_records(self.cache, self.dbpath, force=force_rescan),
                               since, until)
        recs = [r for r in recs
                if not r.is_hidden
                and KW_REVIEWED not in (r.keywords or [])
                and r.uuid not in excluded]
        # refresh the thumbnail index with whatever is in scope
        for r in recs:
            self._index[r.uuid] = r
        return recs

    def record(self, uuid: str) -> Optional[Record]:
        return self._index.get(uuid)

    # ---- candidate builders ------------------------------------------------
    def dedup_groups(self, records):
        """Near-duplicate photoshoot groups (embeds missing candidates on the fly)."""
        from photo_cleanup.cluster import find_duplicate_groups, time_gps_clusters
        from photo_cleanup.embedding import EmbeddingCache, embed_records
        from photo_cleanup.feedback import inject_face_quality

        cand = [r for c in time_gps_clusters(records, self.cfg) if len(c) >= 2 for r in c]
        ec = EmbeddingCache(self.emb_cache)
        if embed_records(cand, ec):
            ec.save()
        inject_face_quality(cand)
        return find_duplicate_groups(records, self.cfg, embeddings=ec)

    def screenshot_items(self, records):
        """[(rec, verdict)] for high-confidence work screenshots."""
        from photo_cleanup.screenshots import classify_screenshot
        out = []
        for rec in records:
            v = classify_screenshot(rec, self.cfg)
            if v.is_work:
                out.append((rec, v))
        return out

    # ---- serialisation -----------------------------------------------------
    def photo_dict(self, rec: Record, *, suggested_keep: bool, subtitle: str = "") -> dict:
        from photo_cleanup.quality import keeper_score
        return {
            "uuid": rec.uuid,
            "filename": rec.original_filename,
            "width": rec.width,
            "height": rec.height,
            "favorite": rec.favorite,
            "suggested_keep": suggested_keep,
            "score": round(keeper_score(rec, self.cfg), 3),
            "focus": rec.laplacian,
            "timestamp": rec.timestamp,
            "subtitle": subtitle,
            "thumb": f"/api/thumb/{rec.uuid}",
        }

    def dedup_payload(self, groups) -> list[dict]:
        out = []
        for g in sorted(groups, key=lambda g: g.size, reverse=True):
            members = sorted(g.keepers, key=lambda r: r.timestamp or 0) + \
                      sorted(g.discards, key=lambda r: r.timestamp or 0)
            keep_ids = {r.uuid for r in g.keepers}
            group_key = min((r.uuid for r in members), default="")
            out.append({
                "group_key": group_key,
                "size": g.size,
                "suggested_keep": len(g.keepers),
                "suggested_discard": len(g.discards),
                "photos": [self.photo_dict(r, suggested_keep=r.uuid in keep_ids)
                           for r in members],
            })
        return out

    def screenshot_payload(self, items) -> list[dict]:
        # one pseudo-group so the FE renders it uniformly; all suggested discard
        photos = []
        for rec, verdict in items:
            snippet = (rec.detected_text or "").strip().replace("\n", " ")[:120]
            sub = " · ".join(verdict.reasons) + (f" — “{snippet}…”" if snippet else "")
            photos.append(self.photo_dict(rec, suggested_keep=False, subtitle=sub))
        return [{"group_key": "screenshots", "size": len(photos),
                 "suggested_keep": 0, "suggested_discard": len(photos),
                 "photos": photos}] if photos else []

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
