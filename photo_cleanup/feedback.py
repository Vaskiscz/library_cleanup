"""Learning engine: learn which frame to keep from the user's own choices.

Each review iteration produces ground truth — within every burst, the photos the
user KEPT should outrank the ones they DISCARDED. We train a small linear model
over Apple's ~27 on-device aesthetic sub-scores (the "small details": sharpness,
well-timed-shot, pleasant-composition, interesting-subject, …) plus measured
sharpness, using pairwise preference learning. The learned weights then drive
keeper_score, so suggestions improve every iteration.

Fully on-device: features are Apple's stored scores; training is plain numpy.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import numpy as np

log = logging.getLogger("photo_cleanup")

# Apple ScoreInfo sub-scores (the learnable "small details") + measured sharpness.
FEATURE_KEYS = [
    "overall", "curation", "promotion", "highlight_visibility", "behavioral",
    "failure", "harmonious_color", "immersiveness", "interaction",
    "interesting_subject", "intrusive_object_presence", "lively_color",
    "low_light", "noise", "pleasant_camera_tilt", "pleasant_composition",
    "pleasant_lighting", "pleasant_pattern", "pleasant_perspective",
    "pleasant_post_processing", "pleasant_reflection", "pleasant_symmetry",
    "sharply_focused_subject", "tastefully_blurred", "well_chosen_subject",
    "well_framed_subject", "well_timed_shot", "laplacian_norm",
    # Apple Vision per-face "best frame" score — captures eyes-open / smile /
    # sharp face / good expression: the small details that decide a burst.
    "face_capture_quality",
]

FACE_CACHE = os.path.expanduser("~/.cache/photo-cleanup/face_quality.json")


def face_capture_quality(path: str) -> float:
    """Max VNDetectFaceCaptureQuality over faces (0 if none). On-device."""
    try:
        import Vision
        from Foundation import NSURL
        url = NSURL.fileURLWithPath_(path)
        h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        req = Vision.VNDetectFaceCaptureQualityRequest.alloc().init()
        h.performRequests_error_([req], None)
        qs = [float(o.faceCaptureQuality() or 0.0) for o in (req.results() or [])
              if o.faceCaptureQuality() is not None]
        return max(qs) if qs else 0.0
    except Exception as e:
        log.debug("face capture quality failed for %s: %s", path, e)
        return 0.0

MODEL_PATH = os.path.expanduser("~/.cache/photo-cleanup/keeper_model.json")
FEATURE_STORE = os.path.expanduser("~/.cache/photo-cleanup/feature_store.json")
FEEDBACK_DIR = os.path.expanduser("~/.cache/photo-cleanup/feedback")


# ---- features -------------------------------------------------------------

def features_from_scoreinfo(score, laplacian: Optional[float]) -> dict:
    """Build a feature dict from an osxphotos ScoreInfo (+ measured sharpness)."""
    f = {}
    for k in FEATURE_KEYS:
        if k == "laplacian_norm":
            f[k] = min((laplacian or 0.0) / 200.0, 2.0)
        else:
            v = getattr(score, k, 0.0) if score is not None else 0.0
            f[k] = float(v or 0.0)
    return f


def vec(features: dict) -> np.ndarray:
    return np.array([float(features.get(k, 0.0)) for k in FEATURE_KEYS], dtype="float64")


# ---- feature store (uuid -> features), read from the library --------------

def _largest_path(photo):
    derivs = []
    try:
        derivs = list(photo.path_derivatives) or []
    except Exception:
        pass
    paths = [p for p in derivs if p and os.path.exists(p)]
    if paths:
        return max(paths, key=lambda p: os.path.getsize(p))
    try:
        return photo.path
    except Exception:
        return None


def build_feature_store(uuids, dbpath: Optional[str] = None, sharpness=None,
                        progress=None) -> dict:
    """Read Apple scores + (cached) face-capture-quality for the given uuids."""
    import osxphotos
    sharpness = sharpness or {}
    face_cache = json.load(open(FACE_CACHE)) if os.path.exists(FACE_CACHE) else {}
    want = set(uuids)
    db = osxphotos.PhotosDB(dbpath) if dbpath else osxphotos.PhotosDB()
    targets = [p for p in db.photos() if p.uuid in want]
    store = {}
    for i, p in enumerate(targets, 1):
        try:
            f = features_from_scoreinfo(p.score, sharpness.get(p.uuid))
            path = _largest_path(p)
            if path and not _face_fresh(face_cache, p.uuid, path):
                _face_set(face_cache, p.uuid, path)
            f["face_capture_quality"] = _face_quality_of(face_cache.get(p.uuid))
            store[p.uuid] = f
        except Exception as e:
            log.warning("feature store: skipping %s: %s", p.uuid, e)
        if progress:
            progress(i, len(targets))
    os.makedirs(os.path.dirname(FACE_CACHE), exist_ok=True)
    json.dump(face_cache, open(FACE_CACHE, "w"))
    return store


# Face-quality cache entries are [quality, source_mtime] so an edited photo is
# recomputed. Legacy entries are bare floats — accepted as-is (no edit check).
def _face_quality_of(entry) -> float:
    if isinstance(entry, list):
        return float(entry[0])
    return float(entry) if entry is not None else 0.0


def _face_fresh(cache: dict, uuid: str, path: str) -> bool:
    e = cache.get(uuid)
    if e is None:
        return False
    if not isinstance(e, list):
        return True   # legacy float entry — use it, don't force a recompute
    try:
        return e[1] == os.path.getmtime(path)
    except OSError:
        return True


def _face_set(cache: dict, uuid: str, path: str) -> None:
    set_face_quality(cache, uuid, path, face_capture_quality(path))


# Public face-cache API for callers that compute the quality themselves (the
# app's merged Vision pass gets it alongside the feature print — one decode —
# and stores it here so inject_face_quality later finds it fresh and skips).

def load_face_cache() -> dict:
    """The on-disk face-quality cache (uuid -> [quality, source_mtime])."""
    return json.load(open(FACE_CACHE)) if os.path.exists(FACE_CACHE) else {}


def save_face_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(FACE_CACHE), exist_ok=True)
    json.dump(cache, open(FACE_CACHE, "w"))


def face_quality_fresh(cache: dict, uuid: str, path: str) -> bool:
    """True when the cached quality is still valid for this source image."""
    return _face_fresh(cache, uuid, path)


def set_face_quality(cache: dict, uuid: str, path: str, quality: float) -> None:
    """Store an externally computed quality with the source mtime (same entry
    shape _face_set writes)."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = None
    cache[uuid] = [float(quality), mt]


def inject_face_quality(records, progress=None) -> None:
    """Ensure each record's features include face_capture_quality (cached by
    uuid+mtime, so edited photos are recomputed). Called before dedup so the
    learned model can use it for suggestions."""
    from .quality import _best_image_path
    face_cache = json.load(open(FACE_CACHE)) if os.path.exists(FACE_CACHE) else {}
    todo = [(r, _best_image_path(r)) for r in records]
    todo = [(r, p) for r, p in todo if p and not _face_fresh(face_cache, r.uuid, p)]
    for i, (r, p) in enumerate(todo, 1):
        _face_set(face_cache, r.uuid, p)
        if progress:
            progress(i, len(todo))
    for r in records:
        if isinstance(r.features, dict):
            r.features["face_capture_quality"] = _face_quality_of(face_cache.get(r.uuid))
    if todo:
        os.makedirs(os.path.dirname(FACE_CACHE), exist_ok=True)
        json.dump(face_cache, open(FACE_CACHE, "w"))


def load_feature_store() -> dict:
    if os.path.exists(FEATURE_STORE):
        with open(FEATURE_STORE) as f:
            return json.load(f)
    return {}


def save_feature_store(store: dict) -> None:
    os.makedirs(os.path.dirname(FEATURE_STORE), exist_ok=True)
    with open(FEATURE_STORE, "w") as f:
        json.dump(store, f)


# ---- model ----------------------------------------------------------------

def default_weights() -> np.ndarray:
    """Seed weights ~ the original hand-tuned keeper_score heuristic."""
    w = {k: 0.0 for k in FEATURE_KEYS}
    w["overall"] = 2.0
    w["sharply_focused_subject"] = 1.5
    w["failure"] = -2.0
    w["noise"] = -0.5
    w["low_light"] = -0.5
    w["laplacian_norm"] = 1.0
    w["face_capture_quality"] = 1.5   # Apple's best-frame-of-a-face prior
    return vec(w)


class KeeperModel:
    def __init__(self, weights: Optional[np.ndarray] = None, trained_pairs: int = 0):
        self.weights = default_weights() if weights is None else weights
        self.trained_pairs = trained_pairs

    def score(self, features: dict) -> float:
        return float(np.dot(self.weights, vec(features)))

    def save(self, path: str = MODEL_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"keys": FEATURE_KEYS, "weights": self.weights.tolist(),
                       "trained_pairs": self.trained_pairs}, f)

    @classmethod
    def load(cls, path: str = MODEL_PATH) -> Optional["KeeperModel"]:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            d = json.load(f)
        if d.get("keys") != FEATURE_KEYS:   # schema changed -> ignore stale model
            return None
        return cls(np.array(d["weights"], dtype="float64"), d.get("trained_pairs", 0))


# ---- training (pairwise logistic preference) ------------------------------

def train(model: KeeperModel, pairs: list[tuple[dict, dict]],
          epochs: int = 300, lr: float = 0.05, reg: float = 0.5,
          conf_scale: float = 2500.0, max_weight: float = 8.0) -> dict:
    """Each pair is (kept_features, discarded_features); learn score(kept)>score(disc).

    Philosophy: NEVER lurch on one review. Always retrain from the heuristic seed
    and regularize toward it; the data's pull is scaled by CONFIDENCE = how much
    total evidence has accumulated (N/(N+conf_scale)). So a single review only
    nudges, but a preference repeated across many reviews accumulates more pairs,
    raises confidence, and earns a bigger (still weight-clipped) shift."""
    if not pairs:
        return {"pairs": 0, "accuracy": None}
    X = np.array([vec(k) - vec(d) for k, d in pairs])  # want w·X > 0
    sd = X.std(0)
    sd[sd == 0] = 1.0
    Xs = X / sd
    w0 = model.weights * sd               # heuristic seed, standardized space
    base_acc = float((X @ model.weights > 0).mean())
    confidence = len(pairs) / (len(pairs) + conf_scale)   # 0..1, grows with evidence
    w = w0.copy()
    for _ in range(epochs):
        z = np.clip(Xs @ w, -30.0, 30.0)   # clip for numerical stability
        g = 1.0 / (1.0 + np.exp(z))        # sigmoid(-z)
        grad = -confidence * (Xs * g[:, None]).mean(0) + reg * (w - w0)
        w -= lr * grad
    model.weights = np.clip(w / sd, -max_weight, max_weight)
    acc = float((X @ model.weights > 0).mean())
    model.trained_pairs += len(pairs)
    return {"pairs": len(pairs), "accuracy": acc, "baseline_accuracy": base_acc,
            "confidence": confidence}


def pairs_from_bursts(bursts: list[dict], kept: set, store: dict,
                      max_per_burst: int = 12) -> list[tuple[dict, dict]]:
    """Within each burst, pair every kept member against every discarded one."""
    pairs = []
    for b in bursts:
        members = b["members"]
        k = [u for u in members if u in kept and u in store]
        d = [u for u in members if u not in kept and u in store]
        n = 0
        for ku in k:
            for du in d:
                pairs.append((store[ku], store[du]))
                n += 1
                if n >= max_per_burst:
                    break
            if n >= max_per_burst:
                break
    return pairs


# ---- scoring hook used by keeper_score ------------------------------------

EXPIRED_CORRECTIONS = os.path.expanduser("~/.cache/photo-cleanup/expired_corrections.json")
SCREENSHOT_CORRECTIONS = os.path.expanduser("~/.cache/photo-cleanup/screenshot_corrections.json")


def _log_flat(prefix: str, items: list[dict], since, until, kept=None) -> str:
    """Persist one iteration of a flat layer (expired/screenshots): each flagged
    photo's uuid + the kind that triggered it. `kept` (explicit uuids the user
    chose to keep — the app knows them) beats presence-inference at learn time."""
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    path = os.path.join(FEEDBACK_DIR, f"{prefix}_{since or 'x'}_{until or 'x'}.json")
    payload = {prefix: items}
    if kept is not None:
        payload["kept"] = sorted(kept)
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def log_expired(flagged, since, until, kept=None) -> str:
    """Record an expired iteration (see _log_flat)."""
    return _log_flat("expired", [{"uuid": r.uuid, "kind": v.kind} for r, v in flagged],
                     since, until, kept)


def log_screenshots(flagged, since, until, kept=None) -> str:
    """Record a screenshots iteration (see _log_flat)."""
    return _log_flat("screenshots",
                     [{"uuid": r.uuid, "kind": v.kind or "generic"} for r, v in flagged],
                     since, until, kept)


def _learn_flat(prefix: str, corrections_path: str, present_uuids: set) -> dict:
    """For every logged flag, outcome = removed (correct) or kept (false
    positive). Files with an explicit 'kept' list (written by the app, which
    knows the user's verdicts) use it; older CLI files infer kept = still in
    the library. Kinds the user keeps too often get suppressed from flagging."""
    import glob
    from collections import defaultdict
    kept = defaultdict(int)
    total = defaultdict(int)
    for fp in glob.glob(os.path.join(FEEDBACK_DIR, f"{prefix}_*.json")):
        try:
            d = json.load(open(fp))
        except Exception as e:
            log.warning("learn %s: unreadable feedback file %s: %s", prefix, fp, e)
            continue
        explicit = set(d["kept"]) if "kept" in d else None
        for it in d.get(prefix, []):
            k = it.get("kind", "generic")
            total[k] += 1
            was_kept = (it["uuid"] in explicit) if explicit is not None \
                else (it["uuid"] in present_uuids)   # survived review => false positive
            if was_kept:
                kept[k] += 1
    rates = {k: kept[k] / total[k] for k in total}
    # Suppress a type only with enough evidence and a clear majority kept.
    suppressed = sorted(k for k in total if total[k] >= 5 and rates[k] >= 0.6)
    os.makedirs(os.path.dirname(corrections_path), exist_ok=True)
    json.dump({"keep_rate": rates, "totals": dict(total), "suppressed": suppressed},
              open(corrections_path, "w"))
    return {"types": dict(total), "keep_rate": rates, "suppressed": suppressed}


def learn_expired(present_uuids: set) -> dict:
    return _learn_flat("expired", EXPIRED_CORRECTIONS, present_uuids)


def learn_screenshots(present_uuids: set) -> dict:
    return _learn_flat("screenshots", SCREENSHOT_CORRECTIONS, present_uuids)


_expired_suppressed = None
_screenshot_suppressed = None


def _suppressed_kinds(cache_name: str, path: str) -> set:
    g = globals()
    if g[cache_name] is None:
        try:
            g[cache_name] = set(json.load(open(path)).get("suppressed", []))
        except Exception:
            g[cache_name] = set()
    return g[cache_name]


def expired_suppressed_kinds() -> set:
    """Types the learning loop found you systematically keep — stop flagging them."""
    return _suppressed_kinds("_expired_suppressed", EXPIRED_CORRECTIONS)


def screenshot_suppressed_kinds() -> set:
    """Screenshot signals the learning loop found you systematically keep."""
    return _suppressed_kinds("_screenshot_suppressed", SCREENSHOT_CORRECTIONS)


def log_apply(groups, since, until) -> str:
    """Record a dedup --apply iteration so it can be learned from later (even
    after the user deletes discards): per burst, every member's features + the
    tool's suggested keepers. Kept-vs-discarded is recovered at learn time from
    which members still exist."""
    bursts = []
    for g in groups:
        members = [{"uuid": r.uuid, "features": r.features}
                   for r in g.keepers + g.discards]
        bursts.append({"members": members,
                       "suggested": [r.uuid for r in g.keepers]})
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    path = os.path.join(FEEDBACK_DIR, f"applied_{since or 'x'}_{until or 'x'}.json")
    with open(path, "w") as f:
        json.dump({"bursts": bursts}, f)
    return path


def gather_training(present_uuids: set, dbpath=None) -> tuple[list, set, dict]:
    """Read all feedback files into (bursts, kept_set, feature_store).

    - explicit-labels files (have a 'kept' list): trust it.
    - apply-log files: kept = members that still exist in the library now;
      missing members were discarded by the user.
    """
    import glob
    # Start from the persisted store so a photo's features survive its deletion.
    store = load_feature_store()
    bursts, kept, need, seen = [], set(), set(), set()
    # Process logs NEWEST-first; a photo re-reviewed in a newer log supersedes its
    # appearance in older logs (so re-deduping an event doesn't double-count it,
    # and the latest grouping/outcome wins).
    files = sorted((f for f in glob.glob(os.path.join(FEEDBACK_DIR, "*.json"))
                    if not os.path.basename(f).startswith(("expired_", "screenshots_"))),
                   key=os.path.getmtime, reverse=True)
    for fp in files:
        try:
            d = json.load(open(fp))
        except Exception as e:
            log.warning("gather_training: unreadable feedback file %s: %s", fp, e)
            continue
        labels = "kept" in d
        log_kept = set(d.get("kept", []))
        for b in d.get("bursts", []):
            raw = b["members"] if labels else [m["uuid"] for m in b["members"]]
            if not labels:   # stash features regardless (for older logs' photos too)
                for m in b["members"]:
                    if m.get("features") and m["uuid"] not in store:
                        store[m["uuid"]] = m["features"]
            fresh = [u for u in raw if u not in seen]   # not covered by a newer log
            seen.update(raw)
            if len(fresh) < 2:
                continue
            bursts.append({"members": fresh})
            need |= set(fresh)
            kept |= (set(fresh) & (log_kept if labels else present_uuids))
    missing = [u for u in need if u not in store]
    if missing:
        store.update(build_feature_store(missing, dbpath))
    save_feature_store(store)
    return bursts, kept, store


def learn_and_save(present_uuids: set, dbpath=None) -> dict:
    bursts, kept, store = gather_training(present_uuids, dbpath)
    pairs = pairs_from_bursts(bursts, kept, store)
    # Always retrain from the heuristic seed on ALL accumulated feedback, so
    # `learn` is idempotent — re-running can't compound the same data into
    # ever-larger weights (which it did before, blowing weights up to ±30).
    model = KeeperModel()
    metrics = train(model, pairs)
    if pairs:
        model.save()
    metrics["bursts"] = len(bursts)
    metrics["kept"] = len(kept)
    return metrics


_cached_model: Optional[KeeperModel] = None
_loaded = False


def reset_model_cache() -> None:
    """Drop the in-process caches so a freshly trained model (and refreshed
    suppression lists) take effect without restarting the process."""
    global _cached_model, _loaded, _expired_suppressed, _screenshot_suppressed
    _cached_model = None
    _loaded = False
    _expired_suppressed = None
    _screenshot_suppressed = None


def model_score(features: dict) -> Optional[float]:
    """Return the learned score for a feature dict, or None if no model/features."""
    global _cached_model, _loaded
    if not _loaded:
        _cached_model = KeeperModel.load()
        _loaded = True
    if _cached_model is None or not features:
        return None
    return _cached_model.score(features)
