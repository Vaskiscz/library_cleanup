"""Stage 2 — write-back to Apple Photos via photoscript (local AppleScript).

Tags discard candidates with `cleanup:*` keywords and (optionally) Favorites the
keepers. Dry-run by default — nothing is written unless apply=True. Reversible
via undo_keywords(). Requires the Photos app and Automation permission; no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

KEYWORD_PREFIX = "cleanup:"
KW_SCREENSHOT = "cleanup:screenshot"
KW_DUPLICATE = "cleanup:duplicate"
KW_EXPIRED = "cleanup:expired"
KW_VIDEO = "cleanup:video"        # video review: near-dup takes + oversized (one tag)
# Permanent "reviewed & decided to keep" mark. Deliberately OUTSIDE the cleanup:
# namespace so undo/clear-tags never remove it. Photos with this are excluded
# from all future review passes.
KW_REVIEWED = "reviewed:keep"


@dataclass
class ApplyResult:
    tagged: int = 0
    favorited: int = 0
    skipped: int = 0      # already had the tag / nothing to do
    errors: int = 0
    first_error: Optional[str] = None   # reason for the first failure (diagnostics)

    def error(self, msg: str) -> None:
        """Count a per-photo failure, keeping the first reason for display."""
        self.errors += 1
        if self.first_error is None:
            self.first_error = msg


class NotAuthorizedError(RuntimeError):
    """Raised when macOS hasn't granted Automation access to Photos (-1743)."""


def _is_auth_error(e: Exception) -> bool:
    return "-1743" in str(e) or "Not authorized" in str(e)


def _library():
    from photoscript import PhotosLibrary
    lib = PhotosLibrary()
    # Probe authorization up front so we fail loudly, not silently per-photo.
    try:
        lib.photos  # noqa: B018 — deliberate probe; force a real Apple event:
        _ = len(list(lib.album_names()))
    except Exception as e:
        if _is_auth_error(e):
            raise NotAuthorizedError(str(e)) from e
        # other errors (e.g. Photos not running) are non-fatal for the probe
    return lib


def _get_photo(lib, uuid):
    try:
        return next(iter(lib.photos(uuid=[uuid])), None)
    except Exception:
        return None


def _photos_by_uuid(lib, uuids, chunk: int = 200) -> dict:
    """Fetch all photos in a few batched queries instead of one Apple-event per
    uuid — the dominant cost on large write batches. Returns {uuid: Photo}."""
    out = {}
    uuids = list(uuids)
    for i in range(0, len(uuids), chunk):
        batch = uuids[i:i + chunk]
        try:
            for p in lib.photos(uuid=batch):
                out[p.uuid] = p
        except Exception:
            for u in batch:                 # fall back to per-photo for this chunk
                p = _get_photo(lib, u)
                if p is not None:
                    out[u] = p
    return out


def add_keyword(
    uuids: Iterable[str],
    keyword: str,
    *,
    apply: bool = False,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ApplyResult:
    """Add `keyword` to each photo. Dry-run unless apply=True."""
    uuids = list(uuids)
    res = ApplyResult()
    if not apply:
        res.tagged = len(uuids)   # what WOULD be tagged
        return res

    lib = _library()
    photos = _photos_by_uuid(lib, uuids)
    for i, uuid in enumerate(uuids, 1):
        photo = photos.get(uuid)
        if photo is None:
            res.error(f"photo not found: {uuid}")
        else:
            try:
                kws = set(photo.keywords or [])
                if keyword in kws:
                    res.skipped += 1
                else:
                    photo.keywords = sorted(kws | {keyword})
                    res.tagged += 1
            except Exception as e:
                res.error(str(e))
        if progress:
            progress(i, len(uuids))
    return res


def favorite(
    uuids: Iterable[str],
    *,
    apply: bool = False,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ApplyResult:
    """Mark each photo as a Favorite (keepers). Dry-run unless apply=True."""
    uuids = list(uuids)
    res = ApplyResult()
    if not apply:
        res.favorited = len(uuids)
        return res

    lib = _library()
    photos = _photos_by_uuid(lib, uuids)
    for i, uuid in enumerate(uuids, 1):
        photo = photos.get(uuid)
        if photo is None:
            res.error(f"photo not found: {uuid}")
        else:
            try:
                if photo.favorite:
                    res.skipped += 1
                else:
                    photo.favorite = True
                    res.favorited += 1
            except Exception as e:
                res.error(str(e))
        if progress:
            progress(i, len(uuids))
    return res


def clear_keywords_for_uuids(
    uuids: Iterable[str],
    prefix: str = KEYWORD_PREFIX,
    *,
    apply: bool = False,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ApplyResult:
    """Remove `prefix` keywords from a SPECIFIC list of uuids. Write-only — does
    not read the library, so it runs from Terminal with only Photos automation
    (no Full Disk Access needed). Used by rescue and file-driven undo."""
    uuids = list(uuids)
    res = ApplyResult()
    if not apply:
        res.tagged = len(uuids)
        return res

    lib = _library()
    photos = _photos_by_uuid(lib, uuids)
    for i, uuid in enumerate(uuids, 1):
        photo = photos.get(uuid)
        if photo is None:
            res.error(f"photo not found: {uuid}")
        else:
            try:
                kws = photo.keywords or []
                kept = [k for k in kws if not k.startswith(prefix)]
                if len(kept) != len(kws):
                    photo.keywords = kept
                    res.tagged += 1
                else:
                    res.skipped += 1
            except Exception as e:
                res.error(str(e))
        if progress:
            progress(i, len(uuids))
    return res


def read_favorites(uuids, *, progress=None) -> list[str]:
    """Return the subset of uuids currently Favorited (live read via photoscript).
    Used to snapshot pre-existing favorites BEFORE the tool favorites keepers, so
    they can be preserved when un-favoriting later. Needs Photos automation only."""
    uuids = list(uuids)
    lib = _library()
    photos = _photos_by_uuid(lib, uuids)
    fav = []
    for i, uuid in enumerate(uuids, 1):
        photo = photos.get(uuid)
        if photo is not None:
            try:
                if photo.favorite:
                    fav.append(uuid)
            except Exception:
                pass
        if progress:
            progress(i, len(uuids))
    return fav


def unfavorite_uuids(
    uuids: Iterable[str],
    *,
    apply: bool = False,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ApplyResult:
    """Remove the Favorite flag from a specific list of uuids. Write-only."""
    uuids = list(uuids)
    res = ApplyResult()
    if not apply:
        res.favorited = len(uuids)
        return res

    lib = _library()
    photos = _photos_by_uuid(lib, uuids)
    for i, uuid in enumerate(uuids, 1):
        photo = photos.get(uuid)
        if photo is None:
            res.error(f"photo not found: {uuid}")
        else:
            try:
                if photo.favorite:
                    photo.favorite = False
                    res.favorited += 1
                else:
                    res.skipped += 1
            except Exception as e:
                res.error(str(e))
        if progress:
            progress(i, len(uuids))
    return res


def find_rescue_uuids(
    prefix: str = KEYWORD_PREFIX,
    *,
    use_favorites: bool = True,
    album: Optional[str] = None,
    dbpath: Optional[str] = None,
) -> list[str]:
    """Read the library for `prefix`-tagged photos the user flagged to keep —
    i.e. now Favorited, or placed in a `album`. These should be un-tagged."""
    import osxphotos
    db = osxphotos.PhotosDB(dbpath) if dbpath else osxphotos.PhotosDB()
    out = []
    for p in db.photos():
        try:
            if not any(k.startswith(prefix) for k in (p.keywords or [])):
                continue
            if use_favorites and p.favorite:
                out.append(p.uuid)
            elif album and album in (p.albums or []):
                out.append(p.uuid)
        except Exception:
            continue
    return out


def find_tagged_uuids(prefix: str = KEYWORD_PREFIX, dbpath: Optional[str] = None) -> list[str]:
    """Fresh read of the library for photos carrying any `prefix` keyword."""
    import osxphotos
    db = osxphotos.PhotosDB(dbpath) if dbpath else osxphotos.PhotosDB()
    out = []
    for p in db.photos():
        try:
            if any(k.startswith(prefix) for k in (p.keywords or [])):
                out.append(p.uuid)
        except Exception:
            continue
    return out


def undo_keywords(
    prefix: str = KEYWORD_PREFIX,
    *,
    apply: bool = False,
    dbpath: Optional[str] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ApplyResult:
    """Remove every keyword starting with `prefix` from all photos."""
    uuids = find_tagged_uuids(prefix, dbpath)
    res = ApplyResult()
    if not apply:
        res.tagged = len(uuids)   # what WOULD be cleared
        return res

    lib = _library()
    for i, uuid in enumerate(uuids, 1):
        photo = _get_photo(lib, uuid)
        if photo is None:
            res.error(f"photo not found: {uuid}")
        else:
            try:
                kws = [k for k in (photo.keywords or []) if not k.startswith(prefix)]
                photo.keywords = kws
                res.tagged += 1
            except Exception as e:
                res.error(str(e))
        if progress:
            progress(i, len(uuids))
    return res
