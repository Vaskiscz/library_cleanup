import pytest
from photo_cleanup.cluster import DuplicateGroup
from photocleanup.engine import Engine

from factories import mk, mkv


def test_analyze_requires_photos_access(monkeypatch):
    """Photos read-write must be granted at connection — fail fast (before any
    scanning) rather than after a whole review."""
    import photocleanup.delete as delete
    monkeypatch.setattr(delete, "ensure_access", lambda timeout=120.0: 0)
    monkeypatch.setattr(delete, "is_authorized", lambda: False)
    with pytest.raises(PermissionError):
        Engine().analyze(layers=["dedup"])


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


def test_manual_feed_includes_kept_items_excludes_hidden(monkeypatch):
    from photo_cleanup import scan
    from photo_cleanup.apply import KW_REVIEWED
    a = mk("a")                              # normal
    b = mk("b", keywords=[KW_REVIEWED])      # already kept (reviewed:keep)
    h = mk("h", is_hidden=True)              # hidden
    monkeypatch.setattr(scan, "ensure_records", lambda *args, **kw: [a, b, h])
    monkeypatch.setattr(scan, "scan_library", lambda *args, **kw: [])
    eng = Engine()
    # curated scan: drops the kept + hidden ones
    assert {r.uuid for r in eng.load_records()} == {"a"}
    # manual feed: shows the kept one too, only Hidden is off-limits
    feed = eng.all_items()[0]["photos"]
    assert {p["uuid"] for p in feed} == {"a", "b"}


def test_thumb_cache_is_memory_only_and_warms(tmp_path):
    from PIL import Image
    p = tmp_path / "img.jpg"; Image.new("RGB", (800, 600), (10, 90, 200)).save(p)
    eng = Engine()
    rec = mk("x", path=str(p)); eng._index[rec.uuid] = rec
    # rendered once, then served from RAM — second call returns the identical object
    first = eng.thumb_bytes("x", px=64)
    assert first and ("x", 64) in eng._thumb_cache
    assert eng.thumb_bytes("x", px=64) is first      # cache hit, not re-rendered
    # warming pre-renders grid thumbs for current candidates into the same RAM cache
    eng._thumb_cache.clear(); eng._thumb_used = 0
    eng._candidates = {"dedup": [{"photos": [{"uuid": "x"}]}]}
    eng.warm_thumbnails(px=64)
    assert ("x", 64) in eng._thumb_cache
    assert eng._warming is False


def test_grouping_reports_progress():
    """The heavy post-passes must report incremental progress so the scan bar
    keeps moving instead of freezing on 'Grouping photoshoots…'."""
    import numpy as np
    from photo_cleanup.model import Config
    from photo_cleanup.cluster import find_duplicate_groups
    from photo_cleanup.video import duplicate_takes
    cfg = Config()

    a, b = mk("a", timestamp=1000.0), mk("b", timestamp=1001.0)
    emb = {"a": np.array([1.0, 0, 0]), "b": np.array([0.999, 0.001, 0])}
    calls = []
    find_duplicate_groups([a, b], cfg, embeddings=type("E", (), {"get": lambda s, u: emb.get(u)})(),
                          progress=lambda i, n: calls.append((i, n)))
    assert calls and calls[-1][0] == calls[-1][1]      # reaches 100% of its work

    va, vb = mkv("va", timestamp=2000.0), mkv("vb", timestamp=2001.0)
    vemb = {"va": np.array([1.0, 0]), "vb": np.array([1.0, 0])}
    vcalls = []
    duplicate_takes([va, vb], type("C", (), {"get": lambda s, u: vemb.get(u)})(), cfg,
                    progress=lambda i, n: vcalls.append((i, n)))
    assert vcalls and vcalls[-1][0] == vcalls[-1][1]


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
