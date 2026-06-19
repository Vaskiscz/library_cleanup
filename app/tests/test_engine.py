from photo_cleanup.cluster import DuplicateGroup
from photocleanup.engine import Engine

from factories import mk


def test_dedup_payload_shape():
    eng = Engine()
    a, b, c = mk("a", timestamp=1), mk("b", timestamp=2), mk("c", timestamp=3)
    groups = [DuplicateGroup(keepers=[a], discards=[b, c])]
    payload = eng.dedup_payload(groups)
    assert len(payload) == 1
    g = payload[0]
    assert g["size"] == 3
    assert g["suggested_keep"] == 1 and g["suggested_discard"] == 2
    assert g["group_key"] == "a"  # min uuid in the group
    assert len(g["photos"]) == 3
    keep = [p for p in g["photos"] if p["suggested_keep"]]
    assert len(keep) == 1 and keep[0]["uuid"] == "a"
    # every photo serialises the fields the FE needs
    for p in g["photos"]:
        assert {"uuid", "filename", "width", "height", "favorite",
                "suggested_keep", "score", "thumb"} <= set(p)
        assert p["thumb"] == f"/api/thumb/{p['uuid']}"


def test_dedup_payload_sorted_largest_first():
    eng = Engine()
    small = DuplicateGroup(keepers=[mk("a")], discards=[mk("b")])
    big = DuplicateGroup(keepers=[mk("c")], discards=[mk("d"), mk("e"), mk("f")])
    payload = eng.dedup_payload([small, big])
    assert [g["size"] for g in payload] == [4, 2]


def test_thumb_bytes_unknown_uuid_is_none():
    assert Engine().thumb_bytes("nope") is None


def test_thumb_bytes_from_real_image(tmp_path):
    from PIL import Image
    p = tmp_path / "img.png"
    Image.new("RGB", (800, 600), (120, 30, 200)).save(p)
    eng = Engine()
    rec = mk("x", path=str(p))
    eng._index[rec.uuid] = rec
    data = eng.thumb_bytes("x", px=64)
    assert data and data[:2] == b"\xff\xd8"  # JPEG SOI marker
