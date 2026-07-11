"""Generate deterministic golden fixtures for the photoshoot clustering +
keeper-selection engine, so a separate Swift port can be tested for EXACT parity.

Everything here is image-free and deterministic. Synthetic Records are built so
that ``keeper_score(rec, cfg) == quality + (1.0 if favorite else 0.0)`` EXACTLY:
empty ``features`` selects the hand-tuned heuristic path, ``score_overall`` is
set to ``quality/2`` (the heuristic multiplies it by 2) with every other
``score_*`` at 0, there is no image on disk so the Laplacian term is skipped,
and ``width/height == 0`` zeroes the resolution tiebreak. The REAL functions
(:func:`time_gps_clusters` then :func:`find_duplicate_groups`) are then run over
these records and whatever they produce is recorded verbatim as ``expected`` —
no expected value is decided by hand.
"""

from __future__ import annotations

import json

import numpy as np

from .cluster import find_duplicate_groups, time_gps_clusters
from .model import Config, Record

SCHEMA_VERSION = 1
GENERATOR = "library-cleanup export-fixtures"

# Prague, used as the base location for GPS-bearing cases.
_LAT, _LON = 50.08, 14.42


def _record(photo: dict) -> Record:
    """Build a synthetic Record whose keeper_score is exactly quality (+1 if fav).

    See the module docstring for why each field is set the way it is.
    """
    return Record(
        uuid=photo["id"],
        original_filename=photo["id"] + ".jpg",
        path=None,
        timestamp=float(photo["t"]),
        latitude=photo.get("lat"),
        longitude=photo.get("lon"),
        width=0,
        height=0,
        is_photo=True,
        is_movie=False,
        is_screenshot=False,
        is_hidden=False,
        in_burst=False,
        favorite=bool(photo.get("favorite", False)),
        score_overall=float(photo["quality"]) / 2.0,
        features={},
        derivatives=[],
        has_adjustments=False,
    )


def _cases() -> list[dict]:
    """The synthetic cases. Each is {name, photos:[{id,t,lat,lon,quality,favorite,vec}]}.

    Vectors, timestamps and qualities are chosen to exercise one behavior each,
    with no exact ties and no distances sitting on the diversity-floor boundary,
    so the Swift port can reproduce every decision without float ambiguity.
    """
    cases: list[dict] = []

    def geo(i: int) -> dict:
        return {"lat": _LAT, "lon": _LON}

    # 1. rapid-burst-tight: a short spread of near-identical frames -> the
    #    (adaptive) diversity floor limits keepers to a couple.
    photos = []
    spread = [0.0, 0.08, 0.17, 0.26, 0.33]
    quals = [3.1, 3.4, 2.9, 3.7, 3.2]
    for i, (x, q) in enumerate(zip(spread, quals, strict=True)):
        photos.append({"id": f"rbt{i}", "t": 1000.0 + i * 12, **geo(i),
                       "quality": q, "favorite": False,
                       "vec": [x, 0.0, 0.0, 0.0, 0.0, 0.0]})
    cases.append({"name": "rapid-burst-tight", "photos": photos})

    # 2. diverse-shoot: genuinely different shots (near-orthogonal vectors) ->
    #    several keepers, capped by the size tier (tier for 6 -> 3).
    photos = []
    quals = [4.1, 3.8, 4.4, 3.5, 4.0, 3.6]
    for i, q in enumerate(quals):
        vec = [0.0] * 6
        vec[i] = 1.0
        photos.append({"id": f"div{i}", "t": 5000.0 + i * 20, **geo(i),
                       "quality": q, "favorite": False, "vec": vec})
    cases.append({"name": "diverse-shoot", "photos": photos})

    # 3. singleton: two one-of-a-kind photos far apart in time -> two singleton
    #    sessions, no groups, no discards.
    cases.append({"name": "singleton", "photos": [
        {"id": "sole0", "t": 100.0, **geo(0), "quality": 3.3, "favorite": False,
         "vec": [0.2, 0.0, 0.0, 0.0, 0.0, 0.0]},
        {"id": "sole1", "t": 100_000.0, **geo(1), "quality": 3.9, "favorite": False,
         "vec": [0.0, 0.4, 0.0, 0.0, 0.0, 0.0]},
    ]})

    # 4. favorite-promoted: a near-identical burst (normally 1 keeper) where the
    #    lowest-quality frame is a Favorite -> it must be promoted into keepers.
    photos = []
    quals = [3.6, 3.2, 3.9, 0.8]
    favs = [False, False, False, True]
    for i, (q, fav) in enumerate(zip(quals, favs, strict=True)):
        photos.append({"id": f"fav{i}", "t": 8000.0 + i * 9, **geo(i),
                       "quality": q, "favorite": fav,
                       "vec": [1.0, 0.01 * i, 0.0, 0.0, 0.0, 0.0]})
    cases.append({"name": "favorite-promoted", "photos": photos})

    # 5. time-split: two bunches separated by > cluster_gap_seconds (600) ->
    #    two sessions.
    photos = []
    for i in range(3):
        photos.append({"id": f"tsA{i}", "t": 20_000.0 + i * 15, **geo(i),
                       "quality": 3.0 + 0.3 * i, "favorite": False,
                       "vec": [0.0, 0.0, 0.0, 0.0, 0.0, 0.05 * i]})
    for i in range(3):
        photos.append({"id": f"tsB{i}", "t": 21_000.0 + i * 15, **geo(i),
                       "quality": 3.1 + 0.2 * i, "favorite": False,
                       "vec": [0.9, 0.0, 0.0, 0.0, 0.0, 0.04 * i]})
    cases.append({"name": "time-split", "photos": photos})

    # 6. gps-split: two bunches close in time but > cluster_gps_meters (150) apart
    #    (~555 m via +0.005 deg latitude) -> two sessions.
    photos = []
    for i in range(3):
        photos.append({"id": f"gpA{i}", "t": 30_000.0 + i * 10,
                       "lat": _LAT, "lon": _LON,
                       "quality": 3.2 + 0.1 * i, "favorite": False,
                       "vec": [0.0, 0.0, 0.03 * i, 0.0, 0.0, 0.0]})
    for i in range(3):
        photos.append({"id": f"gpB{i}", "t": 30_100.0 + i * 10,
                       "lat": _LAT + 0.005, "lon": _LON,
                       "quality": 3.3 + 0.1 * i, "favorite": False,
                       "vec": [0.8, 0.0, 0.02 * i, 0.0, 0.0, 0.0]})
    cases.append({"name": "gps-split", "photos": photos})

    # 7. large-40: a ~40-photo session -> keeper count follows keeper_tiers /
    #    keepers_max (40 > top tier 39 => keepers_max = 10).
    photos = []
    for i in range(40):
        vec = [
            round((i % 7) / 6.0, 3),
            round((i % 5) / 4.0, 3),
            round((i % 3) / 2.0, 3),
            round((i % 11) / 10.0, 3),
            round((i % 2) / 1.0, 3),
            round((i % 13) / 12.0, 3),
        ]
        photos.append({"id": f"big{i:02d}", "t": 40_000.0 + i * 8, **geo(i),
                       "quality": round(2.0 + (i % 9) * 0.21, 3), "favorite": False,
                       "vec": vec})
    cases.append({"name": "large-40", "photos": photos})

    # 8. uniform-burst: many identical frames -> the floor collapses it to one.
    photos = []
    for i in range(8):
        photos.append({"id": f"uni{i}", "t": 60_000.0 + i * 6, **geo(i),
                       "quality": 3.0 + 0.05 * i, "favorite": False,
                       "vec": [0.5, 0.0, 0.0, 0.0, 0.0, 0.0]})
    cases.append({"name": "uniform-burst", "photos": photos})

    # 9. bimodal-burst (edge): two tight sub-clusters far apart -> one keeper per
    #    side (within-cluster distance is below the floor, so the 3rd is refused).
    photos = []
    for i in range(3):
        photos.append({"id": f"bmA{i}", "t": 70_000.0 + i * 7, **geo(i),
                       "quality": 3.4 + 0.1 * i, "favorite": False,
                       "vec": [1.0, 0.02 * i, 0.0, 0.0, 0.0, 0.0]})
    for i in range(3):
        photos.append({"id": f"bmB{i}", "t": 70_030.0 + i * 7, **geo(i),
                       "quality": 3.5 + 0.1 * i, "favorite": False,
                       "vec": [0.0, 0.02 * i, 1.0, 0.0, 0.0, 0.0]})
    cases.append({"name": "bimodal-burst", "photos": photos})

    # 10. quality-gate-outlier (edge): a genuinely-different frame that is ALSO
    #     the worst quality is dropped by the quality gate, not kept for variety.
    photos = []
    for i in range(5):
        photos.append({"id": f"qg{i}", "t": 80_000.0 + i * 5, **geo(i),
                       "quality": 3.5 + 0.15 * i, "favorite": False,
                       "vec": [1.0, 0.015 * i, 0.0, 0.0, 0.0, 0.0]})
    photos.append({"id": "qgX", "t": 80_030.0, **geo(0),
                   "quality": 0.4, "favorite": False,
                   "vec": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]})
    cases.append({"name": "quality-gate-outlier", "photos": photos})

    # 11. missing-gps-no-split (edge): frames close in time where GPS is present
    #     on some and absent on others -> a missing coordinate never forces a
    #     split, so they stay one session.
    photos = []
    for i in range(4):
        p = {"id": f"mg{i}", "t": 90_000.0 + i * 11,
             "quality": 3.2 + 0.1 * i, "favorite": False,
             "vec": [0.0, 0.0, 0.0, 0.1 * i, 0.0, 0.0]}
        if i % 2 == 0:
            p["lat"], p["lon"] = _LAT, _LON
        photos.append(p)
    cases.append({"name": "missing-gps-no-split", "photos": photos})

    return cases


def _config_block(cfg: Config) -> dict:
    return {
        "clusterGapSeconds": float(cfg.cluster_gap_seconds),
        "clusterGpsMeters": float(cfg.cluster_gps_meters),
        "keeperTiers": [[int(a), int(b)] for a, b in cfg.keeper_tiers],
        "keepersMax": int(cfg.keepers_max),
        "keeperDiversityMin": float(cfg.keeper_diversity_min),
        "keeperDiversityAbsMin": float(cfg.keeper_diversity_abs_min),
    }


def _photo_block(photo: dict) -> dict:
    """Serialize the input photo in the cross-language contract's key order."""
    return {
        "id": photo["id"],
        "t": float(photo["t"]),
        "lat": photo.get("lat"),
        "lon": photo.get("lon"),
        "quality": float(photo["quality"]),
        "favorite": bool(photo.get("favorite", False)),
        "vec": [float(x) for x in photo["vec"]],
    }


def _run_case(case: dict, cfg: Config) -> dict:
    """Build records, run the REAL engine, and record exactly what it produced."""
    records = [_record(p) for p in case["photos"]]
    embeddings = {p["id"]: np.asarray(p["vec"], dtype="float64") for p in case["photos"]}

    sessions = [[r.uuid for r in cluster]
                for cluster in time_gps_clusters(records, cfg)]
    groups = []
    for dg in find_duplicate_groups(records, cfg, embeddings=embeddings):
        keepers = [r.uuid for r in dg.keepers]
        discards = [r.uuid for r in dg.discards]
        groups.append({"members": keepers + discards,
                       "keepers": keepers, "discards": discards})

    return {
        "name": case["name"],
        "photos": [_photo_block(p) for p in case["photos"]],
        "expected": {"sessions": sessions, "groups": groups},
    }


def build_fixtures() -> dict:
    """Build the full fixture document (deterministic)."""
    cfg = Config()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generator": GENERATOR,
        "config": _config_block(cfg),
        "cases": [_run_case(c, cfg) for c in _cases()],
    }


def to_json(doc: dict) -> str:
    """Serialize deterministically (stable key order via ensure_ascii + indent)."""
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
