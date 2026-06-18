"""Video cleanup — two gaps Apple Photos ignores entirely:

  1. Near-duplicate takes — several videos of the same thing shot close together.
     We embed each video's poster frame (Apple Vision feature print, reusing the
     photo pipeline) and group same-scene takes within a time/GPS cluster; the
     largest file (proxy for the longest / most complete take) is the keeper.
  2. Oversized videos — large files worth a deliberate "do I really keep this?".

On-device; the poster-frame derivative is already on disk for every video.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .cluster import DuplicateGroup, time_gps_clusters
from .model import Config, Record
from .quality import _best_image_path


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


def duplicate_takes(records: list[Record], cache, cfg: Config) -> list[DuplicateGroup]:
    """Group same-scene video takes within a time/GPS cluster (by poster-frame
    embedding); keeper = largest file, the rest are extra takes."""
    from .embedding import distance
    out: list[DuplicateGroup] = []
    for cluster in time_gps_clusters(records, cfg):
        if len(cluster) < 2:
            continue
        items = [r for r in cluster if cache.get(r.uuid) is not None]
        n = len(items)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            vi = cache.get(items[i].uuid)
            for j in range(i + 1, n):
                if distance(vi, cache.get(items[j].uuid)) <= cfg.video_dup_distance:
                    parent[find(i)] = find(j)

        groups: dict[int, list[Record]] = {}
        for idx, r in enumerate(items):
            groups.setdefault(find(idx), []).append(r)

        for grp in groups.values():
            if len(grp) < 2:
                continue
            keeper = max(grp, key=video_size)
            discards = [r for r in grp if r.uuid != keeper.uuid]
            # never discard a favorite
            promoted = [d for d in discards if d.favorite]
            discards = [d for d in discards if not d.favorite]
            out.append(DuplicateGroup(keepers=[keeper] + promoted, discards=discards))
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
