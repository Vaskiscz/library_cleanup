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
