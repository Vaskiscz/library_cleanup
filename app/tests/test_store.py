import pytest

from photocleanup.store import DISCARD, KEEP, Store


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_record_and_read_decisions(store):
    n = store.record_decisions("dedup", [
        {"uuid": "a", "verdict": KEEP, "group_key": "g1", "suggested": True},
        {"uuid": "b", "verdict": DISCARD, "group_key": "g1", "suggested": False},
    ])
    assert n == 2
    decs = {d.uuid: d for d in store.decisions("dedup")}
    assert decs["a"].verdict == KEEP and decs["a"].suggested is True
    assert decs["b"].verdict == DISCARD and decs["b"].group_key == "g1"
    assert store.decided_uuids("dedup") == {"a", "b"}


def test_decision_upsert_overwrites(store):
    store.record_decisions("dedup", [{"uuid": "a", "verdict": KEEP}])
    store.record_decisions("dedup", [{"uuid": "a", "verdict": DISCARD}])
    decs = store.decisions("dedup")
    assert len(decs) == 1 and decs[0].verdict == DISCARD


def test_bad_verdict_rejected(store):
    with pytest.raises(ValueError):
        store.record_decisions("dedup", [{"uuid": "a", "verdict": "maybe"}])


def test_layers_are_isolated(store):
    store.record_decisions("dedup", [{"uuid": "a", "verdict": KEEP}])
    store.record_decisions("screenshots", [{"uuid": "a", "verdict": DISCARD}])
    assert store.decided_uuids("dedup") == {"a"}
    assert store.decided_uuids("screenshots") == {"a"}
    assert len(store.decisions()) == 2  # both layers


def test_reviewed_is_idempotent(store):
    store.mark_reviewed(["a", "b"], "dedup")
    store.mark_reviewed(["b", "c"], "dedup")
    assert store.reviewed_uuids() == {"a", "b", "c"}


def test_counts(store):
    store.record_decisions("dedup", [
        {"uuid": "a", "verdict": KEEP},
        {"uuid": "b", "verdict": DISCARD},
        {"uuid": "c", "verdict": DISCARD},
    ])
    store.mark_reviewed(["a"], "dedup")
    c = store.counts()
    assert c["decisions"]["dedup"] == {"keep": 1, "discard": 2}
    assert c["reviewed"] == 1


def test_persists_across_reopen(tmp_path):
    p = str(tmp_path / "state.db")
    s1 = Store(p)
    s1.record_decisions("dedup", [{"uuid": "a", "verdict": KEEP}])
    s1.mark_reviewed(["a"], "dedup")
    s1.close()
    s2 = Store(p)
    assert s2.decided_uuids("dedup") == {"a"}
    assert s2.reviewed_uuids() == {"a"}
    s2.close()
