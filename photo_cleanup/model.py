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

    # --- clustering: group a whole PHOTOSHOOT/session (same place, one visit) ---
    # Coarse on purpose: a photoshoot spans minutes with pauses, so small gaps
    # must NOT split it into separate bursts (that left ~15 keepers per shoot).
    cluster_gap_seconds: float = 600.0     # new session when time gap > 10 min
    cluster_gps_meters: float = 150.0      # ...or location moves > 150 m

    # --- near-duplicate keeper selection (Apple Vision feature-print embeddings) ---
    # Adaptive keepers: a bigger photoshoot signals it mattered more, so allow
    # more keepers — (max_session_size, keepers) tiers, then keepers_max above.
    # The diversity floor still gates: a uniform shoot keeps few even if large.
    keeper_tiers: tuple = ((3, 1), (9, 3), (19, 5), (39, 8))
    keepers_max: int = 10
    # A second keeper must be at least this far (embedding L2) from every already
    # chosen keeper — ensures variety (different expression/pose), not near-dupes.
    # Calibrated by eye: ~0.15-0.24 still looks near-identical; ~0.28+ reads as a
    # genuinely different shot. Set at 0.30 so a keeper must be CLEARLY different
    # (and good — see the quality gate) to be added; the cap is only reached when
    # that many truly qualify, never padded.
    keeper_diversity_min: float = 0.30

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

    # --- expired single-purpose utility photos (receipts/wifi/parking/...) ---
    # Per-type minimum age (years): a wifi password is useless in weeks; a
    # receipt may matter for tax/warranty ~2y; ID photos are kept deliberately.
    expired_min_age_years: float = 1.0     # fallback for generic doc+text utility
    expired_age_by_type: dict = field(default_factory=lambda: {
        "wifi": 0.25,             # ~3 months
        "parking/boarding": 0.1,  # ~5 weeks (useless after the trip)
        "ticket/order": 0.5,      # ~6 months (after the event/order)
        "qr/barcode": 0.5,
        "receipt": 2.0,           # tax / warranty window
        "business card": 3.0,     # contacts linger a while
        "ID document": 5.0,       # usually kept on purpose — rarely flag
    })

    # --- video cleanup ---
    video_dup_distance: float = 0.30       # poster-frame L2 <= this => same-scene take
    large_video_mb: float = 200.0          # videos >= this are "reconsider" candidates

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
    # metadata richness (device/AirDrop originals keep these; messaging re-encodes
    # strip them — used to prefer the true original among duplicate videos)
    camera_make: str = ""
    camera_model: str = ""

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

    # full Apple aesthetic sub-score vector (features for the learning engine)
    features: dict = field(default_factory=dict)

    # paths to existing thumbnails/previews (avoid decoding originals for display)
    derivatives: list[str] = field(default_factory=list)

    # --- filled in by later pipeline stages ---
    laplacian: Optional[float] = None       # measured sharpness (None if not computed)

    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})
