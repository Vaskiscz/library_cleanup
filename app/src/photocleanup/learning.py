"""Bridge the app's explicit keep/discard decisions into the existing learning
pipeline. The CLI infers kept-vs-discarded from which photos survived deletion;
the app knows them explicitly, so it writes an "explicit-labels" feedback file
(the format photo_cleanup.feedback.gather_training already accepts).
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .store import KEEP, Store

# A stable filename: re-finalising overwrites it rather than accumulating logs.
_APP_DEDUP_LOG = "app_dedup_decisions.json"


def write_dedup_feedback(store: Store, dbpath: Optional[str] = None) -> Optional[str]:
    """Reconstruct bursts from stored dedup decisions and persist an
    explicit-labels feedback file (+ ensure member features are in the store).
    Returns the file path, or None if there's nothing learnable yet."""
    from photo_cleanup.feedback import (FEEDBACK_DIR, build_feature_store,
                                        load_feature_store, save_feature_store)

    by_group: dict[str, dict] = {}
    for d in store.decisions("dedup"):
        g = by_group.setdefault(d.group_key or "_", {"members": [], "kept": []})
        g["members"].append(d.uuid)
        if d.verdict == KEEP:
            g["kept"].append(d.uuid)

    bursts, kept = [], []
    for g in by_group.values():
        if len(g["members"]) < 2:   # a burst needs >=2 to yield a keep>discard pair
            continue
        bursts.append({"members": g["members"]})
        kept.extend(g["kept"])
    if not bursts:
        return None

    # Make sure every member's features exist in the persisted store, so they
    # survive even after the photo is deleted from the library.
    st = load_feature_store()
    missing = [u for b in bursts for u in b["members"] if u not in st]
    if missing:
        st.update(build_feature_store(missing, dbpath))
        save_feature_store(st)

    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    path = os.path.join(FEEDBACK_DIR, _APP_DEDUP_LOG)
    with open(path, "w") as f:
        json.dump({"kept": kept, "bursts": bursts}, f)
    return path


def write_flat_feedback(store: Store, layer: str, kind_map: dict) -> Optional[str]:
    """Persist a flat layer's explicit keep/remove verdicts for learning.
    `kind_map` (uuid -> triggering kind, from the analyze payload) names WHAT was
    flagged; the store's verdicts say what the user chose. Explicit labels avoid
    the CLI's presence-inference, which would race the deletion that follows."""
    import time

    from photo_cleanup.feedback import log_expired, log_screenshots

    decisions = store.decisions(layer)
    if not decisions:
        return None
    flagged = [(_FlatRec(d.uuid), _FlatVerdict(kind_map.get(d.uuid, "generic")))
               for d in decisions]
    kept = [d.uuid for d in decisions if d.verdict == KEEP]
    logger = log_screenshots if layer == "screenshots" else log_expired
    # Timestamped range key: each round accumulates (suppression wants history).
    return logger(flagged, "app", str(int(time.time())), kept=kept)


class _FlatRec:
    def __init__(self, uuid):
        self.uuid = uuid


class _FlatVerdict:
    def __init__(self, kind):
        self.kind = kind


def run_learning(dbpath: Optional[str] = None) -> dict:
    """Retrain the keeper model + flat-layer suppression from accumulated
    feedback. Reads the library (needs Full Disk Access) to know which photos
    still exist (used by CLI-era logs without explicit labels)."""
    import osxphotos

    from photo_cleanup.feedback import (learn_and_save, learn_expired,
                                        learn_screenshots, reset_model_cache)
    db = osxphotos.PhotosDB(dbpath) if dbpath else osxphotos.PhotosDB()
    present = {p.uuid for p in db.photos()}
    result = {"dedup": learn_and_save(present, dbpath),
              "expired": learn_expired(present),
              "screenshots": learn_screenshots(present)}
    # Drop in-process caches so the next round's suggestions use the new model.
    reset_model_cache()
    return result
