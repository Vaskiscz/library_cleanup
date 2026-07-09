"""apply.py pure logic — ApplyResult accounting and auth-error routing.

Everything runs against fake photoscript objects; no Photos app, no AppleScript.
"""
import sys
import types

import pytest

from photo_cleanup import apply as apply_mod
from photo_cleanup.apply import ApplyResult, NotAuthorizedError, _is_auth_error


class FakePhoto:
    """Stands in for photoscript.Photo. `broken` makes every keyword/favorite
    access raise (simulates a failed Apple event for that photo)."""

    def __init__(self, uuid, keywords=(), favorite=False, broken=None):
        self.uuid = uuid
        self._kw = list(keywords)
        self._fav = favorite
        self._broken = broken

    def _check(self):
        if self._broken is not None:
            raise self._broken

    @property
    def keywords(self):
        self._check()
        return list(self._kw)

    @keywords.setter
    def keywords(self, value):
        self._check()
        self._kw = list(value)

    @property
    def favorite(self):
        self._check()
        return self._fav

    @favorite.setter
    def favorite(self, value):
        self._check()
        self._fav = bool(value)


class FakeLib:
    """Stands in for photoscript.PhotosLibrary (only the batched uuid query)."""

    def __init__(self, photos):
        self.by_uuid = {p.uuid: p for p in photos}

    def photos(self, uuid=None):
        return [self.by_uuid[u] for u in (uuid or []) if u in self.by_uuid]


@pytest.fixture
def lib(monkeypatch):
    """Install a FakeLib with the given photos as the apply-module library."""
    def _install(*photos):
        fake = FakeLib(photos)
        monkeypatch.setattr(apply_mod, "_library", lambda: fake)
        return fake
    return _install


@pytest.fixture
def no_lib(monkeypatch):
    """Fail the test if any code path tries to open the Photos library."""
    def boom():
        raise AssertionError("dry run must not touch the Photos library")
    monkeypatch.setattr(apply_mod, "_library", boom)


# ---------------------------------------------------------------- auth routing

def test_is_auth_error_matches_1743_and_not_authorized():
    assert _is_auth_error(Exception("Photos got an error (-1743)"))
    assert _is_auth_error(Exception("Not authorized to send Apple events"))
    assert not _is_auth_error(Exception("application isn't running (-600)"))


def test_library_raises_not_authorized(monkeypatch):
    # Probe failure with -1743 must surface as NotAuthorizedError, loudly.
    class DeniedLib:
        @property
        def photos(self):
            raise Exception("Not authorized to send Apple events to Photos. (-1743)")

        def album_names(self):
            raise Exception("Not authorized to send Apple events to Photos. (-1743)")

    mod = types.ModuleType("photoscript")
    mod.PhotosLibrary = DeniedLib
    monkeypatch.setitem(sys.modules, "photoscript", mod)
    with pytest.raises(NotAuthorizedError):
        apply_mod._library()


def test_library_tolerates_non_auth_probe_errors(monkeypatch):
    # e.g. Photos not running — the probe must NOT raise, only auth errors do.
    class SleepyLib:
        @property
        def photos(self):
            raise Exception("Photos isn't running (-600)")

        def album_names(self):
            raise Exception("Photos isn't running (-600)")

    mod = types.ModuleType("photoscript")
    mod.PhotosLibrary = SleepyLib
    monkeypatch.setitem(sys.modules, "photoscript", mod)
    assert apply_mod._library() is not None


# ------------------------------------------------------------- ApplyResult.error

def test_first_error_keeps_only_the_first_reason():
    res = ApplyResult()
    res.error("first failure")
    res.error("second failure")
    assert res.errors == 2
    assert res.first_error == "first failure"


# ----------------------------------------------------------------- add_keyword

def test_add_keyword_dry_run_counts_without_library(no_lib):
    res = apply_mod.add_keyword(["a", "b", "c"], "cleanup:duplicate", apply=False)
    assert (res.tagged, res.skipped, res.errors) == (3, 0, 0)
    assert res.first_error is None


def test_add_keyword_accounting(lib):
    fresh = FakePhoto("fresh", keywords=["holiday"])
    already = FakePhoto("done", keywords=["cleanup:duplicate"])
    broken = FakePhoto("broken", broken=RuntimeError("AppleEvent timed out (-1712)"))
    lib(fresh, already, broken)

    res = apply_mod.add_keyword(["fresh", "done", "broken", "missing"],
                                "cleanup:duplicate", apply=True)
    assert (res.tagged, res.skipped, res.errors) == (1, 1, 2)
    assert fresh._kw == ["cleanup:duplicate", "holiday"]   # sorted union, old kept
    assert res.first_error == "AppleEvent timed out (-1712)"   # first failure wins


def test_add_keyword_missing_photo_reason(lib):
    lib()   # empty library — every uuid is "not found"
    res = apply_mod.add_keyword(["ghost"], "cleanup:expired", apply=True)
    assert res.errors == 1
    assert res.first_error == "photo not found: ghost"


def test_add_keyword_reports_progress(lib):
    lib(FakePhoto("a"), FakePhoto("b"))
    seen = []
    apply_mod.add_keyword(["a", "b"], "reviewed:keep", apply=True,
                          progress=lambda i, n: seen.append((i, n)))
    assert seen == [(1, 2), (2, 2)]


# -------------------------------------------------------------------- favorite

def test_favorite_dry_run(no_lib):
    res = apply_mod.favorite(["a", "b"], apply=False)
    assert (res.favorited, res.errors) == (2, 0)


def test_favorite_accounting(lib):
    plain = FakePhoto("plain")
    hearted = FakePhoto("hearted", favorite=True)
    broken = FakePhoto("broken", broken=RuntimeError("event failed"))
    lib(plain, hearted, broken)

    res = apply_mod.favorite(["plain", "hearted", "broken", "missing"], apply=True)
    assert (res.favorited, res.skipped, res.errors) == (1, 1, 2)
    assert plain._fav is True
    assert res.first_error == "event failed"


# ------------------------------------------------------ clear_keywords_for_uuids

def test_clear_keywords_dry_run(no_lib):
    res = apply_mod.clear_keywords_for_uuids(["a"], apply=False)
    assert res.tagged == 1


def test_clear_keywords_accounting(lib):
    tagged = FakePhoto("tagged", keywords=["cleanup:duplicate", "holiday"])
    clean = FakePhoto("clean", keywords=["holiday"])
    broken = FakePhoto("broken", broken=RuntimeError("write refused"))
    lib(tagged, clean, broken)

    res = apply_mod.clear_keywords_for_uuids(
        ["tagged", "clean", "broken", "missing"], apply=True)
    assert (res.tagged, res.skipped, res.errors) == (1, 1, 2)
    assert tagged._kw == ["holiday"]           # only the cleanup:* keyword removed
    assert res.first_error == "write refused"


# ------------------------------------------------------------- unfavorite_uuids

def test_unfavorite_dry_run(no_lib):
    res = apply_mod.unfavorite_uuids(["a", "b", "c"], apply=False)
    assert res.favorited == 3


def test_unfavorite_accounting(lib):
    hearted = FakePhoto("hearted", favorite=True)
    plain = FakePhoto("plain")
    broken = FakePhoto("broken", broken=RuntimeError("unfav failed"))
    lib(hearted, plain, broken)

    res = apply_mod.unfavorite_uuids(["hearted", "plain", "broken", "missing"], apply=True)
    assert (res.favorited, res.skipped, res.errors) == (1, 1, 2)
    assert hearted._fav is False
    assert res.first_error == "unfav failed"


# ---------------------------------------------------------------- undo_keywords

def test_undo_keywords_dry_run_counts_tagged(monkeypatch, no_lib):
    monkeypatch.setattr(apply_mod, "find_tagged_uuids", lambda prefix, dbpath: ["a", "b"])
    res = apply_mod.undo_keywords(apply=False)
    assert (res.tagged, res.errors) == (2, 0)


def test_undo_keywords_accounting(monkeypatch, lib):
    tagged = FakePhoto("tagged", keywords=["cleanup:screenshot", "trip"])
    broken = FakePhoto("broken", broken=RuntimeError("undo failed"))
    lib(tagged, broken)
    monkeypatch.setattr(apply_mod, "find_tagged_uuids",
                        lambda prefix, dbpath: ["tagged", "broken", "missing"])

    res = apply_mod.undo_keywords(apply=True)
    assert (res.tagged, res.errors) == (1, 2)
    assert tagged._kw == ["trip"]
    assert res.first_error == "undo failed"
