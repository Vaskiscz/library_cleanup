"""Cluster shots from the same moment/place, confirm near-duplicates with a
perceptual hash, and pick the best 1..N keepers per similar group.

Keep-bias guarantees:
  * A photo alone in its similar group (a one-of-a-kind image) is NEVER flagged.
  * Every similar group keeps at least one (the best) shot, even if all are soft.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .model import Config, Record
from .quality import keeper_score, measure_sharpness, _fast_image_path


# ---- geo / time clustering -------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _gps_jump(a: Record, b: Record, cfg: Config) -> bool:
    if None in (a.latitude, a.longitude, b.latitude, b.longitude):
        return False  # missing GPS never forces a split (time handles it)
    return haversine_m(a.latitude, a.longitude, b.latitude, b.longitude) > cfg.cluster_gps_meters


def time_gps_clusters(records: list[Record], cfg: Config) -> list[list[Record]]:
    """Greedy sweep: start a new cluster when the time gap or GPS jump is too big."""
    timed = [r for r in records if r.timestamp is not None]
    timed.sort(key=lambda r: r.timestamp)

    clusters: list[list[Record]] = []
    cur: list[Record] = []
    for r in timed:
        if not cur:
            cur = [r]
            continue
        prev = cur[-1]
        gap = r.timestamp - prev.timestamp
        if gap > cfg.cluster_gap_seconds or _gps_jump(prev, r, cfg):
            clusters.append(cur)
            cur = [r]
        else:
            cur.append(r)
    if cur:
        clusters.append(cur)
    return clusters


# ---- perceptual-hash near-duplicate confirmation ---------------------------

def compute_phash(rec: Record, cfg: Config) -> Optional[str]:
    if rec.phash is not None:
        return rec.phash
    p = _fast_image_path(rec)
    if not p:
        return None
    try:
        import imagehash
        from PIL import Image
        with Image.open(p) as im:
            rec.phash = str(imagehash.phash(im, hash_size=cfg.phash_size))
    except Exception:
        rec.phash = None
    return rec.phash


def _hamming(h1: str, h2: str) -> Optional[int]:
    try:
        import imagehash
        return imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2)
    except Exception:
        return None


def similar_groups(cluster: list[Record], cfg: Config) -> list[list[Record]]:
    """Within one time/place cluster, union photos whose perceptual hashes are
    close. Photos without a usable hash stay as singletons (kept)."""
    hashed = []
    for r in cluster:
        if compute_phash(r, cfg) is not None:
            hashed.append(r)

    n = len(hashed)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    for i in range(n):
        for j in range(i + 1, n):
            d = _hamming(hashed[i].phash, hashed[j].phash)
            if d is not None and d <= cfg.phash_max_distance:
                union(i, j)

    groups: dict[int, list[Record]] = {}
    for idx, r in enumerate(hashed):
        groups.setdefault(find(idx), []).append(r)
    return list(groups.values())


# ---- keeper selection ------------------------------------------------------

@dataclass
class DuplicateGroup:
    keepers: list[Record] = field(default_factory=list)
    discards: list[Record] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.keepers) + len(self.discards)


def select_keepers(group: list[Record], cfg: Config) -> DuplicateGroup:
    """Rank a similar group and keep the best 1..N; the rest are discards.
    A singleton yields no discards (one-of-a-kind is always kept)."""
    if len(group) < 2:
        return DuplicateGroup(keepers=list(group), discards=[])

    for r in group:
        measure_sharpness(r)  # fills rec.laplacian for ranking + report

    ranked = sorted(group, key=lambda r: keeper_score(r, cfg), reverse=True)
    n_keep = max(1, min(cfg.keepers_per_group, len(ranked) - 1))
    # never discard a user Favorite — promote any favorites into the keeper set
    keepers = ranked[:n_keep]
    discards = ranked[n_keep:]
    promoted = [r for r in discards if r.favorite]
    if promoted:
        discards = [r for r in discards if not r.favorite]
        keepers = keepers + promoted
    return DuplicateGroup(keepers=keepers, discards=discards)


def find_duplicate_groups(records: list[Record], cfg: Config) -> list[DuplicateGroup]:
    """Full near-duplicate pass -> only groups that actually have discards."""
    out: list[DuplicateGroup] = []
    for cluster in time_gps_clusters(records, cfg):
        if len(cluster) < 2:
            continue
        for grp in similar_groups(cluster, cfg):
            dg = select_keepers(grp, cfg)
            if dg.discards:
                out.append(dg)
    return out
