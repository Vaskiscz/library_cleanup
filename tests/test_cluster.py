from photo_cleanup.model import Config
from photo_cleanup.cluster import (haversine_m, time_gps_clusters,
                                   keepers_for_size, select_keepers)
from conftest import mk, FakeEmbeddings

CFG = Config()


def test_haversine_prague_brno():
    assert 180_000 < haversine_m(50.08, 14.42, 49.19, 16.61) < 195_000


def test_time_split_on_gap():
    photos = [mk("a", timestamp=0), mk("b", timestamp=10),
              mk("c", timestamp=10_000)]  # big gap -> new session
    clusters = time_gps_clusters(photos, CFG)
    assert len(clusters) == 2


def test_keepers_for_size_scales():
    assert keepers_for_size(2, CFG) == 1
    assert keepers_for_size(15, CFG) == 5
    assert keepers_for_size(500, CFG) == CFG.keepers_max


def test_singleton_no_discards():
    dg = select_keepers([mk("solo")], CFG)
    assert dg.discards == []


def test_missing_top_embedding_does_not_crash(caplog):
    # audit #5: if the top-ranked frame's Vision embedding failed (absent from the
    # cache) while others have vectors, select_keepers must not raise ValueError.
    photos = [mk(f"p{i}") for i in range(4)]
    emb = FakeEmbeddings({"p1": [1.0, 0.0, 0.0], "p2": [0.0, 1.0, 0.0],
                          "p3": [0.0, 0.0, 1.0]})   # p0 (ranked first) has NO vector
    dg = select_keepers(photos, CFG, embeddings=emb)   # must not raise
    assert dg.size == 4 and len(dg.keepers) >= 1


def test_no_embeddings_at_all_still_keeps_one():
    # every frame's embedding failed -> fall back gracefully, never crash
    photos = [mk(f"q{i}") for i in range(4)]
    emb = FakeEmbeddings({})
    dg = select_keepers(photos, CFG, embeddings=emb)
    assert dg.size == 4 and len(dg.keepers) >= 1


def test_uniform_burst_collapses_to_one():
    # 5 near-identical (all same vector) -> keep 1, discard 4
    photos = [mk(f"p{i}") for i in range(5)]
    emb = FakeEmbeddings({f"p{i}": [1.0, 0.0, 0.0] for i in range(5)})
    dg = select_keepers(photos, CFG, embeddings=emb)
    assert len(dg.keepers) == 1 and len(dg.discards) == 4


def test_diverse_burst_keeps_several():
    # 4 mutually far-apart vectors -> several keepers (all >= diversity floor)
    photos = [mk(f"p{i}") for i in range(4)]
    emb = FakeEmbeddings({"p0": [1, 0, 0], "p1": [0, 1, 0],
                          "p2": [0, 0, 1], "p3": [1, 1, 1]})
    dg = select_keepers(photos, CFG, embeddings=emb)
    assert len(dg.keepers) >= 3


def test_mega_cluster_median_sampled_and_deterministic():
    """Above MEDIAN_SAMPLE_MAX the adaptive-floor median runs on an evenly-
    strided subsample. The expected ordered keeper list is asserted VERBATIM in
    the iOS engine's test (SelectsCore AlgorithmTests, same synthetic data) —
    change one side only in lockstep with the other."""
    n = 500
    photos = [mk(f"p{i:03d}", timestamp=float(i)) for i in range(n)]
    emb = FakeEmbeddings({f"p{i:03d}": [i * 0.01, 0.0] for i in range(n)})
    dg = select_keepers(photos, CFG, embeddings=emb)
    assert [r.uuid for r in dg.keepers] == [
        "p000", "p499", "p249", "p374", "p125",
        "p187", "p312", "p436", "p062", "p468",
    ]
    assert len(dg.discards) == n - 10
    # Deterministic across runs.
    dg2 = select_keepers(photos, CFG, embeddings=emb)
    assert [r.uuid for r in dg2.keepers] == [r.uuid for r in dg.keepers]


def test_favorite_never_discarded():
    photos = [mk("a"), mk("b"), mk("c", favorite=True)]  # all identical embedding
    emb = FakeEmbeddings({u: [1.0, 0.0] for u in ["a", "b", "c"]})
    dg = select_keepers(photos, CFG, embeddings=emb)
    assert any(r.uuid == "c" for r in dg.keepers)
    assert all(r.uuid != "c" for r in dg.discards)


def test_adaptive_floor_keeps_more_in_tight_burst(emb):
    """A tight burst (all pairwise ~0.2, under the fixed 0.30 floor) should
    still keep its 2 most-different-for-that-burst frames."""
    import numpy as np
    from photo_cleanup.cluster import select_keepers
    from photo_cleanup.model import Config
    cfg = Config()
    group = [mk(f"t{i}", timestamp=float(i)) for i in range(6)]
    # vectors on a small arc: pairwise distances ~0.1-0.28 (all < 0.30)
    vecs = {}
    for i, r in enumerate(group):
        theta = 0.28 * i / 5
        vecs[r.uuid] = np.array([np.cos(theta), np.sin(theta), 0.0])
    dg = select_keepers(group, cfg, embeddings=emb(vecs))
    assert len(dg.keepers) >= 2      # fixed floor would have kept just 1


def test_adaptive_floor_still_single_keeper_for_true_dupes(emb):
    """Near-identical frames (pairwise ~0.02) must still collapse to ONE keeper
    — the adaptive floor never drops below keeper_diversity_abs_min."""
    import numpy as np
    from photo_cleanup.cluster import select_keepers
    from photo_cleanup.model import Config
    cfg = Config()
    group = [mk(f"d{i}", timestamp=float(i)) for i in range(5)]
    vecs = {r.uuid: np.array([1.0, 0.004 * i, 0.0]) for i, r in enumerate(group)}
    dg = select_keepers(group, cfg, embeddings=emb(vecs))
    assert len(dg.keepers) == 1
