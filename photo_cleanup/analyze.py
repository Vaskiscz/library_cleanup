"""Orchestrate the read-only analysis: screenshots + near-duplicate groups."""

from __future__ import annotations

from dataclasses import dataclass, field

from .cluster import DuplicateGroup, find_duplicate_groups
from .model import Config, Record
from .screenshots import ScreenshotVerdict, classify_screenshot


@dataclass
class Findings:
    total_scanned: int = 0
    work_screenshots: list[tuple[Record, ScreenshotVerdict]] = field(default_factory=list)
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)

    @property
    def n_discards(self) -> int:
        d = len(self.work_screenshots)
        d += sum(len(g.discards) for g in self.duplicate_groups)
        return d

    @property
    def n_keepers_marked(self) -> int:
        return sum(len(g.keepers) for g in self.duplicate_groups)


def analyze(records: list[Record], cfg: Config, embeddings=None) -> Findings:
    from .apply import KW_REVIEWED
    # Never auto-review the Hidden album, or anything already reviewed-and-kept.
    records = [r for r in records
               if not r.is_hidden and KW_REVIEWED not in (r.keywords or [])]
    f = Findings(total_scanned=len(records))

    # 1) work screenshots (high confidence only)
    for rec in records:
        verdict = classify_screenshot(rec, cfg)
        if verdict.is_work:
            f.work_screenshots.append((rec, verdict))

    # 2) near-duplicate photoshoots (only if embeddings are available)
    photos = [r for r in records if not (r.is_screenshot or "screenshot" in r.media_types)]
    f.duplicate_groups = find_duplicate_groups(photos, cfg, embeddings=embeddings)
    return f
