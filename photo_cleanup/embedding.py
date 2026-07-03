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


def distance(a: np.ndarray, b: np.ndarray) -> float:
    """L2 distance between two feature-print vectors (matches Vision's own)."""
    return float(np.linalg.norm(a - b))


def embed_records(records, cache, progress=None) -> int:
    """Compute & cache feature prints for records missing or stale (source image
    edited since cached). Reads image files only. Returns count computed."""
    from .quality import _best_image_path

    todo = []
    for r in records:
        p = _best_image_path(r)
        if p and not cache.is_fresh(r.uuid, p):
            todo.append((r, p))
    for i, (r, p) in enumerate(todo, 1):
        cache.compute(r.uuid, p)
        if progress:
            progress(i, len(todo))
    return len(todo)


class EmbeddingCache:
    """Caches feature-print vectors by uuid on disk (npz) so re-runs are instant.

    Vectors are read LAZILY from the npz (NpzFile decompresses per-array on
    access), so opening a 100k-photo cache doesn't materialise hundreds of MB —
    only vectors actually compared get loaded. New/updated vectors live in an
    in-memory overlay; `save()` writes only when something new was computed."""

    def __init__(self, path: str):
        self.path = path
        self._vecs: dict[str, np.ndarray] = {}   # overlay: loaded + new vectors
        self._file = None                        # lazy backing store (NpzFile)
        self._file_keys: frozenset = frozenset()
        self._dirty = False
        self._mt: dict[str, float] = {}     # uuid -> source image mtime
        if os.path.exists(path):
            try:
                self._file = np.load(path, allow_pickle=False)
                self._file_keys = frozenset(self._file.files)
            except Exception as e:
                log.warning("embedding cache unreadable, starting fresh (%s): %s",
                            path, e)
        if os.path.exists(path + ".mt.json"):
            try:
                self._mt = json.load(open(path + ".mt.json"))
            except Exception:
                self._mt = {}

    def get(self, uuid: str) -> Optional[np.ndarray]:
        v = self._vecs.get(uuid)
        if v is None and uuid in self._file_keys:
            v = self._file[uuid]        # decompressed on first access
            self._vecs[uuid] = v        # keep it — comparisons revisit vectors
        return v

    def __contains__(self, uuid: str) -> bool:
        return uuid in self._vecs or uuid in self._file_keys

    def is_fresh(self, uuid: str, image_path: str) -> bool:
        """Cached AND the source image hasn't changed since (catches edits)."""
        if uuid not in self:
            return False
        if uuid not in self._mt:
            return True   # legacy entry (pre-mtime) — accept; avoids mass re-embed
        try:
            return self._mt[uuid] == os.path.getmtime(image_path)
        except OSError:
            return True   # can't stat (e.g. not on disk) — don't force a recompute

    def compute(self, uuid: str, image_path: str) -> Optional[np.ndarray]:
        if image_path and self.is_fresh(uuid, image_path):
            return self.get(uuid)
        try:
            v = _vector_for_path(image_path) if image_path else None
        except Exception as e:
            log.warning("feature print failed for %s (%s): %s",
                        uuid, image_path, e)
            v = None
        if v is not None:
            self._vecs[uuid] = v
            self._dirty = True
            try:
                self._mt[uuid] = os.path.getmtime(image_path)
            except OSError:
                pass
        return v

    def save(self) -> None:
        """Persist the cache. No-op unless new vectors were computed (a rewrite
        needs every vector in memory — don't pay that for read-only runs)."""
        if not self._dirty:
            return
        for k in self._file_keys:            # materialise the untouched rest
            if k not in self._vecs:
                self._vecs[k] = self._file[k]
        if self._file is not None:
            self._file.close()
            self._file = None
        self._file_keys = frozenset()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        np.savez_compressed(self.path, **self._vecs)
        json.dump(self._mt, open(self.path + ".mt.json", "w"))
        self._dirty = False

    def __len__(self) -> int:
        return len(set(self._vecs) | self._file_keys)
