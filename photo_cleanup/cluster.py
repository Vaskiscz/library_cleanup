"""Cluster shots from the same moment/place, confirm near-duplicates by
Vision feature-print (embedding) distance, and pick the best 1..N keepers
per similar group.

Keep-bias guarantees:
  * A photo alone in its similar group (a one-of-a-kind image) is NEVER flagged.
  * Every similar group keeps at least one (the best) shot, even if all are soft.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .model import Config, Record
from .quality import keeper_score, measure_sharpness


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

    sc = {r.uuid: keeper_score(r, cfg) for r in group}   # compute once per photo
    ranked = sorted(group, key=lambda r: sc[r.uuid], reverse=True)
    n_keep = min(keepers_for_size(len(group), cfg), len(ranked) - 1)
    n_keep = max(1, n_keep)

    if embeddings is None:
        keepers = ranked[:n_keep]
    else:
        from .embedding import distance
        # Quality gate: drop only the worst ~30% so a blurry/accidental outlier
        # isn't kept just for being "different", while still letting genuinely
        # distinct-but-slightly-softer moments (e.g. a candid) qualify.
        scores = sorted(sc.values())
        qfloor = scores[int(len(scores) * 0.3)]
        eligible = [r for r in ranked if sc[r.uuid] >= qfloor] or ranked

        # Adaptive diversity floor: a fixed floor under-keeps in a *tight* burst,
        # where even the most different frames sit below it — scale the floor to
        # the burst's own embedding spread (fraction of the median pairwise
        # distance), bounded so it never admits true duplicates and never
        # exceeds the configured floor for a spread-out session.
        div_min = cfg.keeper_diversity_min
        vecs = [v for v in (embeddings.get(r.uuid) for r in eligible) if v is not None]
        if len(vecs) >= 3:
            ds = sorted(distance(vecs[i], vecs[j])
                        for i in range(len(vecs)) for j in range(i + 1, len(vecs)))
            median = ds[len(ds) // 2]
            div_min = max(cfg.keeper_diversity_abs_min,
                          min(cfg.keeper_diversity_min, 0.6 * median))

        # Farthest-point selection: seed with the best quality frame, then keep
        # adding the frame MOST different from those already chosen — but only
        # while it clears the diversity floor. Spreads keepers across the range
        # of expressions/poses; a near-identical burst yields just one keeper.
        # Seed with the best-quality frame that ACTUALLY has an embedding — if the
        # top-ranked frame's Vision embedding failed, seeding with it would leave
        # the diversity loop's min() over an empty set and crash the whole scan
        # (audit #5). Fall back to ranked[0] so a cluster with zero embeddings
        # still keeps one.
        seed = next((r for r in ranked if embeddings.get(r.uuid) is not None), ranked[0])
        keepers = [seed]
        while len(keepers) < n_keep:
            best_c, best_d = None, -1.0
            for c in eligible:
                cv = embeddings.get(c.uuid)
                if cv is None or c in keepers:
                    continue
                kvecs = [kv for k in keepers if (kv := embeddings.get(k.uuid)) is not None]
                dmin = min((distance(cv, kv) for kv in kvecs), default=float("inf"))
                if dmin > best_d:
                    best_d, best_c = dmin, c
            if best_c is None or best_d < div_min:
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


def find_duplicate_groups(records: list[Record], cfg: Config, embeddings=None,
                          progress=None) -> list[DuplicateGroup]:
    """Full near-duplicate pass -> only groups that actually have discards.

    Requires a Vision-embedding cache (the only similarity method). Without one
    (embeddings is None) there's nothing to compare on, so no groups are returned.
    `progress(done, total)` is called per processed cluster (in photos) so callers
    can show a moving bar instead of freezing through a long grouping pass."""
    if embeddings is None:
        return []
    out: list[DuplicateGroup] = []
    multi = [c for c in time_gps_clusters(records, cfg) if len(c) >= 2]
    total = sum(len(c) for c in multi) or 1
    done = 0
    for cluster in multi:
        # Treat the whole session as one group; keep the best, most-diverse 1-N
        # (farthest-point, adaptive cap), discard the rest of the shoot.
        dg = select_keepers(cluster, cfg, embeddings=embeddings)
        if dg.discards:
            out.append(dg)
        done += len(cluster)
        if progress:
            progress(done, total)
    return out
