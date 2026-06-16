"""Data model: a serializable Record per photo, decoupled from osxphotos, plus Config.

We extract everything we need from osxphotos into plain dataclasses so the rest of
the pipeline (and the on-disk cache) never depends on the library internals.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Config:
    """Tunable thresholds. Defaults lean conservative — the guiding rule is
    *when in doubt, keep*. Singletons (one-of-a-kind photos) are never flagged."""

    # --- clustering (group shots from the same moment/place) ---
    cluster_gap_seconds: float = 90.0      # new cluster when time gap exceeds this
    cluster_gps_meters: float = 50.0       # ...or location jumps more than this

    # --- near-duplicate confirmation within a cluster ---
    # Primary method: Apple Vision feature-print embeddings (content similarity,
    # robust to reframing/angle). L2 distance <= threshold => "same shot".
    embedding_max_distance: float = 0.25   # calibrated on real wedding bursts
    keepers_per_group: int = 3             # (legacy/pHash path) fixed keepers

    # Adaptive keepers: burst size signals how much the moment mattered.
    #   (max_burst_size, keepers) tiers, then keepers_max above the last tier.
    keeper_tiers: tuple = ((3, 1), (9, 2), (19, 3))
    keepers_max: int = 4
    # A second keeper must be at least this far (embedding L2) from every already
    # chosen keeper — ensures variety (different expression/pose), not near-dupes.
    keeper_diversity_min: float = 0.12
    # Legacy perceptual-hash fallback (pixel-layout; used only if no embeddings)
    phash_size: int = 16
    phash_max_distance: int = 12

    # --- work-screenshot classifier (CONTENT-BASED) ---
    # Decision reads the OCR text: work lexicon vs private lexicon (see lexicon.py).
    # Keep-bias: only propose removal when work clearly outweighs private.
    work_min_score: int = 3                 # min work score required to flag
    # fallback tier: an impersonal text document (no personal markers, no
    # picture content) reading this densely is treated as a work document.
    doc_fallback_min_chars: int = 120
    doc_fallback_min_words: int = 25
    enable_doc_fallback: bool = True        # set False for max-conservative mode
    # Apple data/graphic labels that are unambiguous work artifacts (weight 2).
    # NOTE: deliberately excludes 'document'/'printed page' — those also cover
    # private chats and notes, so they are NOT a removal signal on their own.
    work_labels: tuple[str, ...] = (
        "chart", "plot", "table", "spreadsheet", "diagram",
        "code", "computer program",
    )
    # Labels that rescue a screenshot regardless of how much text it has — a
    # person, pet, food, meme, scenery, etc. Keep-bias: any of these wins.
    keep_labels: tuple[str, ...] = (
        # people (protects precious chats with people, gym/people photos, selfies)
        "face", "person", "people", "selfie", "crowd",
        # animals & pets
        "pet", "dog", "cat", "animal", "mammal",
        # food
        "food", "meal",
        # scenery / nature
        "landscape", "beach", "mountain", "sky", "outdoor", "plant", "flower",
        # memes / illustrations / art (Apple labels memes as these)
        "meme", "cartoon", "clip art", "comics", "comic", "illustration",
        "drawing", "art", "sticker", "emoji",
    )

    # --- quality / keeper ranking (blur is only a tiebreaker, never a delete reason) ---
    laplacian_blur_floor: float = 40.0     # below this variance => visibly soft

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Record:
    """Flat, JSON-serializable view of one photo."""

    uuid: str
    original_filename: str
    path: Optional[str]                     # original on disk (None if not local)
    timestamp: Optional[float]              # epoch seconds
    latitude: Optional[float]
    longitude: Optional[float]
    width: int
    height: int

    is_photo: bool
    is_movie: bool
    is_screenshot: bool                     # Apple's own flag
    is_hidden: bool                         # in the Hidden album (never auto-reviewed)
    in_burst: bool
    favorite: bool
    keywords: list[str] = field(default_factory=list)

    # Apple's on-device intelligence (no decoding, no network)
    detected_text: str = ""                 # Apple Vision OCR text
    labels: list[str] = field(default_factory=list)  # Apple ML scene labels
    media_types: list[str] = field(default_factory=list)

    # Apple's stored aesthetic / quality scores (may be 0.0 if not computed)
    score_overall: float = 0.0
    score_failure: float = 0.0
    score_focus: float = 0.0                # sharply_focused_subject
    score_noise: float = 0.0
    score_low_light: float = 0.0

    # paths to existing thumbnails/previews (avoid decoding originals for display)
    derivatives: list[str] = field(default_factory=list)

    # --- filled in by later pipeline stages ---
    phash: Optional[str] = None             # hex perceptual hash
    laplacian: Optional[float] = None       # measured sharpness (None if not computed)

    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
