"""The records provider is RAM-only (audit #8): scanned metadata (GPS, Apple
Vision OCR text, filenames) is never written to disk, and the disk-cache writer
no longer exists at all."""
from photo_cleanup import scan
from conftest import mk


def test_records_ram_is_memoized_in_process(monkeypatch):
    calls = []
    monkeypatch.setattr(scan, "scan_library",
                        lambda *a, **k: calls.append(1) or [mk("x", detected_text="code 123456")])
    monkeypatch.setattr(scan, "_db_mtime", lambda dbpath=None: 42.0)
    scan._RAM_RECORDS.clear()

    r1 = scan.records_ram()
    r2 = scan.records_ram()
    assert r1[0].uuid == "x" and r2 is r1        # served from the in-RAM memo
    assert len(calls) == 1                        # library not re-scanned
    scan.records_ram(force=True)
    assert len(calls) == 2                        # explicit force re-scans


class _FakePhoto:
    """Bare-minimum osxphotos photo stand-in; every attribute photo_to_record
    probes beyond these falls back through _safe()."""
    def __init__(self, uuid, ismovie=False):
        self.uuid = uuid
        self.isphoto = not ismovie
        self.ismovie = ismovie
        self.hidden = False
        self.shared = False


class _FakeDB:
    def __init__(self, photos):
        self._photos = photos
        self.calls = 0

    def photos(self, **kw):
        self.calls += 1
        if kw.get("movies"):
            return [p for p in self._photos if p.ismovie]
        return list(self._photos)


def test_scan_library_reuses_shared_db():
    """A caller scanning photos AND videos can pass one already-parsed PhotosDB
    (the parse is the expensive part) instead of constructing two."""
    db = _FakeDB([_FakePhoto("p1"), _FakePhoto("v1", ismovie=True)])
    photos = scan.scan_library(db=db)
    movies = scan.scan_library(db=db, movies_only=True)
    assert [r.uuid for r in photos] == ["p1"]
    assert [r.uuid for r in movies] == ["v1"]
    assert db.calls == 2                     # same instance served both scans


def test_scan_library_accepts_lazy_db_factory():
    """db= may be a zero-arg factory, invoked only when the scan really reads —
    lets memo-holding callers share one parse without paying it up front."""
    db = _FakeDB([_FakePhoto("p1")])
    made = []

    def factory():
        made.append(1)
        return db

    recs = scan.scan_library(db=factory)
    assert [r.uuid for r in recs] == ["p1"] and made == [1]


def test_no_records_disk_writer_exists():
    # The records.json writer/loader are gone — no code path can persist the
    # sensitive photo metadata to disk.
    assert not hasattr(scan, "save_records")
    assert not hasattr(scan, "load_records")
    assert not hasattr(scan, "ensure_records")
