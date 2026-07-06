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
import tempfile
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

    def put(self, key: str, vec: np.ndarray) -> None:
        """Store a vector under an arbitrary key (e.g. video frame samples,
        keyed "uuid#f0"). No mtime tracking — callers own freshness."""
        self._vecs[key] = vec
        self._dirty = True

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
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        # Atomic write (audit #20): a crash / os._exit mid-write must never leave a
        # truncated cache (which would silently trigger a full multi-hour re-embed).
        # Write to a temp file in the same dir, then os.replace() into place.
        self._atomic_write(self.path, lambda fh: np.savez_compressed(fh, **self._vecs), d)
        self._atomic_write(self.path + ".mt.json",
                           lambda fh: fh.write(json.dumps(self._mt).encode()), d)
        self._dirty = False

    @staticmethod
    def _atomic_write(dest: str, write_fn, dirpath: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                write_fn(fh)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def __len__(self) -> int:
        return len(set(self._vecs) | self._file_keys)
