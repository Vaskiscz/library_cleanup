"""Tests for the app -> learning-pipeline bridge (learning.py): dedup feedback
files must ACCUMULATE across rounds (finalize clears the store afterwards, so an
overwritten file would destroy the previous round's labels forever), and every
feedback writer must log only the round's acted-on decisions."""
import glob
import json
import os

import pytest

from photo_cleanup import feedback
from photocleanup import learning
from photocleanup.store import Store


@pytest.fixture
def fb_dir(monkeypatch, tmp_path):
    """Isolated feedback dir + a feature store that never touches the real
    library (write_dedup_feedback ensures member features exist)."""
    d = str(tmp_path / "fb")
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", d)
    monkeypatch.setattr(feedback, "load_feature_store", lambda: {})
    monkeypatch.setattr(feedback, "save_feature_store", lambda st: None)
    monkeypatch.setattr(feedback, "build_feature_store",
                        lambda uuids, dbpath=None: {u: {"f": 1.0} for u in uuids})
    return d


def _round(store, pairs):
    """Record one round of dedup decisions: [(uuid, verdict, group)]."""
    store.record_decisions("dedup", [
        {"uuid": u, "verdict": v, "group_key": g} for u, v, g in pairs])


def test_dedup_feedback_accumulates_across_rounds(fb_dir):
    """Each finalize writes a NEW timestamped file — a stable name would
    overwrite the only copy of the previous round's training labels."""
    store = Store(":memory:")
    _round(store, [("a", "keep", "g1"), ("b", "discard", "g1")])
    p1 = learning.write_dedup_feedback(store, ["a", "b"])
    store.clear_decisions("dedup", ["a", "b"])          # what finalize does

    _round(store, [("c", "keep", "g2"), ("d", "discard", "g2")])
    p2 = learning.write_dedup_feedback(store, ["c", "d"])

    assert p1 and p2 and p1 != p2                       # no overwrite (same-second safe)
    files = glob.glob(os.path.join(fb_dir, "app_dedup_*.json"))
    assert sorted(files) == sorted([p1, p2])
    assert json.load(open(p1))["kept"] == ["a"]         # round 1 labels survive round 2
    assert json.load(open(p2))["kept"] == ["c"]


def test_dedup_feedback_is_discovered_by_gather_training(fb_dir):
    """The timestamped filenames must match gather_training's discovery (every
    *.json except expired_*/screenshots_*), so all accumulated rounds train."""
    store = Store(":memory:")
    _round(store, [("a", "keep", "g1"), ("b", "discard", "g1")])
    learning.write_dedup_feedback(store, ["a", "b"])
    store.clear_decisions("dedup", ["a", "b"])
    _round(store, [("c", "keep", "g2"), ("d", "discard", "g2")])
    learning.write_dedup_feedback(store, ["c", "d"])

    bursts, kept, _ = feedback.gather_training(present_uuids=set())
    assert sorted(sorted(b["members"]) for b in bursts) == [["a", "b"], ["c", "d"]]
    assert kept == {"a", "c"}                            # explicit labels, both rounds


def test_dedup_feedback_only_from_acted_decisions(fb_dir):
    """Stale rows (not acted on this round) must not be logged — they'd be
    re-counted in every future round's file."""
    store = Store(":memory:")
    _round(store, [("a", "keep", "g1"), ("b", "discard", "g1"),
                   ("x", "discard", "g9"), ("y", "keep", "g9")])   # g9 is stale
    path = learning.write_dedup_feedback(store, ["a", "b"])
    d = json.load(open(path))
    assert d["kept"] == ["a"]
    assert [sorted(b["members"]) for b in d["bursts"]] == [["a", "b"]]


def test_flat_feedback_only_from_acted_decisions(fb_dir):
    store = Store(":memory:")
    store.record_decisions("screenshots", [
        {"uuid": "a", "verdict": "keep"},
        {"uuid": "b", "verdict": "discard"},
        {"uuid": "z", "verdict": "discard"},   # stale row from a prior round
    ])
    path = learning.write_flat_feedback(store, "screenshots",
                                        {"a": "code", "b": "chat"}, ["a", "b"])
    d = json.load(open(path))
    assert {it["uuid"] for it in d["screenshots"]} == {"a", "b"}   # z not re-counted
    assert d["kept"] == ["a"]


def test_flat_feedback_nothing_acted_writes_nothing(fb_dir):
    store = Store(":memory:")
    store.record_decisions("expired", [{"uuid": "z", "verdict": "discard"}])
    assert learning.write_flat_feedback(store, "expired", {}, []) is None
    assert glob.glob(os.path.join(fb_dir, "*.json")) == []
