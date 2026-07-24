"""Bridge the app's explicit keep/discard decisions into the existing learning
pipeline. The CLI infers kept-vs-discarded from which photos survived deletion;
the app knows them explicitly, so it writes an "explicit-labels" feedback file
(the format photo_cleanup.feedback.gather_training already accepts).
"""
from __future__ import annotations

import json
import os
from typing import Iterable, Optional

from .store import KEEP, Store

# Timestamped filenames, like the flat-layer logs: dedup labels must ACCUMULATE
# across rounds (finalize clears the store's decisions afterwards, so a stable
# name would overwrite the only copy of every previous round's training labels).
# gather_training globs every *.json in FEEDBACK_DIR except expired_*/
# screenshots_*, so each round's file keeps being read — including any legacy
# app_dedup_decisions.json left behind by older builds.
_APP_DEDUP_PREFIX = "app_dedup"


def write_dedup_feedback(store: Store, acted: Iterable[str],
                         dbpath: Optional[str] = None) -> Optional[str]:
    """Reconstruct bursts from this round's acted-on dedup decisions and persist
    an explicit-labels feedback file (+ ensure member features are in the store).
    `acted` scopes to the uuids finalize is clearing — stale rows from prior
    rounds must not be re-logged. Returns the file path, or None if there's
    nothing learnable yet."""
    import time

    from photo_cleanup.feedback import (FEEDBACK_DIR, build_feature_store,
                                        load_feature_store, save_feature_store)

    acted_set = set(acted)
    by_group: dict[str, dict] = {}
    for d in store.decisions("dedup"):
        if d.uuid not in acted_set:
            continue
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
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(FEEDBACK_DIR, f"{_APP_DEDUP_PREFIX}_{stamp}.json")
    n = 0
    while os.path.exists(path):   # same-second finalizes must not overwrite labels
        n += 1
        path = os.path.join(FEEDBACK_DIR, f"{_APP_DEDUP_PREFIX}_{stamp}-{n}.json")
    with open(path, "w") as f:
        json.dump({"kept": kept, "bursts": bursts}, f)
    return path


def write_flat_feedback(store: Store, layer: str, kind_map: dict,
                        acted: Iterable[str]) -> Optional[str]:
    """Persist a flat layer's explicit keep/remove verdicts for learning.
    `kind_map` (uuid -> triggering kind, from the analyze payload) names WHAT was
    flagged; the store's verdicts say what the user chose. `acted` scopes to the
    uuids finalize is clearing this round — logging ALL stored decisions would
    re-count a stale verdict once per round (and mislabel its kind as "generic"
    once it drops out of the payload). Explicit labels avoid the CLI's
    presence-inference, which would race the deletion that follows."""
    import time

    from photo_cleanup.feedback import log_expired, log_screenshots

    acted_set = set(acted)
    decisions = [d for d in store.decisions(layer) if d.uuid in acted_set]
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
