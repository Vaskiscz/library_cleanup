"""Tests for the in-app updater — version math, GitHub check parsing, URL
guarding, and the /api/update/* endpoints. No real network or process exit."""
import io
import json
import os
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

from factories import make_stub_engine
from photocleanup import updater
from photocleanup.server import create_app
from photocleanup.store import Store


@pytest.fixture
def client():
    app = create_app(store=Store(":memory:"), engine=make_stub_engine())
    with TestClient(app, base_url="http://127.0.0.1") as c:
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


# ---- download integrity (#7/#19) -------------------------------------------
class _FakeResp:
    def __init__(self, data, total=None):
        self._b = io.BytesIO(data)
        self.headers = {"Content-Length": str(total if total is not None else len(data))}

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_dmg_success(monkeypatch, tmp_path):
    payload = b"FAKE-DMG-BYTES" * 100
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda req, timeout=0, context=None: _FakeResp(payload))
    url = updater._ALLOWED_PREFIX + "v0.2.0/Library-Cleanup.dmg"
    dest = updater.download_dmg(url)
    try:
        assert open(dest, "rb").read() == payload
    finally:
        os.unlink(dest)


def test_download_dmg_truncated_body_raises_and_cleans_up(monkeypatch):
    # server claims 9999 bytes but sends 3 -> must raise AND leave no orphan temp file
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda req, timeout=0, context=None: _FakeResp(b"abc", total=9999))
    paths = []
    real_mkstemp = updater.tempfile.mkstemp

    def cap(*a, **k):
        fd, p = real_mkstemp(*a, **k)
        paths.append(p)
        return fd, p
    monkeypatch.setattr(updater.tempfile, "mkstemp", cap)
    with pytest.raises(IOError, match="incomplete download"):
        updater.download_dmg(updater._ALLOWED_PREFIX + "v0.2.0/Library-Cleanup.dmg")
    assert paths and not os.path.exists(paths[0])


# ---- signature/identity verification (#1) ----------------------------------
def _fake_run_factory(fail_on_R=False):
    """A fake subprocess.run: codesign -d -r- returns a designated requirement;
    everything else succeeds — unless fail_on_R, in which case the identity-pin
    (`-R=`) check raises like a real mismatch."""
    def fake_run(cmd, check=False, capture_output=False, text=False, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        r = R()
        if cmd[:3] == ["/usr/bin/codesign", "-d", "-r-"]:
            r.stdout = ('Executable=/x\ndesignated => identifier '
                        '"cz.vaskiscz.photocleanup" and certificate leaf = H"abc"\n')
            return r
        if fail_on_R and any(isinstance(c, str) and c.startswith("-R=") for c in cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="requirement not satisfied")
        return r
    return fake_run


def test_verify_bundle_accepts_matching_identity(monkeypatch):
    monkeypatch.setattr(updater.subprocess, "run", _fake_run_factory(fail_on_R=False))
    updater.verify_bundle("/new.app", "/cur.app")     # must not raise


def test_verify_bundle_rejects_mismatched_identity(monkeypatch):
    monkeypatch.setattr(updater.subprocess, "run", _fake_run_factory(fail_on_R=True))
    with pytest.raises(subprocess.CalledProcessError):
        updater.verify_bundle("/new.app", "/cur.app")


def test_apply_update_aborts_and_does_not_swap_when_verification_fails(monkeypatch, tmp_path):
    app = tmp_path / "Library Cleanup.app"
    app.mkdir()
    mount = str(tmp_path / "mnt")
    monkeypatch.setattr(updater, "app_bundle_path", lambda: str(app))
    monkeypatch.setattr(updater, "_mount_and_find_app", lambda dmg: (mount, mount + "/X.app"))
    monkeypatch.setattr(updater, "verify_bundle",
                        lambda new, cur: (_ for _ in ()).throw(RuntimeError("bad signer")))
    popen_calls, timer_calls, run_calls = [], [], []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: popen_calls.append(a) or object())
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: run_calls.append(a[0]) or None)
    monkeypatch.setattr(updater.threading, "Timer", lambda *a, **k: timer_calls.append(a) or type("T", (), {"start": lambda self: None})())
    with pytest.raises(RuntimeError):
        updater.apply_update("/tmp/x.dmg")
    assert not popen_calls, "must not spawn the swap helper on failed verification"
    assert not timer_calls, "must not schedule os._exit on failed verification"
    assert any("detach" in c for c in run_calls), "must detach the mounted DMG"


def test_apply_update_success_spawns_helper_after_verify(monkeypatch, tmp_path):
    app = tmp_path / "Library Cleanup.app"
    app.mkdir()
    mount, src = str(tmp_path / "mnt"), str(tmp_path / "mnt" / "X.app")
    monkeypatch.setattr(updater, "app_bundle_path", lambda: str(app))
    monkeypatch.setattr(updater, "_mount_and_find_app", lambda dmg: (mount, src))
    monkeypatch.setattr(updater, "verify_bundle", lambda new, cur: None)
    popen_calls, timer_calls = [], []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: popen_calls.append(a[0]) or object())

    class _Timer:
        def __init__(self, *a, **k):
            timer_calls.append(a)

        def start(self):
            pass
    monkeypatch.setattr(updater.threading, "Timer", _Timer)
    updater.apply_update("/tmp/x.dmg", exit_delay=0.01)
    assert len(popen_calls) == 1
    argv = popen_calls[0]
    # [bash, script, pid, SRC, APP, MOUNT, DMG]
    assert argv[0] == "/bin/bash" and argv[3] == src and argv[4] == str(app)
    assert argv[5] == mount and argv[6] == "/tmp/x.dmg"
    assert oct(os.stat(argv[1]).st_mode)[-3:] == "755"
    assert timer_calls and timer_calls[0][1] is os._exit
    os.unlink(argv[1])


def test_relaunch_helper_is_valid_bash():
    fd, p = tempfile.mkstemp(suffix=".sh")
    os.write(fd, updater._HELPER.encode())
    os.close(fd)
    try:
        assert subprocess.run(["bash", "-n", p]).returncode == 0
    finally:
        os.unlink(p)
