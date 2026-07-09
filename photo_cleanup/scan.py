"""Read the Apple Photos library (read-only) into Records via osxphotos.

This stage does NO image decoding — it only pulls metadata and Apple's
pre-computed on-device intelligence (OCR text, ML labels, aesthetic scores).
Fast even on large libraries.
"""

from __future__ import annotations

import os
from typing import Optional

from .model import Record


def _safe(fn, default=None):
    """Per-attribute fallback for osxphotos accessors. Intentionally silent:
    it's called dozens of times per photo and failures (attr absent in this
    library/osxphotos version) are expected — logging here would be pure noise.
    Real failures surface in the aggregate (empty scan) and via the callers
    that do log (embedding, feedback)."""
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
        camera_make=_safe(lambda: photo.exif_info.camera_make, "") or "",
        camera_model=_safe(lambda: photo.exif_info.camera_model, "") or "",
        duration=_safe(lambda: float(photo.exif_info.duration), None)
        or _safe(lambda: float(photo.duration), None),
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
    exclude_shared: bool = True,
    movies_only: bool = False,
    progress: Optional[callable] = None,
    db=None,
) -> list[Record]:
    """Open the Photos library and return Records. dbpath=None => system library.

    Excluded by default: the Hidden album (curated manually) and iCloud Shared
    Album assets (a separate namespace, not the user's main library — they don't
    appear in main-library Smart Albums and must not be tagged/deleted here).
    Set movies_only=True to scan videos instead of photos.

    `progress(done, total)` is called periodically while building records so the
    UI can show live motion through the read (the one part of the read we can
    count — the PhotosDB parse before this is opaque).

    `db` reuses an already-parsed osxphotos.PhotosDB instead of constructing a
    fresh one — the parse is 30–90s on a big library, so a caller scanning both
    photos AND videos should share one. It may also be a zero-arg factory
    returning a PhotosDB, invoked only when this call really reads (lets callers
    with a RAM memo pass a lazy provider without paying the parse up front).
    """
    import osxphotos  # imported lazily so --help etc. work without the library

    if callable(db):
        db = db()
    db = db or (osxphotos.PhotosDB(dbpath) if dbpath else osxphotos.PhotosDB())
    if movies_only:
        photos_iter = db.photos(images=False, movies=True)
        images_only = False
    else:
        photos_iter = db.photos()
    photos_iter = list(photos_iter)          # materialise so we have a total to report against
    total = len(photos_iter)
    records: list[Record] = []
    for i, photo in enumerate(photos_iter, 1):
        if images_only and not _safe(lambda: photo.isphoto, True):
            pass
        elif exclude_hidden and _safe(lambda: photo.hidden, False):
            pass
        elif exclude_shared and _safe(lambda: photo.shared, False):
            pass
        else:
            records.append(photo_to_record(photo))
        if progress is not None and (i % 200 == 0 or i == total):
            progress(i, total)
    return records


# ---- records (RAM-only; never persisted) -----------------------------------
# Scanned Records carry sensitive derived metadata (GPS coordinates, Apple Vision
# OCR text, filenames). We deliberately do NOT write them to disk (audit #8) —
# there is no records.json cache anymore. Records are memoized per PROCESS so
# repeated calls within one run don't re-scan; a fresh process (e.g. each CLI
# command) re-reads the library. (The expensive Vision embeddings remain cached
# in the sanctioned .npz — so this never causes a full re-analyze.)

def _db_mtime(dbpath: Optional[str] = None) -> Optional[float]:
    """Newest mtime of the Photos SQLite + its WAL/SHM — changes on any
    edit/favorite/delete. Used to invalidate the in-RAM records memo."""
    lib = dbpath or os.path.expanduser("~/Pictures/Photos Library.photoslibrary")
    dbdir = os.path.join(lib, "database")
    try:
        ms = [os.path.getmtime(os.path.join(dbdir, f)) for f in os.listdir(dbdir)
              if f.startswith("Photos.sqlite")]
        return max(ms) if ms else None
    except OSError:
        return None


_RAM_RECORDS: dict = {}          # per-process only; keyed by (dbpath, lib_mtime)


def records_ram(dbpath: Optional[str] = None, force: bool = False) -> list[Record]:
    """Photo Records for the library, held in RAM ONLY (never written to disk).
    Memoized per process and invalidated when the library changes."""
    mt = _db_mtime(dbpath)
    key = (dbpath or "", mt)
    if not force and mt is not None and key in _RAM_RECORDS:
        return _RAM_RECORDS[key]
    recs = scan_library(dbpath)
    if mt is not None:
        _RAM_RECORDS.clear()         # only keep the current library's records in RAM
        _RAM_RECORDS[key] = recs
    return recs
