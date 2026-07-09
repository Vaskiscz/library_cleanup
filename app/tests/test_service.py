import time

import pytest
from fastapi.testclient import TestClient

from factories import StubEngine, make_stub_engine, mk, mkv
from photocleanup.server import create_app
from photocleanup.store import Store


@pytest.fixture(autouse=True)
def _no_photo_prompt(monkeypatch):
    # analyze requests Photos access; never trigger a real prompt in tests
    monkeypatch.setattr("photocleanup.delete.ensure_access", lambda timeout=120.0: 3)


@pytest.fixture
def client():
    app = create_app(store=Store(":memory:"), engine=make_stub_engine())
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def _run_analyze(client, layers):
    """Start the analyze job and poll until it finishes; returns the summary."""
    assert client.post("/api/analyze", json={"layers": layers}).json()["started"] is True
    for _ in range(100):
        p = client.get("/api/progress").json()
        if p["status"] == "done":
            return p["summary"]
        if p["status"] == "error":
            raise AssertionError(p.get("error"))
        time.sleep(0.02)
    raise AssertionError("analyze did not finish")


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"]
    assert set(body["layers"]) == {"dedup", "videos", "screenshots", "expired"}


def test_ui_assets_are_not_cached(client):
    # The WebView cache survives auto-updates (stable bundle id), so UI assets
    # must be no-store or the app keeps running the previous build's app.js.
    for path in ("/", "/static/app.js"):
        cc = client.get(path).headers.get("cache-control", "")
        assert "no-store" in cc, f"{path} must be no-store, got {cc!r}"
    # API responses are unaffected by the UI cache rule.
    assert "no-store" not in (client.get("/api/health").headers.get("cache-control") or "")


def test_delete_prunes_deleted_uuids_from_engine(monkeypatch):
    # After a real delete, the endpoint prunes the removed assets from the engine
    # (requested minus unmatched) so the next scan skips re-reading the library.
    import photocleanup.delete as delete_mod
    eng = make_stub_engine()
    seen = {}
    real_forget = eng.forget
    monkeypatch.setattr(eng, "forget",
                        lambda uuids: (seen.__setitem__("uuids", list(uuids)), real_forget(uuids))[1])
    monkeypatch.setattr(delete_mod, "delete_assets",
                        lambda uuids, dry_run=False: {"status": "ok", "requested": len(uuids),
                                                      "matched": 2, "deleted": 2, "unmatched": ["c"]})
    app = create_app(store=Store(":memory:"), engine=eng)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.post("/api/delete", json={"uuids": ["a", "b", "c"]}).status_code == 200
    assert seen["uuids"] == ["a", "b"]                # unmatched "c" is NOT forgotten


def test_delete_dry_run_does_not_prune(monkeypatch):
    import photocleanup.delete as delete_mod
    eng = make_stub_engine()
    called = {"n": 0}
    monkeypatch.setattr(eng, "forget", lambda uuids: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(delete_mod, "delete_assets",
                        lambda uuids, dry_run=False: {"status": "ok", "dry_run": True,
                                                      "requested": len(uuids), "matched": 2,
                                                      "deleted": 0, "unmatched": []})
    app = create_app(store=Store(":memory:"), engine=eng)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.post("/api/delete", json={"uuids": ["a", "b"], "dry_run": True})
    assert called["n"] == 0                            # nothing deleted -> nothing pruned


def test_analyze_summary(client):
    summary = _run_analyze(client, ["dedup", "videos", "expired"])
    assert summary["dedup"]["groups"] == 1 and summary["dedup"]["removable"] == 2
    assert summary["videos"]["groups"] == 1 and summary["videos"]["removable"] == 1
    assert summary["expired"]["items"] == 0  # stub records aren't expired


def test_rejects_foreign_host(client):
    # loopback Host is allowed (the client fixture pins base_url to 127.0.0.1)...
    assert client.get("/api/health").status_code == 200
    # ...a rebound/foreign Host is refused (DNS-rebinding defense)
    assert client.get("/api/health", headers={"host": "evil.example.com"}).status_code == 400


def test_diagnostics_endpoint(client):
    r = client.get("/api/diagnostics")
    assert r.status_code == 200
    assert "log_path" in r.json() and "library-cleanup.log" in r.json()["log_path"]


def test_analyze_rejects_bad_layer(client):
    assert client.post("/api/analyze", json={"layers": ["bogus"]}).status_code == 400


def test_candidates_dedup(client):
    body = client.get("/api/candidates", params={"layer": "dedup"}).json()
    assert body["layer"] == "dedup" and len(body["groups"]) == 1
    g = body["groups"][0]
    assert g["size"] == 3 and "title" in g
    assert all("decided" in p for p in g["photos"])


def test_video_404_without_file(client):
    assert client.get("/api/video/v1").status_code == 404   # stub video has no path on disk
    assert client.get("/api/video/nope").status_code == 404


def test_video_streams_with_range(tmp_path):
    eng = make_stub_engine()
    vid = tmp_path / "clip.mov"; vid.write_bytes(b"\x00\x01\x02\x03\x04")
    eng._index["vx"] = mkv("vx", path=str(vid))
    app = create_app(store=Store(":memory:"), engine=eng)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.get("/api/video/vx")
        assert r.status_code == 200 and r.content == b"\x00\x01\x02\x03\x04"
        rng = c.get("/api/video/vx", headers={"Range": "bytes=1-2"})   # seeking
        assert rng.status_code == 206 and rng.content == b"\x01\x02"


def test_all_items_feed(client):
    body = client.get("/api/all-items").json()
    assert body["layer"] == "all" and len(body["groups"]) == 1
    g = body["groups"][0]
    assert g["size"] == 5                                  # 3 photos + 2 videos
    assert len(g["photos"]) == 5                           # no params -> whole feed
    assert body["total"] == 5 and body["count"] == 5
    assert all(p["suggested_keep"] for p in g["photos"])   # everything kept by default
    assert any(p["is_video"] for p in g["photos"])         # videos included


def _paging_client():
    """Engine with 5 items at distinct, out-of-insertion-order timestamps so the
    chronological sort (and thus page windows) is observable."""
    recs = [mk("p2", timestamp=3000.0), mk("p0", timestamp=1000.0), mk("p1", timestamp=2000.0)]
    vids = [mkv("v1", timestamp=5000.0), mkv("v0", timestamp=4000.0)]
    eng = StubEngine(recs=recs, videos=vids)
    app = create_app(store=Store(":memory:"), engine=eng)
    return TestClient(app, base_url="http://127.0.0.1")


def test_all_items_pagination_windows():
    with _paging_client() as c:
        full = c.get("/api/all-items").json()
        order = [p["uuid"] for p in full["groups"][0]["photos"]]
        assert order == ["p0", "p1", "p2", "v0", "v1"]     # chronological
        # page 1
        r1 = c.get("/api/all-items", params={"offset": 0, "limit": 2}).json()
        g1 = r1["groups"][0]
        assert [p["uuid"] for p in g1["photos"]] == order[:2]
        assert r1["total"] == 5 and r1["count"] == 2 and r1["offset"] == 0
        assert g1["size"] == 5 and g1["total"] == 5        # size stays the TOTAL count
        # page 2 continues exactly where page 1 stopped (stable windows)
        r2 = c.get("/api/all-items", params={"offset": 2, "limit": 2}).json()
        assert [p["uuid"] for p in r2["groups"][0]["photos"]] == order[2:4]
        # short tail page
        r3 = c.get("/api/all-items", params={"offset": 4, "limit": 50}).json()
        assert [p["uuid"] for p in r3["groups"][0]["photos"]] == order[4:]
        assert r3["count"] == 1


def test_all_items_pagination_edges():
    with _paging_client() as c:
        beyond = c.get("/api/all-items", params={"offset": 99, "limit": 5}).json()
        assert beyond["groups"][0]["photos"] == [] and beyond["total"] == 5
        assert c.get("/api/all-items", params={"offset": -1}).status_code == 400
        assert c.get("/api/all-items", params={"limit": 0}).status_code == 400


def test_candidates_videos_have_video_flag(client):
    g = client.get("/api/candidates", params={"layer": "videos"}).json()["groups"][0]
    assert g["size"] == 2
    assert all(p["is_video"] for p in g["photos"])
    assert any(p["duration"] for p in g["photos"])


def test_unknown_layer_is_400(client):
    assert client.get("/api/candidates", params={"layer": "bogus"}).status_code == 400


def test_decisions_then_overlay(client):
    client.post("/api/decisions", json={
        "layer": "dedup",
        "decisions": [
            {"uuid": "a", "verdict": "keep", "group_key": "a", "suggested": True},
            {"uuid": "b", "verdict": "discard", "group_key": "a"},
        ],
    })
    g = client.get("/api/candidates", params={"layer": "dedup"}).json()["groups"][0]
    decided = {p["uuid"]: p["decided"] for p in g["photos"]}
    assert decided["a"] == "keep" and decided["b"] == "discard"


def test_thumb_404_when_missing(client):
    client.post("/api/analyze", json={"layers": ["dedup"]})  # indexes records
    assert client.get("/api/thumb/a").status_code == 404           # no image on disk
    assert client.get("/api/thumb/does-not-exist").status_code == 404


def test_delete_endpoint(client, monkeypatch):
    import photocleanup.delete as d
    monkeypatch.setattr(d, "delete_assets",
                        lambda uuids, dry_run=False: {"status": "ok", "requested": len(uuids),
                                                      "matched": len(uuids), "deleted": len(uuids)})
    r = client.post("/api/delete", json={"uuids": ["a", "b"]})
    assert r.status_code == 200 and r.json()["deleted"] == 2


def test_finalize_across_layers(client, monkeypatch):
    import photocleanup.learning as learning
    import photocleanup.server as server
    monkeypatch.setattr(learning, "write_dedup_feedback", lambda *a, **k: "/tmp/fake.json")
    monkeypatch.setattr(server, "_start_learning", lambda dbpath: True)  # don't touch the live library

    client.post("/api/decisions", json={"layer": "dedup", "decisions": [
        {"uuid": "a", "verdict": "keep"}, {"uuid": "b", "verdict": "discard"},
    ]})
    client.post("/api/decisions", json={"layer": "videos", "decisions": [
        {"uuid": "v1", "verdict": "keep"}, {"uuid": "v2", "verdict": "discard"},
    ]})
    r = client.post("/api/finalize", json={})  # all layers
    assert r.status_code == 200
    body = r.json()
    assert body["reviewed"] == 2                       # a, v1
    assert sorted(body["to_delete"]) == ["b", "v2"]
    assert body["feedback_log"] == "/tmp/fake.json"
    assert client.get("/api/health").json()["counts"]["reviewed"] == 2


def test_finalize_triggers_learning(client, monkeypatch):
    """When dedup labels are written, finalize kicks off a background retrain."""
    import photocleanup.learning as learning
    import photocleanup.server as server
    monkeypatch.setattr(learning, "write_dedup_feedback", lambda *a, **k: "/tmp/fake.json")
    calls = []
    monkeypatch.setattr(server, "_start_learning", lambda dbpath: calls.append(dbpath) or True)

    client.post("/api/decisions", json={"layer": "dedup", "decisions": [
        {"uuid": "a", "verdict": "keep"}, {"uuid": "b", "verdict": "discard"},
    ]})
    r = client.post("/api/finalize", json={"layers": ["dedup"]})
    assert r.json()["learning_started"] is True
    assert len(calls) == 1


def test_finalize_without_dedup_skips_learning(client, monkeypatch):
    """No dedup feedback -> no retrain triggered (learning_started is False)."""
    import photocleanup.server as server
    monkeypatch.setattr(server, "_start_learning", lambda dbpath: True)

    client.post("/api/decisions", json={"layer": "videos", "decisions": [
        {"uuid": "v1", "verdict": "keep"}, {"uuid": "v2", "verdict": "discard"},
    ]})
    r = client.post("/api/finalize", json={"layers": ["videos"]})
    assert r.json()["learning_started"] is False


def test_start_learning_is_serialized(monkeypatch):
    """Only one retrain runs at a time; a concurrent request is skipped."""
    import threading

    import photocleanup.learning as learning
    import photocleanup.server as server

    gate = threading.Event()
    started = []

    def fake_run(dbpath=None):
        started.append(dbpath)
        gate.wait(2.0)

    monkeypatch.setattr(learning, "run_learning", fake_run)

    assert server._start_learning(None) is True       # acquires the lock, spawns thread
    assert server._start_learning(None) is False       # busy -> skipped
    gate.set()
    server._learning_lock.acquire(timeout=2.0)          # wait for the thread to release
    server._learning_lock.release()
    assert started == [None]


def test_cancel_endpoint(client):
    r = client.post("/api/cancel")
    assert r.status_code == 200 and r.json()["cancelling"] is True


def test_donate_opens_kofi_without_network(client, monkeypatch):
    """The support button opens Ko-fi via `open` — never handles payment in-app."""
    import subprocess
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]))
    r = client.post("/api/donate").json()
    assert r["opened"] is True and "ko-fi.com/vaclavtrnka" in r["url"]
    assert calls and calls[0][0] == "open" and "ko-fi.com/vaclavtrnka" in calls[0][1]


# ---- finalize round-scoping (audit #2) -------------------------------------
def test_finalize_scopes_to_current_round_and_prunes(client, monkeypatch):
    """finalize must only act on decisions in the current candidates, and must
    prune them so a later round never re-deletes them."""
    import photocleanup.learning as learning
    import photocleanup.server as server
    monkeypatch.setattr(learning, "write_dedup_feedback", lambda *a, **k: "/tmp/fake.json")
    monkeypatch.setattr(server, "_start_learning", lambda dbpath: True)

    _run_analyze(client, ["dedup"])          # candidates: a (keep), b, c (discard)
    client.post("/api/decisions", json={"layer": "dedup", "decisions": [
        {"uuid": "a", "verdict": "keep"},
        {"uuid": "b", "verdict": "discard"},
        {"uuid": "c", "verdict": "discard"},
        {"uuid": "z", "verdict": "discard"},   # stale: a prior round, NOT in candidates
    ]})
    r = client.post("/api/finalize", json={"layers": ["dedup"]}).json()
    assert sorted(r["to_delete"]) == ["b", "c"]           # z is not re-deleted
    # second finalize must not re-emit b,c (their rows were pruned once acted on)
    r2 = client.post("/api/finalize", json={"layers": ["dedup"]}).json()
    assert r2["to_delete"] == []


# ---- cross-origin / CSRF guard (audit #4) ----------------------------------
def test_cross_origin_post_is_refused(client):
    r = client.post("/api/cancel", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_loopback_origin_post_is_allowed(client):
    r = client.post("/api/cancel", headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200


def test_post_without_origin_is_allowed(client):
    # our own WebView omits Origin for same-origin requests; must still work
    assert client.post("/api/cancel").status_code == 200


def test_get_is_not_origin_checked(client):
    r = client.get("/api/progress", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


# ---- scan/update mutual exclusion (audit #10 / #15) ------------------------
def test_analyze_refused_while_updating(client):
    client.app.state.update_job.clear()
    client.app.state.update_job.update({"status": "downloading"})
    r = client.post("/api/analyze", json={"layers": ["dedup"]}).json()
    assert r == {"started": False, "updating": True}


def test_update_refused_while_scanning(client):
    client.app.state.job.clear()
    client.app.state.job.update({"status": "running"})
    r = client.post("/api/update/apply").json()
    assert r == {"started": False, "scanning": True}   # returns before any network check


def test_finalize_writes_flat_feedback(client, monkeypatch, tmp_path):
    """Deciding on a flat layer (screenshots/expired) at finalize writes an
    explicit-labels feedback file and kicks off learning."""
    from photo_cleanup import feedback
    import photocleanup.server as server
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", str(tmp_path / "fb"))
    calls = []
    monkeypatch.setattr(server, "_start_learning", lambda dbpath: calls.append(1) or True)

    client.post("/api/decisions", json={"layer": "expired", "decisions": [
        {"uuid": "a", "verdict": "keep"}, {"uuid": "b", "verdict": "discard"},
    ]})
    r = client.post("/api/finalize", json={"layers": ["expired"]})
    assert r.json()["learning_started"] is True
    assert calls == [1]
    import glob
    import json as _json
    files = glob.glob(str(tmp_path / "fb" / "expired_*.json"))
    assert len(files) == 1
    d = _json.load(open(files[0]))
    assert d["kept"] == ["a"]
    assert {it["uuid"] for it in d["expired"]} == {"a", "b"}
