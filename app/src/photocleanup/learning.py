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


def run_learning(dbpath: Optional[str] = None) -> dict:
    """Retrain the keeper model + expired suppression from accumulated feedback.
    Reads the library (needs Full Disk Access) to know which photos still exist."""
    import osxphotos

    from photo_cleanup.feedback import learn_and_save, learn_expired
    db = osxphotos.PhotosDB(dbpath) if dbpath else osxphotos.PhotosDB()
    present = {p.uuid for p in db.photos()}
    return {"dedup": learn_and_save(present, dbpath),
            "expired": learn_expired(present)}
