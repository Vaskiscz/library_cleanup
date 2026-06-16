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


def keepers_for_size(size: int, cfg: Config) -> int:
    """Adaptive keeper count: bigger burst => you cared more => keep more."""
    for max_size, n in cfg.keeper_tiers:
        if size <= max_size:
            return n
    return cfg.keepers_max


def select_keepers(group: list[Record], cfg: Config, embeddings=None) -> DuplicateGroup:
    """Keep the best, then add more keepers only if they are both high quality
    AND visibly different from the ones already kept (diversity), up to an
    adaptive cap based on burst size. A singleton yields no discards.

    Diversity needs embeddings; without them it falls back to fixed top-N."""
    if len(group) < 2:
        return DuplicateGroup(keepers=list(group), discards=[])

    for r in group:
        measure_sharpness(r)  # fills rec.laplacian for ranking + report

    ranked = sorted(group, key=lambda r: keeper_score(r, cfg), reverse=True)
    n_keep = min(keepers_for_size(len(group), cfg), len(ranked) - 1)
    n_keep = max(1, n_keep)

    if embeddings is None:
        keepers = ranked[:n_keep]
    else:
        from .embedding import distance
        # Quality gate: only consider the "nicer" frames (>= median score) so we
        # never keep a blurry outlier just because it's visually different.
        scores = sorted(keeper_score(r, cfg) for r in ranked)
        median = scores[len(scores) // 2]
        eligible = [r for r in ranked if keeper_score(r, cfg) >= median] or ranked

        # Farthest-point selection: seed with the best quality frame, then keep
        # adding the frame MOST different from those already chosen — but only
        # while it clears the diversity floor. Spreads keepers across the range
        # of expressions/poses; a near-identical burst yields just one keeper.
        keepers = [ranked[0]]
        while len(keepers) < n_keep:
            best_c, best_d = None, -1.0
            for c in eligible:
                cv = embeddings.get(c.uuid)
                if cv is None or c in keepers:
                    continue
                dmin = min(distance(cv, embeddings.get(k.uuid)) for k in keepers
                           if embeddings.get(k.uuid) is not None)
                if dmin > best_d:
                    best_d, best_c = dmin, c
            if best_c is None or best_d < cfg.keeper_diversity_min:
                break
            keepers.append(best_c)

    kept_ids = {r.uuid for r in keepers}
    discards = [r for r in ranked if r.uuid not in kept_ids]
    # never discard a user Favorite — promote any favorites into the keeper set
    promoted = [r for r in discards if r.favorite]
    if promoted:
        discards = [r for r in discards if not r.favorite]
        keepers = keepers + promoted
    return DuplicateGroup(keepers=keepers, discards=discards)


def embedding_groups(cluster: list[Record], cache, cfg: Config) -> list[list[Record]]:
    """Group by Apple Vision feature-print distance (content similarity). Photos
    without a cached embedding fall through as singletons (kept)."""
    from .embedding import distance

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
            if distance(vi, cache.get(items[j].uuid)) <= cfg.embedding_max_distance:
                parent[find(i)] = find(j)

    groups: dict[int, list[Record]] = {}
    for idx, r in enumerate(items):
        groups.setdefault(find(idx), []).append(r)
    return list(groups.values())


def leader_dedup(cluster: list[Record], cache, cfg: Config) -> list[DuplicateGroup]:
    """Leader clustering on embeddings: process photos best-quality first; each
    becomes a 'leader' (keeper) unless it is within `embedding_max_distance` of
    an existing leader, in which case it's a near-duplicate follower (discard).

    Guarantees a photo is discarded ONLY if a KEPT photo is within the radius —
    so every genuinely distinct sub-shot keeps its best frame (no chaining bug),
    while runs of near-identical frames collapse to one keeper each."""
    from .embedding import distance

    items = [r for r in cluster if cache.get(r.uuid) is not None]
    for r in items:
        measure_sharpness(r)
    # Process in TIME order so the radius can be relaxed for rapid-fire frames.
    items.sort(key=lambda r: r.timestamp or 0)

    base, relaxed, rapid = (cfg.embedding_max_distance,
                            cfg.rapid_burst_radius, cfg.rapid_burst_seconds)
    leaders: list[dict] = []  # {"repr": Record, "t": float, "members": [Record]}
    for r in items:
        rv = cache.get(r.uuid)
        rt = r.timestamp or 0.0
        chosen, best_d = None, None
        for L in leaders:
            radius = relaxed if abs(rt - L["t"]) <= rapid else base
            d = distance(rv, cache.get(L["repr"].uuid))
            if d <= radius and (best_d is None or d < best_d):
                chosen, best_d = L, d
        if chosen is not None:
            chosen["members"].append(r)
        else:
            leaders.append({"repr": r, "t": rt, "members": [r]})

    groups: list[DuplicateGroup] = []
    for L in leaders:
        members = L["members"]
        if len(members) < 2:
            continue  # unique shot — kept, nothing to discard
        # Keeper = best quality of the group; the rest are near-dup discards.
        ranked = sorted(members, key=lambda r: keeper_score(r, cfg), reverse=True)
        rest = ranked[1:]
        # never discard a user Favorite — promote any favorited members
        promoted = [d for d in rest if d.favorite]
        discards = [d for d in rest if not d.favorite]
        groups.append(DuplicateGroup(keepers=[ranked[0]] + promoted, discards=discards))
    return [g for g in groups if g.discards]


def find_duplicate_groups(records: list[Record], cfg: Config, embeddings=None) -> list[DuplicateGroup]:
    """Full near-duplicate pass -> only groups that actually have discards.

    Uses Vision-embedding leader clustering when an `embeddings` cache is
    provided (preferred); otherwise falls back to perceptual-hash grouping."""
    out: list[DuplicateGroup] = []
    for cluster in time_gps_clusters(records, cfg):
        if len(cluster) < 2:
            continue
        if embeddings is not None:
            out.extend(leader_dedup(cluster, embeddings, cfg))
        else:
            for grp in similar_groups(cluster, cfg):
                dg = select_keepers(grp, cfg)
                if dg.discards:
                    out.append(dg)
    return out
