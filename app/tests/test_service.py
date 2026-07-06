import time

import pytest
from fastapi.testclient import TestClient

from factories import make_stub_engine, mkv
from photocleanup.server import create_app
from photocleanup.store import Store


@pytest.fixture(autouse=True)
def _no_photo_prompt(monkeypatch):
    # analyze requests Photos access; never trigger a real prompt in tests
    monkeypatch.setattr("photocleanup.delete.ensure_access", lambda timeout=120.0: 3)


@pytest.fixture
def client():
    app = create_app(store=Store(":memory:"), engine=make_stub_engine())
    with TestClient(app) as c:
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


def test_analyze_summary(client):
    summary = _run_analyze(client, ["dedup", "videos", "expired"])
    assert summary["dedup"]["groups"] == 1 and summary["dedup"]["removable"] == 2
    assert summary["videos"]["groups"] == 1 and summary["videos"]["removable"] == 1
    assert summary["expired"]["items"] == 0  # stub records aren't expired


def test_rejects_foreign_host(client):
    # loopback Host is allowed (TestClient uses "testserver")...
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
    with TestClient(app) as c:
        r = c.get("/api/video/vx")
        assert r.status_code == 200 and r.content == b"\x00\x01\x02\x03\x04"
        rng = c.get("/api/video/vx", headers={"Range": "bytes=1-2"})   # seeking
        assert rng.status_code == 206 and rng.content == b"\x01\x02"


def test_all_items_feed(client):
    body = client.get("/api/all-items").json()
    assert body["layer"] == "all" and len(body["groups"]) == 1
    g = body["groups"][0]
    assert g["size"] == 5                                  # 3 photos + 2 videos
    assert all(p["suggested_keep"] for p in g["photos"])   # everything kept by default
    assert any(p["is_video"] for p in g["photos"])         # videos included


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
    import photocleanup.server as server
    eng = server  # silence linter
    r = client.post("/api/cancel")
    assert r.status_code == 200 and r.json()["cancelling"] is True


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
    import glob, json as _json
    files = glob.glob(str(tmp_path / "fb" / "expired_*.json"))
    assert len(files) == 1
    d = _json.load(open(files[0]))
    assert d["kept"] == ["a"]
    assert {it["uuid"] for it in d["expired"]} == {"a", "b"}
