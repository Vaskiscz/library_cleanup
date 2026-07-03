"""EmbeddingCache: lazy npz reads + dirty-aware save."""
import numpy as np

from photo_cleanup.embedding import EmbeddingCache


def test_lazy_roundtrip_and_dirty_save(tmp_path):
    p = str(tmp_path / "emb.npz")
    c = EmbeddingCache(p)
    assert len(c) == 0
    c._vecs["u1"] = np.ones(4)      # stand-in for compute() (no Vision in tests)
    c._vecs["u2"] = np.zeros(4)
    c._dirty = True
    c.save()

    c2 = EmbeddingCache(p)          # fresh open: nothing materialised yet
    assert "u1" in c2 and "u2" in c2 and len(c2) == 2
    assert not c2._vecs             # lazy: overlay empty until vectors are read
    assert np.allclose(c2.get("u1"), np.ones(4))
    assert "u1" in c2._vecs and "u2" not in c2._vecs   # only the read one loaded
    assert c2.get("missing") is None


def test_save_is_noop_when_clean(tmp_path):
    p = str(tmp_path / "emb.npz")
    c = EmbeddingCache(p)
    c._vecs["u1"] = np.ones(3); c._dirty = True
    c.save()
    mtime = (tmp_path / "emb.npz").stat().st_mtime_ns

    c2 = EmbeddingCache(p)
    c2.get("u1")
    c2.save()                        # read-only session → must not rewrite
    assert (tmp_path / "emb.npz").stat().st_mtime_ns == mtime


def test_dirty_save_preserves_unread_entries(tmp_path):
    p = str(tmp_path / "emb.npz")
    c = EmbeddingCache(p)
    c._vecs.update({"a": np.ones(2), "b": np.full(2, 5.0)}); c._dirty = True
    c.save()

    c2 = EmbeddingCache(p)           # add a vector WITHOUT reading "b"
    c2._vecs["c"] = np.zeros(2); c2._dirty = True
    c2.save()

    c3 = EmbeddingCache(p)
    assert {u for u in ("a", "b", "c") if u in c3} == {"a", "b", "c"}
    assert np.allclose(c3.get("b"), np.full(2, 5.0))
