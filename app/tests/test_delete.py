"""Tests for the deletion path — the code that decides which PHAssets get
destroyed (audit #6, the highest-consequence untested path) and the Limited-
access handling (audit #3). Uses a fake PhotoKit so no real library is touched.
"""
import pytest

from photocleanup import delete

# Fake authorization constants (values only need to be internally consistent).
DENIED, LIMITED, AUTHORIZED, NOT_DETERMINED = 1, 4, 2, 0


class _FakeAsset:
    def __init__(self, local_id):
        self._id = local_id

    def localIdentifier(self):
        return self._id


class _FakeFetch:
    def __init__(self, assets):
        self._assets = list(assets)

    def count(self):
        return len(self._assets)

    def objectAtIndex_(self, i):
        return self._assets[i]


def _make_photos(local_ids, status=AUTHORIZED, ok=True, error=None):
    """Build a fake `Photos` module holding assets with the given
    localIdentifiers. Returns the fake; read fake._captured['deleted'] to see
    exactly what was handed to deleteAssets_ (None => never called)."""
    lib_assets = [_FakeAsset(lid) for lid in local_ids]
    captured = {"deleted": None}

    class PHPhotoLibrary:
        @staticmethod
        def authorizationStatusForAccessLevel_(_level):
            return status

        @staticmethod
        def requestAuthorizationForAccessLevel_handler_(_level, handler):
            handler(status)

        @staticmethod
        def sharedPhotoLibrary():
            class _Lib:
                def performChangesAndWait_error_(self, changes, _err):
                    changes()            # invokes PHAssetChangeRequest.deleteAssets_
                    return (ok, error)
            return _Lib()

    class PHAsset:
        @staticmethod
        def fetchAssetsWithLocalIdentifiers_options_(ids, _opts):
            want = set(ids)
            return _FakeFetch([a for a in lib_assets if a.localIdentifier() in want])

        @staticmethod
        def fetchAssetsWithOptions_(_opts):
            return _FakeFetch(lib_assets)

    class PHAssetChangeRequest:
        @staticmethod
        def deleteAssets_(assets):
            captured["deleted"] = list(assets)

    class P:
        pass
    P.PHAccessLevelReadWrite = 3
    P.PHAuthorizationStatusNotDetermined = NOT_DETERMINED
    P.PHAuthorizationStatusAuthorized = AUTHORIZED
    P.PHAuthorizationStatusLimited = LIMITED
    P.PHPhotoLibrary = PHPhotoLibrary
    P.PHAsset = PHAsset
    P.PHAssetChangeRequest = PHAssetChangeRequest
    P._captured = captured
    return P


@pytest.fixture
def patch_photos(monkeypatch):
    def _install(P):
        monkeypatch.setattr(delete, "_photos", lambda: P)
        return P
    return _install


def _deleted_ids(P):
    return sorted(a.localIdentifier().split("/")[0] for a in (P._captured["deleted"] or []))


# ---- the wrong-asset guarantee (#6) ----------------------------------------
def test_fast_path_deletes_exactly_requested(patch_photos):
    P = patch_photos(_make_photos([f"{u}/L0/001" for u in ("a", "b", "c")]))
    res = delete.delete_assets(["a", "b"])
    assert res["status"] == "ok" and res["deleted"] == 2
    assert _deleted_ids(P) == ["a", "b"]        # exactly the requested subset, not c


def test_enumeration_fallback_resolves_but_never_grabs_unrequested(patch_photos):
    # 'a' has a non-conventional local id (fast path misses -> enumeration),
    # and an unrequested 'x' also exists — it must NEVER be deleted.
    P = patch_photos(_make_photos(["a/L0/007", "x/L0/001"]))
    res = delete.delete_assets(["a"])
    assert res["status"] == "ok"
    assert _deleted_ids(P) == ["a"]             # x is left untouched


def test_deduped_and_order_preserved(patch_photos):
    P = patch_photos(_make_photos([f"{u}/L0/001" for u in ("a", "b")]))
    res = delete.delete_assets(["a", "a", "b", "a"])
    assert res["requested"] == 2 and _deleted_ids(P) == ["a", "b"]


def test_no_match_does_not_call_delete(patch_photos):
    P = patch_photos(_make_photos(["a/L0/001"]))
    res = delete.delete_assets(["zzz"])
    assert res["status"] == "no-match" and P._captured["deleted"] is None


def test_unauthorized_never_deletes(patch_photos):
    P = patch_photos(_make_photos(["a/L0/001"], status=DENIED))
    res = delete.delete_assets(["a"])
    assert res["status"] == "unauthorized" and P._captured["deleted"] is None


def test_dry_run_never_deletes(patch_photos):
    P = patch_photos(_make_photos([f"{u}/L0/001" for u in ("a", "b")]))
    res = delete.delete_assets(["a", "b"], dry_run=True)
    assert res["status"] == "ok" and res["matched"] == 2 and res["deleted"] == 0
    assert P._captured["deleted"] is None


def test_error_from_photokit_reports_error(patch_photos):
    patch_photos(_make_photos(["a/L0/001"], ok=False, error="user cancelled"))
    res = delete.delete_assets(["a"])
    assert res["status"] == "error" and res["deleted"] == 0


# ---- Limited access (#3) ---------------------------------------------------
def test_limited_with_unreachable_asset_is_access_limited(patch_photos):
    # Under Limited, only 'a' is in the selection; 'b' can't be reached.
    patch_photos(_make_photos(["a/L0/001"], status=LIMITED))
    res = delete.delete_assets(["a", "b"])
    assert res["status"] == "access-limited"     # NOT "ok"
    assert res["deleted"] == 1 and res["unmatched"] == ["b"]


def test_limited_but_all_reachable_is_ok(patch_photos):
    patch_photos(_make_photos([f"{u}/L0/001" for u in ("a", "b")], status=LIMITED))
    res = delete.delete_assets(["a", "b"])
    assert res["status"] == "ok" and res["deleted"] == 2


def test_limited_zero_match_is_access_limited_not_no_match(patch_photos):
    P = patch_photos(_make_photos(["a/L0/001"], status=LIMITED))
    res = delete.delete_assets(["zzz"])
    assert res["status"] == "access-limited" and P._captured["deleted"] is None
