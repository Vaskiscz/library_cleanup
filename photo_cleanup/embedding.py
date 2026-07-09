"""On-device image embeddings via Apple's Vision framework feature prints.

`VNGenerateImageFeaturePrintRequest` is the same learned-similarity tech Photos
uses. It yields a 768-dim vector per image; L2 distance between vectors tracks
*content* similarity and is robust to reframing / angle / zoom changes — unlike a
perceptual hash, which keys off pixel layout.

100% on-device (Apple's local ML). No network, no model download, no uploads.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Optional

import numpy as np

log = logging.getLogger("photo_cleanup")


def _vector_for_path(path: str) -> Optional[np.ndarray]:
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(path)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    req = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
    ok, err = handler.performRequests_error_([req], None)
    res = req.results()
    if not res:
        return None
    obs = res[0]
    vec = np.frombuffer(bytes(obs.data()), dtype=np.float32)
    # normalize defensively (vectors are already ~unit, but be safe for L2)
    return vec.copy()


def _safe_vector(path: str) -> Optional[np.ndarray]:
    """_vector_for_path that never raises — worker threads report failures via
    None, and the coordinating thread just skips the cache write (same outcome
    as EmbeddingCache.compute's own except path)."""
    try:
        return _vector_for_path(path)
    except Exception as e:
        log.warning("feature print failed for %s: %s", os.path.basename(path or ""), e)
        return None


def feature_print_and_face_quality(path: str) -> tuple[Optional[np.ndarray], float]:
    """BOTH the feature print AND the face-capture quality from ONE
    VNImageRequestHandler pass — Vision decodes the image once and runs both
    requests over it, instead of two separate full-resolution decodes.

    Returns (vector-or-None, max face quality — 0.0 when no faces / on failure),
    matching _vector_for_path and feedback.face_capture_quality respectively.
    Never raises (safe to run inside worker threads)."""
    try:
        import Vision
        from Foundation import NSURL

        url = NSURL.fileURLWithPath_(path)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        fp = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
        fq = Vision.VNDetectFaceCaptureQualityRequest.alloc().init()
        handler.performRequests_error_([fp, fq], None)
        res = fp.results()
        vec = np.frombuffer(bytes(res[0].data()), dtype=np.float32).copy() if res else None
        qs = [float(o.faceCaptureQuality() or 0.0) for o in (fq.results() or [])
              if o.faceCaptureQuality() is not None]
        return vec, (max(qs) if qs else 0.0)
    except Exception as e:
        log.warning("merged Vision pass failed for %s: %s",
                    os.path.basename(path or ""), e)
        return None, 0.0


def distance(a: np.ndarray, b: np.ndarray) -> float:
    """L2 distance between two feature-print vectors (matches Vision's own)."""
    return float(np.linalg.norm(a - b))


def _vector_for_cgimage(img) -> Optional[np.ndarray]:
    import Vision
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    req = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
    handler.performRequests_error_([req], None)
    res = req.results()
    if not res:
        return None
    return np.frombuffer(bytes(res[0].data()), dtype=np.float32).copy()


def sample_video_frame_vectors(path: str, fractions=(0.15, 0.5, 0.85)) -> list:
    """Feature-print several frames of a video (AVFoundation frame grab →
    Vision, all on-device, nothing written to disk). Returns [] unless every
    requested frame could be sampled — callers fall back to the poster frame."""
    try:
        import AVFoundation
        from CoreMedia import CMTimeGetSeconds, CMTimeMakeWithSeconds
        from Foundation import NSURL
    except Exception as e:                       # bindings unavailable → poster fallback
        log.debug("AVFoundation unavailable (%s) — poster-frame fallback", e)
        return []
    try:
        asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(
            NSURL.fileURLWithPath_(path), None)
        gen = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
        gen.setAppliesPreferredTrackTransform_(True)
        dur = CMTimeGetSeconds(asset.duration())
        if not dur or dur <= 0:
            return []
        vecs = []
        for f in fractions:
            t = CMTimeMakeWithSeconds(dur * f, 600)
            img, _actual, err = gen.copyCGImageAtTime_actualTime_error_(t, None, None)
            v = _vector_for_cgimage(img) if img is not None else None
            if v is None:
                log.warning("frame sample failed for %s @%.0f%%: %s", os.path.basename(path), f * 100, err)
                return []
            vecs.append(v)
        return vecs
    except Exception as e:
        log.warning("frame sampling failed for %s: %s", os.path.basename(path), e)
        return []


def embed_records(records, cache, progress=None, workers: int = 1) -> int:
    """Compute & cache feature prints for records missing or stale (source image
    edited since cached). Reads image files only. Returns count computed.

    `workers > 1` decodes in a thread pool: pyobjc releases the GIL during the
    Vision call, so images embed truly in parallel. Vectors come back to THIS
    thread, which owns every cache write (and calls `progress`), so callers see
    the exact same ordering and cache semantics as the sequential path."""
    from .quality import _best_image_path

    todo = []
    for r in records:
        p = _best_image_path(r)
        if p and not cache.is_fresh(r.uuid, p):
            todo.append((r, p))
    if workers > 1 and len(todo) > 1:
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            vecs = pool.map(_safe_vector, [p for _, p in todo])
            for i, ((r, p), v) in enumerate(zip(todo, vecs, strict=True), 1):
                if v is not None:
                    cache.set(r.uuid, v, p)
                if progress:
                    progress(i, len(todo))
        finally:
            # On cancellation (progress raising) drop the queued decodes instead
            # of blocking until the whole backlog drains.
            pool.shutdown(wait=False, cancel_futures=True)
    else:
        for i, (r, p) in enumerate(todo, 1):
            cache.compute(r.uuid, p)
            if progress:
                progress(i, len(todo))
    return len(todo)


class EmbeddingCache:
    """Caches feature-print vectors by uuid in a SQLite file so re-runs are instant.

    One table: (uuid TEXT PRIMARY KEY, vec BLOB, mtime REAL) — vec is the raw
    float32 bytes of the vector, mtime the source image's mtime (NULL for keys
    without freshness tracking, e.g. video-frame samples "uuid#fN"). This
    replaced a compressed .npz + .mt.json sidecar (perf audit #9): the npz
    forced every dirty save() to materialise ALL vectors in RAM (~150 MB at
    50k photos) and recompress the whole archive — save() now upserts just the
    pending rows — and it had no eviction, so deleted photos' vectors piled up
    forever (see forget()).

    Vectors are read LAZILY (one row per get()), so opening a 100k-photo cache
    doesn't materialise hundreds of MB — only vectors actually compared get
    loaded. New/updated vectors live in an in-memory overlay until save();
    save() is a no-op when nothing changed, and a run that never stores
    anything creates no file at all.

    On first touch, a legacy npz cache (the old default name sibling, or the
    npz path itself if a pre-SQLite --emb-cache value is passed) is migrated
    into SQLite once; the legacy files stay on disk untouched as a safety net."""

    _SCHEMA = ("CREATE TABLE IF NOT EXISTS embeddings ("
               "uuid TEXT PRIMARY KEY, vec BLOB NOT NULL, mtime REAL)")

    def __init__(self, path: str):
        self.path = path
        if path.endswith(".npz"):
            # A legacy cache path (old default / scripted --emb-cache value):
            # keep the npz as the migration source, store the DB alongside it.
            self._db_path = path[: -len(".npz")] + ".db"
            self._legacy_path = path
        else:
            self._db_path = path
            self._legacy_path = os.path.splitext(path)[0] + ".npz"
        self._vecs: dict[str, np.ndarray] = {}   # overlay: loaded + new vectors
        self._mt: dict[str, float] = {}          # uuid -> source image mtime
        self._pending: set[str] = set()          # keys changed since last save()
        self._db_keys: set[str] = set()          # keys present in the DB file
        self._conn: Optional[sqlite3.Connection] = None
        # Guards ALL state including DB access: the connection is shared across
        # threads (check_same_thread=False) under single-writer discipline.
        # Makes get/put/set safe to call from worker threads.
        self._lock = threading.Lock()

    def _open(self, create: bool = False) -> Optional[sqlite3.Connection]:
        """Lazily open the DB (call with the lock held). Read paths pass
        create=False so a run that never stores anything leaves no file behind
        — the old laziness contract (unchanged cache => no write), except for
        the sanctioned one-time legacy migration."""
        if self._conn is not None:
            return self._conn
        fresh = not os.path.exists(self._db_path)
        if fresh and not create and not os.path.exists(self._legacy_path):
            return None                     # nothing on disk, nothing to write yet
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute(self._SCHEMA)
        except sqlite3.DatabaseError as e:
            # Same stance as the old npz open: unreadable cache -> start fresh
            # (worst case a re-embed) — but set the bad file aside rather than
            # clobbering it.
            conn.close()
            log.warning("embedding cache unreadable, starting fresh (%s): %s",
                        self._db_path, e)
            os.replace(self._db_path, self._db_path + ".corrupt")
            fresh = True
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute(self._SCHEMA)
        if fresh and os.path.exists(self._legacy_path):
            self._migrate_legacy(conn)
        conn.commit()                       # persist schema (+ any migration)
        # Key/mtime index up front (tiny — no vectors); vectors stay on disk
        # until get(). Overlay mtimes win: they are newer than the file's.
        for u, mt in conn.execute("SELECT uuid, mtime FROM embeddings"):
            self._db_keys.add(u)
            if mt is not None and u not in self._mt:
                self._mt[u] = mt
        self._conn = conn
        return conn

    def _migrate_legacy(self, conn: sqlite3.Connection) -> None:
        """One-time carry-over from the legacy cache layout (compressed .npz +
        .mt.json mtime sidecar) into SQLite. NpzFile decompresses per-array, so
        this streams one vector at a time — no RAM spike even at 100k photos.
        Video-frame keys ("uuid#fN") carry over like any other row (they never
        had sidecar mtimes -> NULL). The legacy files are left on disk
        untouched as a safety net; delete them once the .db has proven itself."""
        try:
            legacy = np.load(self._legacy_path, allow_pickle=False)
        except Exception as e:
            log.warning("legacy embedding cache unreadable, skipping migration "
                        "(%s): %s", self._legacy_path, e)
            return
        try:
            mt: dict[str, float] = {}
            if os.path.exists(self._legacy_path + ".mt.json"):
                try:
                    mt = json.load(open(self._legacy_path + ".mt.json"))
                except Exception:
                    mt = {}
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings (uuid, vec, mtime) VALUES (?, ?, ?)",
                ((k, np.asarray(legacy[k], dtype=np.float32).tobytes(), mt.get(k))
                 for k in legacy.files))
            conn.commit()
            log.info("migrated %d cached embeddings: %s -> %s",
                     len(legacy.files), self._legacy_path, self._db_path)
        except Exception as e:
            log.warning("legacy embedding cache migration failed (%s): %s",
                        self._legacy_path, e)
        finally:
            legacy.close()

    def get(self, uuid: str) -> Optional[np.ndarray]:
        with self._lock:
            v = self._vecs.get(uuid)
            if v is None:
                conn = self._open()
                if conn is not None and uuid in self._db_keys:
                    row = conn.execute("SELECT vec FROM embeddings WHERE uuid = ?",
                                       (uuid,)).fetchone()
                    if row is not None:
                        v = np.frombuffer(row[0], dtype=np.float32)
                        self._vecs[uuid] = v   # keep it — comparisons revisit vectors
            return v

    def __contains__(self, uuid: str) -> bool:
        with self._lock:
            self._open()
            return uuid in self._vecs or uuid in self._db_keys

    def is_fresh(self, uuid: str, image_path: str) -> bool:
        """Cached AND the source image hasn't changed since (catches edits)."""
        with self._lock:
            self._open()
            if uuid not in self._vecs and uuid not in self._db_keys:
                return False
            if uuid not in self._mt:
                return True   # legacy entry (pre-mtime) — accept; avoids mass re-embed
            try:
                return self._mt[uuid] == os.path.getmtime(image_path)
            except OSError:
                return True   # can't stat (e.g. not on disk) — don't force a recompute

    def put(self, key: str, vec: np.ndarray) -> None:
        """Store a vector under an arbitrary key (e.g. video frame samples,
        keyed "uuid#f0"). No mtime tracking — callers own freshness."""
        with self._lock:
            self._vecs[key] = vec
            self._pending.add(key)

    def set(self, uuid: str, vec: np.ndarray, image_path: Optional[str] = None) -> None:
        """Store an externally computed vector (e.g. from a worker thread's
        Vision pass) with the source mtime — exactly what compute() would have
        recorded. Thread-safe."""
        with self._lock:
            self._vecs[uuid] = vec
            self._pending.add(uuid)
            if image_path:
                try:
                    self._mt[uuid] = os.path.getmtime(image_path)
                except OSError:
                    pass

    def compute(self, uuid: str, image_path: str) -> Optional[np.ndarray]:
        if image_path and self.is_fresh(uuid, image_path):
            return self.get(uuid)
        try:
            v = _vector_for_path(image_path) if image_path else None
        except Exception as e:
            log.warning("feature print failed for %s (%s): %s",
                        uuid, os.path.basename(image_path or ""), e)
            v = None
        if v is not None:
            self.set(uuid, v, image_path)
        return v

    def save(self) -> None:
        """Commit pending vectors. Cheap: upserts ONLY the rows changed since
        the last save — the old npz rewrite materialised the entire cache in
        RAM and recompressed it (~100 MB of churn for one new photo). No-op
        when nothing changed. One transaction; SQLite's journal replaces the
        old temp-file + os.replace() atomicity trick (audit #20 still holds:
        a crash mid-save can never leave a truncated cache)."""
        with self._lock:
            if not self._pending:
                return
            conn = self._open(create=True)
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings (uuid, vec, mtime) VALUES (?, ?, ?)",
                [(k, np.asarray(self._vecs[k], dtype=np.float32).tobytes(),
                  self._mt.get(k)) for k in self._pending])
            conn.commit()
            self._db_keys.update(self._pending)
            self._pending.clear()

    def forget(self, uuids) -> None:
        """Evict vectors of deleted assets — each uuid plus its derived
        video-frame keys ("uuid#fN"). The npz predecessor had no eviction at
        all, so the cache grew forever. Commits immediately: eviction follows
        a delete, which is rare and already disk-bound."""
        drop = {u for u in uuids if u}
        if not drop:
            return
        prefixes = tuple(u + "#" for u in drop)
        with self._lock:
            for d in (self._vecs, self._mt):
                for k in [k for k in d if k in drop or k.startswith(prefixes)]:
                    del d[k]
            doomed = {k for k in self._pending | self._db_keys
                      if k in drop or k.startswith(prefixes)}
            self._pending -= doomed
            self._db_keys -= doomed
            conn = self._open()      # no create: nothing on disk -> nothing to delete
            if conn is not None:
                # LIKE is safe here: Photos uuids are hex+dashes, never % or _.
                conn.executemany(
                    "DELETE FROM embeddings WHERE uuid = ? OR uuid LIKE ? || '#%'",
                    [(u, u) for u in drop])
                conn.commit()

    def __len__(self) -> int:
        with self._lock:
            self._open()
            return len(set(self._vecs) | self._db_keys)
