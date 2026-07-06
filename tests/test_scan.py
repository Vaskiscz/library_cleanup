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


def test_no_records_disk_writer_exists():
    # The records.json writer/loader are gone — no code path can persist the
    # sensitive photo metadata to disk.
    assert not hasattr(scan, "save_records")
    assert not hasattr(scan, "load_records")
    assert not hasattr(scan, "ensure_records")
