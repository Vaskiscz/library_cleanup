"""Quality signals for keeper ranking.

Blur is NEVER a reason to delete on its own — it only helps decide which
shot in a similar group is the keeper. Sharpness is measured locally with a
Laplacian-variance estimate (Pillow + scipy), combined with Apple's stored
aesthetic scores.
"""

from __future__ import annotations

from typing import Optional

from .model import Config, Record


def _best_image_path(rec: Record) -> Optional[str]:
    """Largest derivative — most reliable for sharpness (only used on near-dup
    groups, so cost is bounded). Falls back to the original."""
    if rec.derivatives:
        try:
            return max(rec.derivatives, key=lambda p: _file_size(p))
        except Exception:
            return rec.derivatives[0]
    return rec.path


def _fast_image_path(rec: Record) -> Optional[str]:
    """Smallest available image — enough for pHash (it downscales anyway) and
    much faster across a whole library."""
    candidates = [p for p in rec.derivatives if _file_size(p) > 0]
    if candidates:
        return min(candidates, key=_file_size)
    return rec.path


def _file_size(p: str) -> int:
    import os
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def laplacian_variance(path: str, max_dim: int = 1024) -> Optional[float]:
    """Variance of the Laplacian = focus/sharpness estimate. Higher = sharper.

    Runs entirely in local memory. Returns None if the image can't be read.
    """
    try:
        import numpy as np
        from PIL import Image
        from scipy import ndimage
    except Exception:
        return None

    try:
        with Image.open(path) as im:
            im = im.convert("L")
            im.thumbnail((max_dim, max_dim))
            arr = np.asarray(im, dtype="float64")
        if arr.size == 0:
            return None
        return float(ndimage.laplace(arr).var())
    except Exception:
        return None


def measure_sharpness(rec: Record) -> Optional[float]:
    """Compute and cache the Laplacian variance on the Record."""
    if rec.laplacian is not None:
        return rec.laplacian
    p = _best_image_path(rec)
    if not p:
        return None
    rec.laplacian = laplacian_variance(p)
    return rec.laplacian


def keeper_score(rec: Record, cfg: Config) -> float:
    """Higher = better keeper. Uses the LEARNED model when available (weights
    trained from the user's own keep/discard choices), else the hand-tuned
    heuristic below."""
    if rec.features:
        from .feedback import model_score
        feats = dict(rec.features)
        if rec.laplacian is not None:
            feats["laplacian_norm"] = min(rec.laplacian / 200.0, 2.0)
        learned = model_score(feats)
        if learned is not None:
            return learned + (1.0 if rec.favorite else 0.0)

    s = 0.0
    s += rec.score_overall * 2.0
    s += rec.score_focus * 1.5
    s -= rec.score_failure * 2.0
    s -= rec.score_noise * 0.5
    s -= rec.score_low_light * 0.5

    if rec.laplacian is not None:
        # gentle, saturating contribution so a single tack-sharp frame doesn't dominate
        s += min(rec.laplacian / 200.0, 1.5)

    if rec.favorite:
        s += 1.0  # user already signalled they value this one

    # mild resolution tiebreaker
    s += min((rec.width * rec.height) / 50_000_000.0, 0.5)
    return s
