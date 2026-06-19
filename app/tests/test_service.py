import time

import pytest
from fastapi.testclient import TestClient

from factories import make_stub_engine
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
    assert set(body["layers"]) == {"dedup", "videos", "screenshots", "expired"}


def test_analyze_summary(client):
    summary = _run_analyze(client, ["dedup", "videos", "expired"])
    assert summary["dedup"]["groups"] == 1 and summary["dedup"]["removable"] == 2
    assert summary["videos"]["groups"] == 1 and summary["videos"]["removable"] == 1
    assert summary["expired"]["items"] == 0  # stub records aren't expired


def test_analyze_rejects_bad_layer(client):
    assert client.post("/api/analyze", json={"layers": ["bogus"]}).status_code == 400


def test_candidates_dedup(client):
    body = client.get("/api/candidates", params={"layer": "dedup"}).json()
    assert body["layer"] == "dedup" and len(body["groups"]) == 1
    g = body["groups"][0]
    assert g["size"] == 3 and "title" in g
    assert all("decided" in p for p in g["photos"])


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
    monkeypatch.setattr(learning, "write_dedup_feedback", lambda *a, **k: "/tmp/fake.json")

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
