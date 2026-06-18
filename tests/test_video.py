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
