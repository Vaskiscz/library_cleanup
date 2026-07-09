"""EmbeddingCache: lazy SQLite reads + pending-only save + eviction + thread-safe
writes + one-time migration from the legacy npz layout."""
import json
import os
import sqlite3

import numpy as np

from photo_cleanup.embedding import EmbeddingCache


def test_lazy_roundtrip_and_dirty_save(tmp_path):
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    assert len(c) == 0
    c.set("u1", np.ones(4, dtype=np.float32))     # no Vision in tests
    c.set("u2", np.zeros(4, dtype=np.float32))
    c.save()

    c2 = EmbeddingCache(p)          # fresh open: nothing materialised yet
    assert "u1" in c2 and "u2" in c2 and len(c2) == 2
    assert not c2._vecs             # lazy: overlay empty until vectors are read
    assert np.allclose(c2.get("u1"), np.ones(4))
    assert "u1" in c2._vecs and "u2" not in c2._vecs   # only the read one loaded
    assert c2.get("missing") is None


def test_save_is_noop_when_clean(tmp_path):
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    c.set("u1", np.ones(3, dtype=np.float32))
    c.save()
    mtime = (tmp_path / "emb.db").stat().st_mtime_ns

    c2 = EmbeddingCache(p)
    c2.get("u1")
    c2.save()                        # read-only session → must not rewrite
    assert (tmp_path / "emb.db").stat().st_mtime_ns == mtime


def test_read_only_session_creates_no_file(tmp_path):
    # Laziness extends to the file itself: a run that never stores anything
    # must leave no trace on disk (no empty .db from mere lookups).
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    assert c.get("u") is None
    assert "u" not in c
    assert len(c) == 0
    c.save()
    c.forget(["u"])
    assert not os.path.exists(p)


def test_dirty_save_preserves_unread_entries(tmp_path):
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    c.set("a", np.ones(2, dtype=np.float32))
    c.set("b", np.full(2, 5.0, dtype=np.float32))
    c.save()

    c2 = EmbeddingCache(p)           # add a vector WITHOUT reading "b"
    c2.set("c", np.zeros(2, dtype=np.float32))
    c2.save()
    assert "b" not in c2._vecs       # "b" was never materialised, just kept

    c3 = EmbeddingCache(p)
    assert {u for u in ("a", "b", "c") if u in c3} == {"a", "b", "c"}
    assert np.allclose(c3.get("b"), np.full(2, 5.0))


def test_save_upserts_only_pending_rows(tmp_path):
    # The point of audit #9: save() must touch only the rows changed since the
    # last save — never rewrite the table (the old npz recompressed everything).
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    for i in range(50):
        c.put(f"u{i}", np.full(4, float(i), dtype=np.float32))
    c.save()

    c2 = EmbeddingCache(p)
    c2.put("new", np.ones(4, dtype=np.float32))
    c2.save()
    assert c2._conn.total_changes == 1      # exactly the one pending row written
    assert set(c2._vecs) == {"new"}         # nothing else materialised into RAM
    assert len(EmbeddingCache(p)) == 51


def test_saved_cache_is_raw_float32_blobs(tmp_path):
    # audit #16 carried over: the one sanctioned on-disk artifact holds plain
    # float32 bytes — decodable with np.frombuffer, no pickle anywhere.
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    c.put("u1", np.array([1.0, 2.0, 3.0], dtype=np.float32))
    c.save()
    rows = sqlite3.connect(p).execute(
        "SELECT uuid, vec, mtime FROM embeddings").fetchall()
    assert [r[0] for r in rows] == ["u1"]
    assert isinstance(rows[0][1], bytes)
    assert np.frombuffer(rows[0][1], dtype=np.float32).tolist() == [1.0, 2.0, 3.0]
    assert rows[0][2] is None       # put() tracks no mtime — callers own freshness


def test_save_leaves_only_the_db(tmp_path):
    # The .mt.json sidecar and the temp-file dance are gone: mtimes live in the
    # rows and SQLite's journal handles crash atomicity (audit #20 intent).
    img = tmp_path / "img.jpg"; img.write_bytes(b"x")
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    c.set("u1", np.ones(3, dtype=np.float32), str(img))
    c.save()
    assert {f.name for f in tmp_path.iterdir()} == {"emb.db", "img.jpg"}
    assert EmbeddingCache(p).is_fresh("u1", str(img))


def test_set_records_mtime_like_compute(tmp_path):
    """set() (externally computed vector, e.g. from a worker thread) must leave
    the same freshness state compute() would — so the next scan's is_fresh check
    skips the decode."""
    img = tmp_path / "img.jpg"; img.write_bytes(b"x")
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    c.set("u1", np.ones(3, dtype=np.float32), str(img))
    assert c.is_fresh("u1", str(img))
    assert np.allclose(c.get("u1"), np.ones(3))
    c.save()
    assert EmbeddingCache(p).is_fresh("u1", str(img))    # mtime persisted too


def test_set_is_thread_safe(tmp_path):
    """Concurrent set()/put() from worker threads must not lose entries (the
    cache guards its state with an internal lock)."""
    from concurrent.futures import ThreadPoolExecutor
    c = EmbeddingCache(str(tmp_path / "emb.db"))
    n = 200

    def store(i):
        c.set(f"u{i}", np.full(4, float(i), dtype=np.float32))
        c.put(f"u{i}#f0", np.zeros(2, dtype=np.float32))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store, range(n)))
    assert len(c) == 2 * n
    assert all(f"u{i}" in c and f"u{i}#f0" in c for i in range(n))
    assert np.allclose(c.get("u137"), np.full(4, 137.0))


def test_embed_records_workers_applies_writes_on_coordinator(tmp_path, monkeypatch):
    """workers>1: vectors are computed in the pool but the cache writes (and
    progress calls) happen on the calling thread, in submission order."""
    import threading
    from photo_cleanup import embedding
    from conftest import mk

    imgs = []
    for i in range(5):
        f = tmp_path / f"i{i}.jpg"; f.write_bytes(b"x")
        imgs.append(str(f))
    recs = [mk(f"u{i}", path=imgs[i]) for i in range(5)]

    compute_threads = set()

    def fake_vec(path):
        compute_threads.add(threading.get_ident())
        return np.ones(3, dtype=np.float32)
    monkeypatch.setattr(embedding, "_safe_vector", fake_vec)

    c = EmbeddingCache(str(tmp_path / "emb.db"))
    ticks = []
    n = embedding.embed_records(recs, c, progress=lambda i, t: ticks.append((i, t)),
                                workers=4)
    assert n == 5
    assert ticks == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]    # ordered, from this thread
    assert all(c.is_fresh(f"u{i}", imgs[i]) for i in range(5))  # written with mtimes
    assert threading.get_ident() not in compute_threads          # decodes ran in workers


def test_forget_evicts_uuid_and_frame_keys(tmp_path):
    """forget() deletes rows for good — the npz cache grew forever (audit #9).
    Derived video-frame keys ("uuid#fN") go with their uuid."""
    img = tmp_path / "img.jpg"; img.write_bytes(b"x")
    p = str(tmp_path / "emb.db")
    c = EmbeddingCache(p)
    c.set("u1", np.ones(3, dtype=np.float32), str(img))
    c.put("u1#f0", np.zeros(2, dtype=np.float32))
    c.put("u1#f1", np.zeros(2, dtype=np.float32))
    c.set("u2", np.full(3, 2.0, dtype=np.float32))
    c.save()

    c.forget(["u1", None, "ghost"])          # falsy/unknown uuids are harmless
    assert "u1" not in c and "u1#f0" not in c and "u1#f1" not in c
    assert "u2" in c and len(c) == 1
    assert not c.is_fresh("u1", str(img))    # freshness state evicted too

    c2 = EmbeddingCache(p)                   # eviction was committed to disk
    assert "u1" not in c2 and "u1#f1" not in c2 and "u2" in c2 and len(c2) == 1

    # forgetting a pending (unsaved) key must not resurrect it on save
    c2.put("u3", np.ones(2, dtype=np.float32))
    c2.forget(["u3"])
    c2.save()
    assert "u3" not in EmbeddingCache(p)


def _write_legacy_npz(dirpath, name="embeddings"):
    npz = dirpath / f"{name}.npz"
    with open(npz, "wb") as fh:
        np.savez_compressed(fh, **{
            "u1": np.ones(4, dtype=np.float32),
            "u2": np.full(4, 7.0, dtype=np.float32),
            "v1#f0": np.zeros(4, dtype=np.float32),   # video-frame key
        })
    (dirpath / f"{name}.npz.mt.json").write_text(json.dumps({"u1": 123.0}))
    return npz


def test_migration_from_legacy_npz(tmp_path):
    """First open with no .db but a legacy npz sibling (the old default cache
    name) migrates everything — vectors, mtimes, video-frame keys — and leaves
    the legacy files untouched as a safety net."""
    npz = _write_legacy_npz(tmp_path)
    legacy_bytes = npz.read_bytes()

    c = EmbeddingCache(str(tmp_path / "embeddings.db"))
    assert len(c) == 3
    assert np.allclose(c.get("u2"), np.full(4, 7.0))
    assert np.allclose(c.get("v1#f0"), np.zeros(4))

    img = tmp_path / "img.jpg"; img.write_bytes(b"x")
    assert not c.is_fresh("u1", str(img))    # migrated mtime (123.0) mismatches
    assert c.is_fresh("u2", str(img))        # no sidecar mtime → legacy-accept

    assert npz.read_bytes() == legacy_bytes  # safety net untouched
    assert (tmp_path / "embeddings.npz.mt.json").exists()

    c2 = EmbeddingCache(str(tmp_path / "embeddings.db"))   # one-time: no re-migrate
    c2.forget(["u1"])
    assert len(EmbeddingCache(str(tmp_path / "embeddings.db"))) == 2
    assert npz.read_bytes() == legacy_bytes


def test_legacy_npz_path_redirects_to_db(tmp_path):
    """A pre-SQLite cache path (old default / scripted --emb-cache value ending
    in .npz) keeps working: the DB lands alongside and the npz stays intact."""
    npz = _write_legacy_npz(tmp_path)
    c = EmbeddingCache(str(npz))             # old-style path
    assert "u1" in c and len(c) == 3
    c.set("u9", np.ones(4, dtype=np.float32))
    c.save()
    assert (tmp_path / "embeddings.db").exists()
    assert np.load(str(npz), allow_pickle=False).files  # npz still a valid npz
    assert "u9" in EmbeddingCache(str(tmp_path / "embeddings.db"))
