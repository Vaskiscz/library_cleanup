"""Tests for the on-disk records cache (audit #8): it holds sensitive derived
metadata (GPS, OCR text, filenames), so it must not be world/group-readable."""
import json
import os
import stat

from photo_cleanup.scan import save_records
from conftest import mk


def test_records_cache_is_owner_only(tmp_path):
    cache_dir = tmp_path / "photo-cleanup"
    path = str(cache_dir / "records.json")
    rec = mk("x", latitude=50.0, longitude=14.0, detected_text="one-time code 123456")

    save_records([rec], path)          # dbpath=None -> lib_mtime None, fine

    # directory 0700, both files 0600 — not readable by group/other or lax backups
    assert stat.S_IMODE(os.stat(str(cache_dir)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path + ".meta.json").st_mode) == 0o600

    # sanity: the data round-trips (we only locked perms, didn't corrupt content)
    data = json.load(open(path))
    assert data[0]["uuid"] == "x"
