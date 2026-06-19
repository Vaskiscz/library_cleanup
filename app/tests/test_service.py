import pytest
from fastapi.testclient import TestClient

from factories import make_stub_engine
from photocleanup.server import create_app
from photocleanup.store import Store


@pytest.fixture
def client():
    app = create_app(store=Store(":memory:"), engine=make_stub_engine())
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "dedup" in r.json()["layers"]


def test_scan_returns_scope(client):
    r = client.post("/api/scan", json={"since": "2024-01-01", "until": "2024-12-31"})
    assert r.status_code == 200
    assert r.json()["scanned"] == 3  # the three stubbed records


def test_candidates_dedup(client):
    r = client.get("/api/candidates", params={"layer": "dedup"})
    assert r.status_code == 200
    body = r.json()
    assert body["layer"] == "dedup" and len(body["groups"]) == 1
    g = body["groups"][0]
    assert g["size"] == 3
    assert all("decided" in p for p in g["photos"])  # overlay present (None here)


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
    # records are indexed but have no image on disk -> no thumbnail
    client.post("/api/scan", json={})
    assert client.get("/api/thumb/a").status_code == 404
    assert client.get("/api/thumb/does-not-exist").status_code == 404


def test_finalize_marks_reviewed_and_returns_discards(client, monkeypatch):
    # avoid touching the real library / feature store in the learning bridge
    import photocleanup.learning as learning
    monkeypatch.setattr(learning, "write_dedup_feedback", lambda *a, **k: "/tmp/fake.json")

    client.post("/api/decisions", json={
        "layer": "dedup",
        "decisions": [
            {"uuid": "a", "verdict": "keep", "group_key": "a"},
            {"uuid": "b", "verdict": "discard", "group_key": "a"},
            {"uuid": "c", "verdict": "discard", "group_key": "a"},
        ],
    })
    r = client.post("/api/finalize", json={"layer": "dedup"})
    assert r.status_code == 200
    body = r.json()
    assert body["reviewed"] == 1
    assert sorted(body["to_delete"]) == ["b", "c"]
    assert body["feedback_log"] == "/tmp/fake.json"
    # 'a' is now reviewed -> excluded from health reviewed count
    assert client.get("/api/health").json()["counts"]["reviewed"] == 1
