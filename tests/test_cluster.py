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


def test_favorite_never_discarded():
    photos = [mk("a"), mk("b"), mk("c", favorite=True)]  # all identical embedding
    emb = FakeEmbeddings({u: [1.0, 0.0] for u in ["a", "b", "c"]})
    dg = select_keepers(photos, CFG, embeddings=emb)
    assert any(r.uuid == "c" for r in dg.keepers)
    assert all(r.uuid != "c" for r in dg.discards)
