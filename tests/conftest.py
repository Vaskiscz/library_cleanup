"""Shared test helpers — synthetic Records + a fake embedding cache.

All tests run on in-memory data; no Photos library or osxphotos needed.
"""
import numpy as np
import pytest

from photo_cleanup.model import Record


def mk(uuid="x", **kw):
    """Build a Record with sensible defaults; override any field via kwargs."""
    base = dict(
        uuid=uuid, original_filename=f"{uuid}.jpg", path=None, timestamp=0.0,
        latitude=None, longitude=None, width=4000, height=3000,
        is_photo=True, is_movie=False, is_screenshot=False, is_hidden=False,
        in_burst=False, favorite=False,
    )
    base.update(kw)
    return Record(**base)


class FakeEmbeddings:
    """Stands in for EmbeddingCache: maps uuid -> vector via .get()."""
    def __init__(self, vecs):
        self._v = {u: np.asarray(v, dtype="float64") for u, v in vecs.items()}

    def get(self, uuid):
        return self._v.get(uuid)


@pytest.fixture
def emb():
    return FakeEmbeddings
