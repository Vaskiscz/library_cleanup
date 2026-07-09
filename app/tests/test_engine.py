import pytest
from photo_cleanup.cluster import DuplicateGroup
from photocleanup.engine import Engine

from factories import mk, mkv


def test_analyze_progress_is_monotonic_and_phased(monkeypatch, tmp_path):
    """The scan bar must never go backwards, must end at 100%, and the count must
    reflect the current phase (Option A)."""
    from photo_cleanup import cluster, embedding, expired, feedback, scan, screenshots, video
    import photocleanup.delete as delete
    monkeypatch.setattr(delete, "is_authorized", lambda: True)

    vid = tmp_path / "v.mov"; vid.write_bytes(b"x")
    photos = [mk(f"p{i}") for i in range(6)]
    videos = [mkv(f"v{i}", path=str(vid)) for i in range(2)]
    monkeypatch.setattr(scan, "scan_library",
                        lambda *a, movies_only=False, **k: list(videos) if movies_only else list(photos))
    monkeypatch.setattr(cluster, "time_gps_clusters",
                        lambda recs, cfg: [recs[:2]] if recs and not recs[0].is_movie else [])

    class _EC:
        def __init__(self, *a): pass
        def save(self): pass
        def get(self, u): return None
    monkeypatch.setattr(embedding, "EmbeddingCache", _EC)
    monkeypatch.setattr(embedding, "embed_records", lambda recs, ec: None)
    monkeypatch.setattr(screenshots, "classify_screenshot", lambda r, cfg: type("V", (), {"is_work": False})())
    monkeypatch.setattr(expired, "classify_expired", lambda r, cfg: type("V", (), {"is_expired": False})())
    monkeypatch.setattr(feedback, "inject_face_quality",
                        lambda records, progress=None: progress and progress(2, 2))
    monkeypatch.setattr(cluster, "find_duplicate_groups",
                        lambda recs, cfg, embeddings=None, progress=None: (progress and progress(2, 2)) or [])
    monkeypatch.setattr(video, "duplicate_takes",
                        lambda recs, cache, cfg, progress=None: (progress and progress(2, 2)) or [])

    events = []
    Engine().analyze(layers=["dedup", "screenshots", "expired", "videos"],
                     progress=lambda msg, done, total, frac: events.append((msg, done, total, frac)))
    fracs = [f for *_, f in events if f is not None]
    assert fracs == sorted(fracs)          # monotonic, never regresses
    assert fracs[-1] == 1.0                 # ends at 100%
    labels = {m for m, *_ in events}
    assert {"Analyzing photos…", "Detecting faces…", "Analyzing videos…"} <= labels
    photo = [e for e in events if e[0] == "Analyzing photos…"][-1]
    assert photo[1] == 6 and photo[2] == 6  # count = photos in that phase


def test_read_phase_reports_live_progress(monkeypatch):
    """The read preamble must emit live, increasing counts (not one frozen
    'Reading…' state), stay within the read band [0, 0.30], and hand off to the
    compute phases above it."""
    from photo_cleanup import cluster, embedding, expired, feedback, scan, screenshots, video
    import photocleanup.delete as delete
    monkeypatch.setattr(delete, "is_authorized", lambda: True)

    photos = [mk(f"p{i}") for i in range(500)]

    def fake_scan(dbpath=None, movies_only=False, progress=None, **k):
        if progress and not movies_only:          # emulate scan_library's periodic callback
            for i in (100, 300, 500):
                progress(i, 500)
        return [] if movies_only else list(photos)
    monkeypatch.setattr(scan, "scan_library", fake_scan)
    monkeypatch.setattr(cluster, "time_gps_clusters", lambda recs, cfg: [recs[:2]])

    class _EC:
        def __init__(self, *a): pass
        def save(self): pass
        def get(self, u): return None
    monkeypatch.setattr(embedding, "EmbeddingCache", _EC)
    monkeypatch.setattr(embedding, "embed_records", lambda recs, ec: None)
    monkeypatch.setattr(screenshots, "classify_screenshot", lambda r, cfg: type("V", (), {"is_work": False})())
    monkeypatch.setattr(expired, "classify_expired", lambda r, cfg: type("V", (), {"is_expired": False})())
    monkeypatch.setattr(feedback, "inject_face_quality", lambda records, progress=None: progress and progress(1, 1))
    monkeypatch.setattr(cluster, "find_duplicate_groups", lambda recs, cfg, embeddings=None, progress=None: [])
    monkeypatch.setattr(video, "duplicate_takes", lambda recs, cache, cfg, progress=None: [])

    events = []
    Engine().analyze(layers=["dedup"], progress=lambda m, d, t, f: events.append((m, d, t, f)))

    fracs = [f for *_, f in events if f is not None]
    assert fracs == sorted(fracs)                          # monotonic overall
    reading = [e for e in events if e[0] == "Reading photos…"]
    assert [e[1] for e in reading] == [100, 300, 500]      # live per-item count
    assert all(e[3] <= 0.30 + 1e-9 for e in reading)       # within the read band
    assert len({round(e[3], 4) for e in reading}) >= 3     # distinct -> the bar moves
    compute = [f for m, d, t, f in events if m == "Analyzing photos…" and f is not None]
    assert compute and min(compute) >= 0.30 - 1e-9         # compute phases sit above the read band


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
    monkeypatch.setattr(scan, "scan_library",
                        lambda *_, movies_only=False, **k: [] if movies_only else [a, b, h])
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
    vcache = type("C", (), {"get": lambda s, u: vemb.get(u),
                            "put": lambda s, k, v: vemb.__setitem__(k, v),
                            "__contains__": lambda s, k: k in vemb})()
    vcalls = []
    duplicate_takes([va, vb], vcache, cfg,
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


def test_load_records_drops_stale_paths(monkeypatch, tmp_path):
    """A cached record whose local original vanished (purged from the library)
    must not be suggested; iCloud-only records (path=None) are kept.
    (scan_library is stubbed — hitting the real library would need Full Disk
    Access the test shell may lack.)"""
    from photo_cleanup import scan
    good = tmp_path / "img.jpg"; good.write_bytes(b"x")
    recs = [mk("ok", path=str(good)),
            mk("gone", path=str(tmp_path / "missing.jpg")),
            mk("icloud", path=None)]
    monkeypatch.setattr(scan, "scan_library",
                        lambda *a, movies_only=False, **k: [] if movies_only else list(recs))
    got = {r.uuid for r in Engine().load_records()}
    assert got == {"ok", "icloud"}


def test_analyze_rolls_back_state_on_failure(monkeypatch):
    """An aborted scan must not leave stale candidates/records behind."""
    eng = Engine()
    eng._index["stale"] = mk("stale")
    eng._candidates["dedup"] = [{"group_key": "g"}]

    def boom(*a, **k):
        eng._index["partial"] = mk("partial")   # simulate mid-scan population
        raise RuntimeError("scan died")
    monkeypatch.setattr(eng, "_analyze", boom)

    with pytest.raises(RuntimeError):
        eng.analyze()
    assert eng._index == {} and eng._candidates == {}


def test_request_cancel_aborts_analyze(monkeypatch):
    """A cancelled scan raises AnalysisCancelled at the next checkpoint and
    rolls back to a clean slate."""
    from photo_cleanup import scan
    from photocleanup.engine import AnalysisCancelled
    import photocleanup.delete as delete
    monkeypatch.setattr(delete, "is_authorized", lambda: True)
    photos = [mk(f"p{i}") for i in range(10)]
    monkeypatch.setattr(scan, "scan_library",
                        lambda *a, movies_only=False, **k: [] if movies_only else list(photos))

    eng = Engine()
    eng.request_cancel()             # cancel before the loop starts
    with pytest.raises(AnalysisCancelled):
        eng._analyze(layers=["screenshots"])
    # analyze() (the public wrapper) also clears the cancel flag for a NEW scan
    monkeypatch.setattr(eng, "_analyze", lambda *a, **k: {"summary": {}})
    eng.request_cancel()
    assert eng.analyze() == {"summary": {}}     # cleared flag -> runs fine


def test_load_records_writes_nothing_to_disk(monkeypatch, tmp_path):
    """audit #8b: the photo-metadata cache is RAM-only — analyze must not write
    records.json (which held GPS + OCR text) to disk."""
    from photo_cleanup import scan
    cache = tmp_path / "records.json"
    monkeypatch.setattr(scan, "scan_library",
                        lambda *_, movies_only=False, **k: [] if movies_only else [mk("a")])
    eng = Engine(cache=str(cache))
    eng.load_records()
    assert not cache.exists() and not (tmp_path / "records.json.meta.json").exists()


def test_records_are_memoized_in_ram(monkeypatch, tmp_path):
    """Repeat scans in a session reuse the in-RAM memo (no re-read) while the
    library is unchanged — so RAM-only doesn't mean rescanning on every click."""
    from photo_cleanup import scan
    calls = []
    def fake(*_, movies_only=False, **k):
        if not movies_only:
            calls.append(1)
        return [] if movies_only else [mk("a")]
    monkeypatch.setattr(scan, "scan_library", fake)
    monkeypatch.setattr(scan, "_db_mtime", lambda dbpath=None: 123.0)   # stable library mtime
    eng = Engine(cache=str(tmp_path / "r.json"))
    eng.load_records()
    eng.load_records()
    assert len(calls) == 1                     # second load served from the RAM memo
    eng.load_records(force_rescan=True)
    assert len(calls) == 2                     # explicit force re-scans
