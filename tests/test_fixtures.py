"""The export-fixtures generator is a cross-language contract: these tests pin
its determinism, the keeper_score invariant the synthetic Records rely on, and
the structural schema the Swift port will consume."""

import numpy as np

from photo_cleanup.fixtures import (GENERATOR, SCHEMA_VERSION, _record,
                                    build_fixtures, to_json)
from photo_cleanup.model import Config
from photo_cleanup.quality import keeper_score

CFG = Config()


def test_generator_is_deterministic():
    # Same bytes across two independent runs — the whole point of a golden file.
    assert to_json(build_fixtures()) == to_json(build_fixtures())


def test_synthetic_keeper_score_is_quality_plus_favorite():
    # The fixtures are only meaningful if a synthetic Record's keeper_score is
    # exactly quality (+1 when favorited) — no laplacian/resolution contamination.
    for quality, favorite, expect in [(3.2, False, 3.2), (0.8, True, 1.8),
                                      (4.5, False, 4.5), (2.0, True, 3.0)]:
        rec = _record({"id": "s", "t": 1.0, "quality": quality,
                       "favorite": favorite, "vec": [0.1]})
        assert keeper_score(rec, CFG) == expect


def test_config_block_matches_real_defaults():
    doc = build_fixtures()
    c = doc["config"]
    assert doc["schemaVersion"] == SCHEMA_VERSION
    assert doc["generator"] == GENERATOR
    assert c["clusterGapSeconds"] == CFG.cluster_gap_seconds
    assert c["clusterGpsMeters"] == CFG.cluster_gps_meters
    assert c["keeperTiers"] == [[a, b] for a, b in CFG.keeper_tiers]
    assert c["keepersMax"] == CFG.keepers_max
    assert c["keeperDiversityMin"] == CFG.keeper_diversity_min
    assert c["keeperDiversityAbsMin"] == CFG.keeper_diversity_abs_min


def test_cases_validate_against_schema():
    doc = build_fixtures()
    assert doc["cases"], "expected at least one case"
    names = [c["name"] for c in doc["cases"]]
    assert len(names) == len(set(names)), "case names must be unique"

    for case in doc["cases"]:
        ids = [p["id"] for p in case["photos"]]
        assert len(ids) == len(set(ids)), f"{case['name']}: duplicate photo ids"
        idset = set(ids)
        exp = case["expected"]

        # Sessions partition the photos: every photo appears in exactly one
        # session, and sessions cover the whole set.
        session_ids = [pid for s in exp["sessions"] for pid in s]
        assert len(session_ids) == len(ids), f"{case['name']}: sessions not a partition"
        assert set(session_ids) == idset, f"{case['name']}: sessions miss/extra ids"

        for g in exp["groups"]:
            # members == keepers ∪ discards (order: keepers then discards), and
            # keepers/discards are disjoint subsets of the photos.
            assert g["members"] == g["keepers"] + g["discards"], \
                f"{case['name']}: members != keepers+discards"
            assert set(g["keepers"]).isdisjoint(g["discards"]), \
                f"{case['name']}: a photo is both kept and discarded"
            assert set(g["members"]) <= idset, f"{case['name']}: group has unknown id"
            assert g["discards"], f"{case['name']}: groups must have discards"
            assert g["keepers"], f"{case['name']}: every group keeps at least one"


def test_photo_fields_are_json_scalars():
    doc = build_fixtures()
    for case in doc["cases"]:
        for p in case["photos"]:
            assert isinstance(p["t"], float)
            assert isinstance(p["quality"], float)
            assert isinstance(p["favorite"], bool)
            assert p["lat"] is None or isinstance(p["lat"], float)
            assert isinstance(p["vec"], list) and all(isinstance(x, float) for x in p["vec"])
            # vectors must actually reproduce under numpy (the port's reference)
            assert np.asarray(p["vec"], dtype="float64").ndim == 1
