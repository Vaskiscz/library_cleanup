"""Diagnostic logging must not leak the user's home path / username (audit #14)."""
import logging
import os

from photocleanup import diagnostics


def test_scrub_filter_removes_home_path():
    home = os.path.expanduser("~")
    rec = logging.LogRecord("photo_cleanup", logging.WARNING, "x", 1,
                            "feature print failed for %s", (f"{home}/Pictures/secret.jpg",), None)
    assert diagnostics._ScrubFilter().filter(rec) is True
    msg = rec.getMessage()
    assert home not in msg                 # username/home is gone
    assert "~/Pictures/secret.jpg" in msg  # path is relativised, not lost
