"""Read the Apple Photos library (read-only) into Records via osxphotos.

This stage does NO image decoding — it only pulls metadata and Apple's
pre-computed on-device intelligence (OCR text, ML labels, aesthetic scores).
Fast even on large libraries.
"""

from __future__ import annotations

import json
import os
from typing import Iterable, Optional

from .model import Record


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _extract_text(search_info) -> str:
    """Apple Vision OCR text, already stored in the library (no re-processing)."""
    if search_info is None:
        return ""
    dt = _safe(lambda: search_info.detected_text, None)
    if not dt:
        return ""
    if isinstance(dt, str):
        return dt
    # some versions return a list of strings / (text, conf) tuples
    parts = []
    for item in dt:
        if isinstance(item, (list, tuple)) and item:
            parts.append(str(item[0]))
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _extract_score(score) -> dict:
    if score is None:
        return {}
    g = lambda name: float(_safe(lambda: getattr(score, name), 0.0) or 0.0)
    return {
        "score_overall": g("overall"),
        "score_failure": g("failure"),
        "score_focus": g("sharply_focused_subject"),
        "score_noise": g("noise"),
        "score_low_light": g("low_light"),
    }


def photo_to_record(photo) -> Record:
    si = _safe(lambda: photo.search_info, None)
    ts = _safe(lambda: photo.date.timestamp(), None)
    score_obj = _safe(lambda: photo.score, None)
    scores = _extract_score(score_obj)
    from .feedback import features_from_scoreinfo
    feats = _safe(lambda: features_from_scoreinfo(score_obj, None), {}) or {}

    labels = _safe(lambda: list(si.labels), []) if si else []
    media_types = _safe(lambda: list(si.media_types), []) if si else []

    return Record(
        uuid=photo.uuid,
        original_filename=_safe(lambda: photo.original_filename, "") or "",
        path=_safe(lambda: photo.path, None),
        timestamp=ts,
        latitude=_safe(lambda: photo.latitude, None),
        longitude=_safe(lambda: photo.longitude, None),
        width=int(_safe(lambda: photo.width, 0) or 0),
        height=int(_safe(lambda: photo.height, 0) or 0),
        is_photo=bool(_safe(lambda: photo.isphoto, False)),
        is_movie=bool(_safe(lambda: photo.ismovie, False)),
        is_screenshot=bool(_safe(lambda: photo.screenshot, False)),
        is_hidden=bool(_safe(lambda: photo.hidden, False)),
        in_burst=bool(_safe(lambda: photo.burst, False)),
        favorite=bool(_safe(lambda: photo.favorite, False)),
        keywords=_safe(lambda: list(photo.keywords), []) or [],
        detected_text=_extract_text(si),
        labels=[str(x).lower() for x in (labels or [])],
        media_types=[str(x).lower() for x in (media_types or [])],
        features=feats,
        derivatives=_safe(lambda: list(photo.path_derivatives), []) or [],
        **scores,
    )


def scan_library(
    dbpath: Optional[str] = None,
    images_only: bool = True,
    exclude_hidden: bool = True,
) -> list[Record]:
    """Open the Photos library and return Records. dbpath=None => system library.

    Photos in the Hidden album are excluded by default — that album is curated
    manually and must never be auto-reviewed.
    """
    import osxphotos  # imported lazily so --help etc. work without the library

    db = osxphotos.PhotosDB(dbpath) if dbpath else osxphotos.PhotosDB()
    records: list[Record] = []
    for photo in db.photos():
        if images_only and not _safe(lambda: photo.isphoto, True):
            continue
        if exclude_hidden and _safe(lambda: photo.hidden, False):
            continue
        records.append(photo_to_record(photo))
    return records


# ---- cache -----------------------------------------------------------------

def save_records(records: Iterable[Record], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump([r.to_dict() for r in records], f)


def load_records(path: str) -> list[Record]:
    with open(path) as f:
        return [Record.from_dict(d) for d in json.load(f)]
