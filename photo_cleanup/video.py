"""Video cleanup — two gaps Apple Photos ignores entirely:

  1. Near-duplicate takes — several videos of the same thing shot close together.
     We sample start/middle/end frames of each video (AVFoundation grab → Apple
     Vision feature print; poster-frame fallback when sampling isn't possible)
     and group same-scene takes within a time/GPS cluster; the keeper is the
     most-original, best size-to-quality take.
  2. Oversized videos — large files worth a deliberate "do I really keep this?".

On-device; sampled frames live only in memory (vectors are cached, frames not).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .cluster import DuplicateGroup, time_gps_clusters
from .model import Config, Record

# Where in the video to sample comparison frames (fractions of the duration).
FRAME_FRACS = (0.15, 0.5, 0.85)


def video_size(rec: Record) -> int:
    for p in [rec.path] + list(rec.derivatives):
        # prefer the original movie's size; derivatives are just the poster
        if p and p == rec.path:
            try:
                return os.path.getsize(p)
            except OSError:
                pass
    try:
        return os.path.getsize(rec.path) if rec.path else 0
    except OSError:
        return 0


def metadata_richness(rec: Record) -> int:
    """How much original metadata survives — device/AirDrop originals keep GPS +
    camera EXIF; messaging-app re-encodes strip them. Higher = more likely the
    true original (preferred as the keeper)."""
    score = 0
    if rec.latitude is not None and rec.longitude is not None:
        score += 2          # GPS is the strongest signal (stripped by messaging)
    if rec.camera_make:
        score += 1
    if rec.camera_model:
        score += 1
    return score


def quality_per_byte(rec: Record) -> float:
    """Size-to-quality ratio: pixels (resolution) per byte. Higher = better value
    — keeps a crisp-but-lean take over a bloated one of the same scene."""
    size = video_size(rec)
    pixels = (rec.width or 0) * (rec.height or 0)
    if size <= 0:
        return 0.0
    if pixels <= 0:        # no resolution metadata — fall back to favoring smaller
        return 1.0 / size
    return pixels / size


def _frame_keys(uuid: str) -> list[str]:
    return [f"{uuid}#f{i}" for i in range(len(FRAME_FRACS))]


def take_vectors(rec: Record, cache) -> list:
    """Comparison vectors for a video: sampled start/mid/end frames (cached by
    key "uuid#fN", computed on first use), else the poster-frame embedding."""
    from .embedding import sample_video_frame_vectors
    keys = _frame_keys(rec.uuid)
    if all(k in cache for k in keys):
        return [cache.get(k) for k in keys]
    if rec.path and os.path.exists(rec.path):
        vecs = sample_video_frame_vectors(rec.path, FRAME_FRACS)
        if len(vecs) == len(FRAME_FRACS):
            for k, v in zip(keys, vecs, strict=True):
                cache.put(k, v)
            return vecs
    pv = cache.get(rec.uuid)                      # poster-frame fallback
    return [pv] if pv is not None else []


def _take_distance(va: list, vb: list) -> float:
    """Distance between two takes. With full frame sets, compare positionally
    (start↔start, mid↔mid, end↔end) and take the median — same-scene takes
    track each other through the video, while a coincidentally similar opening
    frame alone can't fake a match. Mixed/poster sets fall back to the closest
    cross pair (the old poster behaviour)."""
    from .embedding import distance
    if len(va) == len(vb) and len(va) > 1:
        ds = sorted(distance(a, b) for a, b in zip(va, vb, strict=True))
        return ds[len(ds) // 2]
    return min(distance(a, b) for a in va for b in vb)


def duplicate_takes(records: list[Record], cache, cfg: Config, progress=None,
                    workers: int = 1) -> list[DuplicateGroup]:
    """Group same-scene video takes within a time/GPS cluster (by sampled-frame
    embeddings); keeper = best size-to-quality ratio, the rest are extra takes.
    `progress(done, total)` is called per processed cluster (in videos).

    `workers > 1` samples a cluster's frames in a thread pool (AVFoundation +
    Vision release the GIL; each video's generator/handler is independent).
    Cache writes inside take_vectors go through the cache's own lock."""
    pool = None
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=workers)
    try:
        return _duplicate_takes(records, cache, cfg, progress, pool)
    finally:
        if pool is not None:
            # Cancellation (progress raising) must not block on queued samples.
            pool.shutdown(wait=False, cancel_futures=True)


def _duplicate_takes(records, cache, cfg, progress, pool) -> list[DuplicateGroup]:
    out: list[DuplicateGroup] = []
    multi = [c for c in time_gps_clusters(records, cfg) if len(c) >= 2]
    grp_total = sum(len(c) for c in multi) or 1
    grp_done = 0
    for cluster in multi:
        if pool is not None:
            sampled = pool.map(lambda r: take_vectors(r, cache), cluster)
            vecs = {r.uuid: v for r, v in zip(cluster, sampled, strict=True)}
        else:
            vecs = {r.uuid: take_vectors(r, cache) for r in cluster}
        items = [r for r in cluster if vecs[r.uuid]]
        n = len(items)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            vi = vecs[items[i].uuid]
            for j in range(i + 1, n):
                if _take_distance(vi, vecs[items[j].uuid]) <= cfg.video_dup_distance:
                    parent[find(i)] = find(j)

        groups: dict[int, list[Record]] = {}
        for idx, r in enumerate(items):
            groups.setdefault(find(idx), []).append(r)

        for grp in groups.values():
            if len(grp) < 2:
                continue
            # Keeper: most original metadata first (the true device/AirDrop
            # original over a stripped messaging copy), then best size/quality.
            keeper = max(grp, key=lambda r: (metadata_richness(r), quality_per_byte(r)))
            discards = [r for r in grp if r.uuid != keeper.uuid]
            # never discard a favorite
            promoted = [d for d in discards if d.favorite]
            discards = [d for d in discards if not d.favorite]
            out.append(DuplicateGroup(keepers=[keeper] + promoted, discards=discards))
        grp_done += len(cluster)
        if progress:
            progress(grp_done, grp_total)
    return [g for g in out if g.discards]


@dataclass
class LargeVideo:
    rec: Record
    size: int


def large_videos(records: list[Record], cfg: Config) -> list[LargeVideo]:
    floor = cfg.large_video_mb * 1024 * 1024
    out = [LargeVideo(r, video_size(r)) for r in records]
    out = [lv for lv in out if lv.size >= floor]
    out.sort(key=lambda lv: lv.size, reverse=True)
    return out
