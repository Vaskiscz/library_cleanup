"""Delete assets from the Photos library via PhotoKit (pyobjc).

This is the only place the app removes anything. Deletion goes through
``PHPhotoLibrary.performChangesAndWait`` which makes macOS show its OWN
confirmation dialog and routes deletions to Recently Deleted (recoverable for
30 days). Everything stays on-device.

osxphotos exposes a photo's UUID; PhotoKit identifies assets by
``localIdentifier`` ("<uuid>/L0/001"). We resolve UUIDs to PHAssets with the
conventional suffix first, then fall back to a full enumeration for any that
don't match, so we never silently miss a deletion.
"""
from __future__ import annotations

import threading
from typing import Iterable


def _photos():
    import Photos  # pyobjc-framework-Photos (bundled)
    return Photos


def authorization_status() -> int:
    """Current Photos read-write authorization (non-prompting)."""
    P = _photos()
    return int(P.PHPhotoLibrary.authorizationStatusForAccessLevel_(P.PHAccessLevelReadWrite))


def ensure_access(timeout: float = 120.0) -> int:
    """Public entry point: prompt for Photos access if not yet decided. Safe to
    call before scanning so deletion later is seamless; returns the status int."""
    return _ensure_authorized(timeout)


def _ensure_authorized(timeout: float = 120.0) -> int:
    P = _photos()
    status = authorization_status()
    if status == P.PHAuthorizationStatusNotDetermined:
        ev, box = threading.Event(), {}

        def handler(s):
            box["s"] = int(s)
            ev.set()

        P.PHPhotoLibrary.requestAuthorizationForAccessLevel_handler_(
            P.PHAccessLevelReadWrite, handler)
        ev.wait(timeout)
        status = box.get("s", status)
    return int(status)


def _resolve_assets(uuids: list[str]):
    """Map UUIDs -> PHAssets. Returns (assets, matched_uuids)."""
    P = _photos()
    by_uuid = {}
    # 1) fast path: conventional local identifiers
    ids = [f"{u}/L0/001" for u in uuids]
    fetch = P.PHAsset.fetchAssetsWithLocalIdentifiers_options_(ids, None)
    for i in range(fetch.count()):
        a = fetch.objectAtIndex_(i)
        by_uuid[a.localIdentifier().split("/")[0]] = a
    # 2) fallback: enumerate everything for any UUID still unmatched
    missing = [u for u in uuids if u not in by_uuid]
    if missing:
        want = set(missing)
        allp = P.PHAsset.fetchAssetsWithOptions_(None)
        for i in range(allp.count()):
            a = allp.objectAtIndex_(i)
            uid = a.localIdentifier().split("/")[0]
            if uid in want:
                by_uuid[uid] = a
                want.discard(uid)
                if not want:
                    break
    return [by_uuid[u] for u in uuids if u in by_uuid], set(by_uuid)


def delete_assets(uuids: Iterable[str], dry_run: bool = False) -> dict:
    """Delete the given library assets (or just resolve them if dry_run).

    Returns {status, requested, matched, deleted, [unmatched], [error]}.
    status: 'ok' | 'unauthorized' | 'no-match' | 'error' | 'cancelled'.
    """
    uuids = list(dict.fromkeys(uuids))  # de-dupe, keep order
    P = _photos()

    status = _ensure_authorized()
    if status not in (P.PHAuthorizationStatusAuthorized, P.PHAuthorizationStatusLimited):
        return {"status": "unauthorized", "auth": int(status),
                "requested": len(uuids), "matched": 0, "deleted": 0}

    assets, matched = _resolve_assets(uuids)
    unmatched = [u for u in uuids if u not in matched]
    if dry_run:
        return {"status": "ok" if assets else "no-match", "dry_run": True,
                "requested": len(uuids), "matched": len(assets), "deleted": 0,
                "unmatched": unmatched}
    if not assets:
        return {"status": "no-match", "requested": len(uuids), "matched": 0,
                "deleted": 0, "unmatched": unmatched}

    lib = P.PHPhotoLibrary.sharedPhotoLibrary()

    def changes():
        P.PHAssetChangeRequest.deleteAssets_(assets)

    ok, error = lib.performChangesAndWait_error_(changes, None)
    if not ok:
        # user pressing "Don't Allow"/Cancel surfaces here as an error too
        return {"status": "error", "requested": len(uuids), "matched": len(assets),
                "deleted": 0, "unmatched": unmatched, "error": str(error)}
    return {"status": "ok", "requested": len(uuids), "matched": len(assets),
            "deleted": len(assets), "unmatched": unmatched}
