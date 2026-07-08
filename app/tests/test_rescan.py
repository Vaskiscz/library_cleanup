"""Rescan-after-delete: deleting prunes the in-RAM records so the next analyze
re-clusters the survivors WITHOUT re-parsing the whole library via osxphotos —
identical results, near-instant, and still RAM-only (audit #8)."""
import builtins

from photo_cleanup import scan
from photocleanup.engine import Engine

from factories import mk, mkv


def _stub_library(monkeypatch, photos, videos, mtime=1.0):
    """Fake osxphotos: count reads and hold the library mtime steady so the memo
    is considered fresh unless we prune (which adopts the current mtime) or the
    test moves the mtime to simulate an out-of-band edit."""
    calls = {"n": 0}

    def fake_scan(dbpath=None, movies_only=False, **k):
        calls["n"] += 1
        return list(videos) if movies_only else list(photos)

    monkeypatch.setattr(scan, "scan_library", fake_scan)
    monkeypatch.setattr(scan, "_db_mtime", lambda dbpath=None: mtime)
    return calls


def test_forget_prunes_records_videos_and_index(monkeypatch):
    photos = [mk("a"), mk("b"), mk("c")]
    videos = [mkv("v1"), mkv("v2")]
    _stub_library(monkeypatch, photos, videos)
    eng = Engine()
    eng.load_records()
    eng.load_videos()
    assert eng.record("b") is not None

    eng.forget(["b", "v2"])

    assert {r.uuid for r in eng._records_memo} == {"a", "c"}
    assert {r.uuid for r in eng._videos_memo} == {"v1"}
    assert eng.record("b") is None and eng.record("v2") is None


def test_rescan_after_forget_skips_library_read(monkeypatch):
    photos = [mk("a"), mk("b"), mk("c")]
    videos = [mkv("v1"), mkv("v2")]
    calls = _stub_library(monkeypatch, photos, videos)
    eng = Engine()
    eng.load_records()
    eng.load_videos()
    reads = calls["n"]                    # one photos + one videos parse

    eng.forget(["b", "v2"])
    eng.load_records()                    # rescan: survivors from pruned RAM memo
    eng.load_videos()

    assert calls["n"] == reads            # <-- no additional osxphotos read
    assert {r.uuid for r in eng._records_memo} == {"a", "c"}
    assert {r.uuid for r in eng._videos_memo} == {"v1"}


def test_out_of_band_change_forces_fresh_read(monkeypatch):
    photos = [mk("a"), mk("b")]
    calls = _stub_library(monkeypatch, photos, [], mtime=1.0)
    eng = Engine()
    eng.load_records()
    eng.forget(["b"])
    reads = calls["n"]

    # Library edited elsewhere -> mtime moves -> the pruned memo must invalidate
    # and a full re-read must happen (correctness beats the fast path).
    monkeypatch.setattr(scan, "_db_mtime", lambda dbpath=None: 2.0)
    eng.load_records()
    assert calls["n"] == reads + 1


def test_load_force_rescan_rereads_even_with_fresh_memo(monkeypatch):
    photos = [mk("a"), mk("b")]
    calls = _stub_library(monkeypatch, photos, [])
    eng = Engine()
    eng.load_records()
    reads = calls["n"]

    eng.load_records()                       # memo hit -> no read
    assert calls["n"] == reads
    eng.load_records(force_rescan=True)      # explicit Re-scan -> full re-read
    assert calls["n"] == reads + 1


def test_forget_writes_nothing_to_disk(monkeypatch):
    """The prune is a pure RAM operation — it must never open a file for writing
    (records carry GPS + OCR text; they stay off disk)."""
    photos = [mk("a", latitude=50.1, longitude=14.4, detected_text="SECRET OCR"),
              mk("b")]
    _stub_library(monkeypatch, photos, [])
    eng = Engine()
    eng.load_records()

    real_open = builtins.open

    def guard_open(file, mode="r", *a, **k):
        assert not any(c in mode for c in "wax"), f"forget() wrote to {file!r}"
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guard_open)
    eng.forget(["a"])                     # must not persist anything
    assert {r.uuid for r in eng._records_memo} == {"b"}
