"""Shared test helpers (a plain module, not a conftest, so it doesn't clash
with the backend's tests/conftest.py). app/src is on sys.path via the root
pyproject's [tool.pytest.ini_options] pythonpath setting."""
from photo_cleanup.cluster import DuplicateGroup
from photo_cleanup.model import Record
from photocleanup.engine import Engine


def mk(uuid, **kw):
    """Minimal Record factory for tests (no library needed)."""
    base = dict(
        original_filename=f"{uuid}.jpg", path=None, timestamp=1000.0,
        latitude=None, longitude=None, width=4000, height=3000,
        is_photo=True, is_movie=False, is_screenshot=False, is_hidden=False,
        in_burst=False, favorite=False,
    )
    base.update(kw)
    return Record(uuid=uuid, **base)


def mkv(uuid, **kw):
    """Video Record factory."""
    return mk(uuid, original_filename=f"{uuid}.mov", is_photo=False, is_movie=True,
              width=1920, height=1080, duration=12.0, **kw)


class StubEngine(Engine):
    """Engine whose record loading + grouping are canned, so the service can be
    tested without a Photos library."""

    def __init__(self, recs=None, groups=None, videos=None, vgroups=None):
        super().__init__()
        self._recs = recs or []
        self._groups = groups or []
        self._videos = videos or []
        self._vgroups = vgroups or []

    def load_records(self, since=None, until=None, excluded=None, force_rescan=False, eligible_only=True):
        for r in self._recs:
            self._index[r.uuid] = r
        return list(self._recs)

    def load_videos(self, since=None, until=None, excluded=None, eligible_only=True):
        for r in self._videos:
            self._index[r.uuid] = r
        return list(self._videos)

    def dedup_groups(self, records, progress=None):
        return self._groups

    def video_groups(self, videos, progress=None):
        return self._vgroups

    def analyze(self, since=None, until=None, layers=None, excluded=None, progress=None):
        from photocleanup.engine import ALL_LAYERS
        layers = [l for l in (layers or ALL_LAYERS) if l in ALL_LAYERS]
        if progress:
            progress("Analyzing photos…", 1, 2)
            progress("Finishing up…", 2, 2)
        built = {
            "dedup": self.dedup_payload(self._groups),
            "videos": self.video_payload(self._vgroups),
            "screenshots": self.screenshot_payload([]),
            "expired": self.expired_payload([]),
        }
        self._candidates = {l: built[l] for l in layers}
        return {"since": since, "until": until,
                "summary": {l: self._summarize(self._candidates[l]) for l in layers}}


def make_stub_engine():
    """One photo burst (keep 'a', remove 'b','c') and one video set (keep 'v1',
    remove 'v2')."""
    a, b, c = mk("a"), mk("b"), mk("c")
    v1, v2 = mkv("v1"), mkv("v2")
    return StubEngine(
        recs=[a, b, c],
        groups=[DuplicateGroup(keepers=[a], discards=[b, c])],
        videos=[v1, v2],
        vgroups=[DuplicateGroup(keepers=[v1], discards=[v2])],
    )
