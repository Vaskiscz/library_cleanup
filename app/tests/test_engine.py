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


def test_summarize_counts_and_bytes():
    eng = Engine()
    payload = eng.dedup_payload([DuplicateGroup(keepers=[mk("a")], discards=[mk("b")])])
    s = eng._summarize(payload)
    assert s["groups"] == 1 and s["items"] == 2 and s["removable"] == 1
    assert s["reclaimable_bytes"] >= 0


def test_summarize_months_histogram():
    import datetime as _dt
    ts = lambda y, m: _dt.datetime(y, m, 1).timestamp()
    # grouped: each cluster buckets into its own month
    grouped = [
        {"photos": [{"timestamp": ts(2024, 7), "bytes": 10_000_000, "suggested_keep": True},
                    {"timestamp": ts(2024, 7), "bytes": 9_000_000, "suggested_keep": False}]},
        {"photos": [{"timestamp": ts(2023, 3), "bytes": 8_000_000, "suggested_keep": True},
                    {"timestamp": ts(2023, 3), "bytes": 8_000_000, "suggested_keep": False}]},
    ]
    s = Engine._summarize(grouped, grouped=True)
    by = {m["m"]: m for m in s["months"]}
    assert by["2024-07"] == {"m": "2024-07", "items": 2, "bytes": 9_000_000, "groups": 1}
    assert by["2023-03"]["groups"] == 1 and by["2023-03"]["items"] == 2
    # flat: each item buckets by its own month, no groups
    flat = [{"photos": [{"timestamp": ts(2025, 1), "bytes": 2_000_000, "suggested_keep": False},
                        {"timestamp": ts(2025, 2), "bytes": 3_000_000, "suggested_keep": False}]}]
    f = Engine._summarize(flat, grouped=False)
    fb = {m["m"]: m for m in f["months"]}
    assert fb["2025-01"]["items"] == 1 and fb["2025-01"]["groups"] == 0
    assert fb["2025-02"]["bytes"] == 3_000_000


def test_flat_payload_all_flagged_remove():
    from photo_cleanup.expired import ExpiredVerdict
    eng = Engine()
    rec = mk("x", detected_text="receipt total 100")
    pl = eng.expired_payload([(rec, ExpiredVerdict(True, ["receipt · age 3y"], 3.0, "receipt"))])
    assert len(pl) == 1 and pl[0]["group_key"] == "expired"
    assert pl[0]["photos"][0]["suggested_keep"] is False
    assert "receipt" in pl[0]["photos"][0]["subtitle"]


def test_thumb_bytes_from_real_image(tmp_path):
    from PIL import Image
    p = tmp_path / "img.png"
    Image.new("RGB", (800, 600), (120, 30, 200)).save(p)
    eng = Engine()
    rec = mk("x", path=str(p))
    eng._index[rec.uuid] = rec
    data = eng.thumb_bytes("x", px=64)
    assert data and data[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_thumb_bytes_high_res_prefers_original(tmp_path):
    import io
    from PIL import Image
    big = tmp_path / "orig.jpg"; Image.new("RGB", (1600, 1200), (10, 90, 200)).save(big)
    small = tmp_path / "deriv.jpg"; Image.new("RGB", (160, 120), (10, 90, 200)).save(small)
    eng = Engine()
    rec = mk("x", path=str(big), derivatives=[str(small)])
    eng._index[rec.uuid] = rec

    def longest(data):
        with Image.open(io.BytesIO(data)) as im:
            return max(im.size)

    # grid thumb: smallest source, downscaled small
    assert longest(eng.thumb_bytes("x", px=64)) <= 64
    # detail preview: sourced from the 1600px original (not the 160px derivative),
    # and never upscaled past the source
    assert longest(eng.thumb_bytes("x", px=2048)) == 1600
