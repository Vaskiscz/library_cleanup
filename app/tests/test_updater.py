"""Tests for the in-app updater — version math, GitHub check parsing, URL
guarding, and the /api/update/* endpoints. No real network or process exit."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from factories import make_stub_engine
from photocleanup import updater
from photocleanup.server import create_app
from photocleanup.store import Store


@pytest.fixture
def client():
    app = create_app(store=Store(":memory:"), engine=make_stub_engine())
    with TestClient(app) as c:
        yield c


# ---- version helpers -------------------------------------------------------
@pytest.mark.parametrize("tag,expected", [
    ("v0.2.0", (0, 2, 0)), ("0.2.0", (0, 2, 0)), ("v1.10.3", (1, 10, 3)),
    ("", None), ("v", None), ("nightly", None), ("1.2.x", None), ("1..2", None),
])
def test_parse(tag, expected):
    assert updater._parse(tag) == expected


@pytest.mark.parametrize("latest,current,newer", [
    ((0, 2, 0), (0, 1, 23), True),      # minor beats a high patch
    ((0, 1, 24), (0, 1, 23), True),     # patch bump
    ((0, 1, 23), (0, 1, 23), False),    # same
    ((0, 1, 22), (0, 1, 23), False),    # older
    ((1, 0), (0, 9, 9), True),          # ragged lengths
    ((0, 2), (0, 2, 0), False),         # 0.2 == 0.2.0
])
def test_is_newer(latest, current, newer):
    assert updater.is_newer(latest, current) is newer


# ---- check() ---------------------------------------------------------------
def _fake_release(tag, with_asset=True, size=42_000_000):
    assets = ([{"name": updater.ASSET_NAME, "size": size,
                "browser_download_url":
                    f"https://github.com/{updater.REPO}/releases/download/{tag}/{updater.ASSET_NAME}"}]
              if with_asset else [])
    return {"tag_name": tag, "assets": assets, "body": "- Faster scans\n- Bug fixes",
            "html_url": f"https://github.com/{updater.REPO}/releases/tag/{tag}"}


def _patch_urlopen(monkeypatch, payload):
    def fake_urlopen(req, timeout=0, context=None):
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(updater.urllib.request, "urlopen", fake_urlopen)


def test_check_offers_update_when_newer(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.23")
    _patch_urlopen(monkeypatch, _fake_release("v0.2.0"))
    info = updater.check()
    assert info["available"] is True
    assert info["latest"] == "0.2.0"
    assert info["url"].endswith(updater.ASSET_NAME)
    assert info["size"] == 42_000_000


def test_check_silent_when_current_is_latest(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.2.0")
    _patch_urlopen(monkeypatch, _fake_release("v0.2.0"))
    assert updater.check()["available"] is False


def test_check_not_available_without_asset(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    _patch_urlopen(monkeypatch, _fake_release("v0.2.0", with_asset=False))
    assert updater.check()["available"] is False


def test_check_stays_quiet_when_offline(monkeypatch):
    def boom(req, timeout=0, context=None):
        raise OSError("offline")
    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    info = updater.check()
    assert info["available"] is False
    assert "error" in info


# ---- download / apply guards ----------------------------------------------
def test_download_rejects_unexpected_url():
    with pytest.raises(ValueError):
        updater.download_dmg("https://evil.example.com/x.dmg")


def test_apply_update_refuses_outside_bundle(monkeypatch):
    # In the test env there is no enclosing .app, so self-install must refuse
    # rather than delete/replace anything or exit the process.
    monkeypatch.setattr(updater, "app_bundle_path", lambda: None)
    with pytest.raises(RuntimeError):
        updater.apply_update("/tmp/whatever.dmg")


# ---- endpoints -------------------------------------------------------------
def test_update_check_endpoint(client, monkeypatch):
    monkeypatch.setattr("photocleanup.updater.check",
                        lambda: {"available": True, "latest": "0.2.0"})
    r = client.get("/api/update/check")
    assert r.status_code == 200 and r.json()["available"] is True


def test_update_apply_when_nothing_available(client, monkeypatch):
    monkeypatch.setattr("photocleanup.updater.check", lambda: {"available": False})
    r = client.post("/api/update/apply")
    assert r.json() == {"started": False, "available": False, "error": None}


def test_update_apply_falls_back_when_cannot_install(client, monkeypatch):
    monkeypatch.setattr("photocleanup.updater.check", lambda: {
        "available": True, "url": "https://x", "can_install": False,
        "html_url": "https://github.com/Vaskiscz/library_cleanup/releases"})
    r = client.post("/api/update/apply").json()
    assert r["started"] is False and r["can_install"] is False
    assert r["html_url"].startswith("https://github.com/Vaskiscz/library_cleanup")
