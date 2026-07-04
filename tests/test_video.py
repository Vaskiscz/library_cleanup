from photo_cleanup.video import metadata_richness, quality_per_byte
from conftest import mk


def test_metadata_richness_prefers_original():
    original = mk("orig", latitude=25.2, longitude=55.3,
                  camera_make="Apple", camera_model="iPhone 12 Pro")
    stripped = mk("msg")  # no GPS, no camera EXIF (messaging copy)
    assert metadata_richness(original) > metadata_richness(stripped)


def test_metadata_gps_weighted_strongest():
    # GPS (weight 2) outweighs a single camera field (weight 1)
    gps_only = mk("g", latitude=1.0, longitude=2.0)
    make_only = mk("c", camera_make="Apple")
    assert metadata_richness(gps_only) > metadata_richness(make_only)


def test_quality_per_byte_handles_zero_size():
    # no file on disk -> size 0 -> ratio 0, must not raise
    assert quality_per_byte(mk("v", width=1920, height=1080)) == 0.0


def _cfgv():
    from photo_cleanup.model import Config
    return Config()


def test_takes_group_by_frames_not_poster(emb):
    """Two takes whose OPENING frames differ but whose sampled frames track each
    other must group; similar posters alone (different content) must NOT."""
    import numpy as np
    from photo_cleanup.video import duplicate_takes
    a = mk("a", is_photo=False, is_movie=True, timestamp=100.0)
    b = mk("b", is_photo=False, is_movie=True, timestamp=110.0)
    c = mk("c", is_photo=False, is_movie=True, timestamp=120.0)
    e1, e2, e3 = np.eye(3)
    vecs = {
        # a & b: dissimilar posters, near-identical sampled frames -> same take
        "a": e1, "b": e2,
        "a#f0": e1, "a#f1": e2, "a#f2": e3,
        "b#f0": e1 * 0.99 + e2 * 0.01, "b#f1": e2 * 0.99 + e1 * 0.01, "b#f2": e3,
        # c: poster similar to a's poster, but frames totally different -> separate
        "c": e1 * 0.98 + e2 * 0.02,
        "c#f0": e2, "c#f1": e3, "c#f2": e1,
    }
    groups = duplicate_takes([a, b, c], emb(vecs), _cfgv())
    assert len(groups) == 1
    members = {r.uuid for r in groups[0].keepers + groups[0].discards}
    assert members == {"a", "b"}


def test_takes_poster_fallback_still_groups(emb):
    """Videos without sampled frames (no local file) fall back to the poster
    embedding — the pre-existing behaviour."""
    import numpy as np
    from photo_cleanup.video import duplicate_takes
    a = mk("a", is_photo=False, is_movie=True, timestamp=100.0)
    b = mk("b", is_photo=False, is_movie=True, timestamp=110.0)
    v = np.array([1.0, 0.0])
    groups = duplicate_takes([a, b], emb({"a": v, "b": v * 0.999}), _cfgv())
    assert len(groups) == 1
