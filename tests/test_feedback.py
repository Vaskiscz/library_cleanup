import json
import os

from photo_cleanup.feedback import KeeperModel, train, FEATURE_KEYS, default_weights

# Pairs where "kept" beats "discarded" by `sep` on a couple of keys.
def _pairs(n, sep=0.8):
    kept = {k: 0.0 for k in FEATURE_KEYS}
    disc = {k: 0.0 for k in FEATURE_KEYS}
    disc["failure"] = sep            # discarded photos have higher "failure"
    kept["pleasant_composition"] = sep
    return [(dict(kept), dict(disc)) for _ in range(n)]


def test_weights_are_clipped():
    m = KeeperModel()
    train(m, _pairs(2000, sep=0.9))   # strong signal would blow up if unclipped
    assert max(abs(w) for w in m.weights) <= 8.0 + 1e-6


def test_idempotent_from_seed():
    # training the same data twice from a fresh seed gives identical weights
    m1 = KeeperModel(); train(m1, _pairs(500))
    m2 = KeeperModel(); train(m2, _pairs(500))
    assert (m1.weights == m2.weights).all()


def test_confidence_scales_with_evidence():
    # The compounding lever: more accumulated pairs -> higher confidence weight,
    # so a preference repeated across reviews earns a progressively bigger shift.
    c100 = train(KeeperModel(), _pairs(100))["confidence"]
    c1000 = train(KeeperModel(), _pairs(1000))["confidence"]
    c5000 = train(KeeperModel(), _pairs(5000))["confidence"]
    assert c100 < c1000 < c5000 < 1.0


def test_low_confidence_stays_near_seed():
    # a tiny single review barely moves weights from the heuristic seed
    fi = FEATURE_KEYS.index("failure")
    seed = default_weights()[fi]
    m = KeeperModel(); train(m, _pairs(5, sep=0.05))   # almost no evidence
    assert abs(m.weights[fi] - seed) < 1.0


def test_empty_pairs_safe():
    m = KeeperModel()
    assert train(m, [])["pairs"] == 0


def test_gather_training_skips_flat_layer_logs(tmp_path, monkeypatch):
    """Dedup training must ignore expired_*/screenshots_* files — they're a
    different format and belong to the flat-layer suppression loop."""
    import json
    from photo_cleanup import feedback
    fb = tmp_path / "fb"; fb.mkdir()
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", str(fb))
    monkeypatch.setattr(feedback, "FEATURE_STORE", str(tmp_path / "store.json"))
    (fb / "screenshots_app_1.json").write_text(json.dumps(
        {"screenshots": [{"uuid": "s1", "kind": "words"}], "kept": []}))
    (fb / "expired_app_1.json").write_text(json.dumps(
        {"expired": [{"uuid": "e1", "kind": "wifi"}], "kept": []}))
    bursts, kept, store = feedback.gather_training(present_uuids=set())
    assert bursts == [] and kept == set()


# ---- JSON robustness: corrupt files quarantined, writes atomic -------------

def test_corrupt_model_quarantined_and_heuristic_fallback(tmp_path, monkeypatch):
    """A truncated keeper_model.json must not crash scoring: the file is set
    aside as .corrupt and keeper_score falls back to the heuristic."""
    from photo_cleanup import feedback
    from photo_cleanup.model import Config
    from photo_cleanup.quality import keeper_score
    from conftest import mk
    mp = tmp_path / "keeper_model.json"
    mp.write_text('{"keys": ["overall", "cur')   # truncated mid-write
    monkeypatch.setattr(feedback, "MODEL_PATH", str(mp))
    feedback.reset_model_cache()
    try:
        assert feedback.model_score({"overall": 1.0}) is None   # no model -> None
        rec = mk("r1", features={"overall": 1.0})
        assert keeper_score(rec, Config()) > 0   # heuristic path, no raise
    finally:
        feedback.reset_model_cache()   # don't leak the poisoned cache to other tests
    assert not mp.exists()
    assert (tmp_path / "keeper_model.json.corrupt").exists()


def test_corrupt_face_cache_quarantined(tmp_path, monkeypatch):
    from photo_cleanup import feedback
    fc = tmp_path / "face_quality.json"
    fc.write_text("not json at all")
    monkeypatch.setattr(feedback, "FACE_CACHE", str(fc))
    assert feedback.load_face_cache() == {}
    assert not fc.exists()
    assert (tmp_path / "face_quality.json.corrupt").exists()


def test_corrupt_feature_store_quarantined(tmp_path, monkeypatch):
    from photo_cleanup import feedback
    fs = tmp_path / "feature_store.json"
    fs.write_text('{"u1": {"overall"')
    monkeypatch.setattr(feedback, "FEATURE_STORE", str(fs))
    assert feedback.load_feature_store() == {}
    assert not fs.exists()
    assert (tmp_path / "feature_store.json.corrupt").exists()


def test_atomic_json_dump_replaces_only_on_success(tmp_path):
    import pytest
    from photo_cleanup.feedback import _atomic_json_dump
    p = tmp_path / "out.json"
    p.write_text('{"old": 1}')
    _atomic_json_dump({"new": 2}, str(p))
    assert json.loads(p.read_text()) == {"new": 2}
    # unserializable payload -> original intact, no temp leftover
    with pytest.raises(TypeError):
        _atomic_json_dump({"bad": object()}, str(p))
    assert json.loads(p.read_text()) == {"new": 2}
    assert not (tmp_path / "out.json.tmp").exists()


# ---- log filenames accumulate (no clobber) and stay discoverable ------------

def test_log_apply_iterations_accumulate(tmp_path, monkeypatch):
    """Two applies over the same (None, None) range must produce two files —
    the old fixed name overwrote the first iteration's training data — and
    gather_training must discover both alongside a legacy un-stamped file."""
    from photo_cleanup import feedback

    class R:
        def __init__(self, uuid):
            self.uuid = uuid
            self.features = {"overall": 1.0}

    class G:
        def __init__(self, keepers, discards):
            self.keepers, self.discards = keepers, discards

    fb = tmp_path / "fb"
    fb.mkdir()
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", str(fb))
    monkeypatch.setattr(feedback, "FEATURE_STORE", str(tmp_path / "store.json"))

    # legacy fixed-name file from an older version must still be discovered
    (fb / "applied_x_x.json").write_text(json.dumps({"bursts": [{"members": [
        {"uuid": "a", "features": {"overall": 1.0}},
        {"uuid": "b", "features": {"overall": 0.5}}],
        "suggested": ["a"]}]}))

    p1 = feedback.log_apply([G([R("c")], [R("d")])], None, None)
    p2 = feedback.log_apply([G([R("e")], [R("f")])], None, None)
    assert p1 != p2
    assert os.path.exists(p1) and os.path.exists(p2)

    bursts, kept, store = feedback.gather_training(present_uuids={"a", "c", "e"})
    assert len(bursts) == 3          # legacy + both new iterations
    assert kept == {"a", "c", "e"}


def test_log_flat_iterations_accumulate(tmp_path, monkeypatch):
    from photo_cleanup import feedback
    fb = tmp_path / "fb"
    fb.mkdir()
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", str(fb))
    p1 = feedback._log_flat("screenshots", [{"uuid": "s1", "kind": "words"}], None, None)
    p2 = feedback._log_flat("screenshots", [{"uuid": "s2", "kind": "words"}], None, None)
    assert p1 != p2 and os.path.exists(p1) and os.path.exists(p2)
    # _learn_flat's prefix glob picks up both stamped files
    res = feedback._learn_flat("screenshots", str(tmp_path / "corr.json"),
                               present_uuids={"s1"})
    assert res["types"] == {"words": 2}
