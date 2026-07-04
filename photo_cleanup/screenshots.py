"""Content-based work-screenshot classifier.

Reads the screenshot's text (Apple's on-device Vision OCR, stored in the
library) and scores it as work vs private using two lexicons (see lexicon.py).
A screenshot is proposed for removal ONLY when it clearly reads as work.

Keep-bias, in order:
  1. A screenshot showing a person / pet / food / meme / scenery is kept
     outright (it's a picture, not a document).
  2. Anything that reads private — messaging UI, casual/intimate words
     (English or Czech) — is kept.
  3. Only a clear work score (work apps, dev/business vocabulary, or a
     data/graphic label) above threshold, and outweighing the private signal,
     is proposed for removal.

Everything is matched against Apple's stored OCR — nothing is decoded or
uploaded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .lexicon import (
    PRIVATE_APPS, PRIVATE_CASUAL, PRIVATE_UI, WORK_APPS, WORK_BIZ, WORK_CHAT_APPS,
    WORK_DEV,
)
from .model import Config, Record

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)  # letters only, keeps accents


@dataclass
class ScreenshotVerdict:
    is_work: bool                 # True => propose removal (cleanup:screenshot)
    reasons: list[str]
    work_score: int = 0
    private_score: int = 0
    kind: str = ""                # dominant work signal (drives the learning loop)


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _hits(tokens: set[str], vocab) -> set[str]:
    return tokens.intersection(vocab)


def classify_screenshot(rec: Record, cfg: Config) -> ScreenshotVerdict:
    # Only screenshots are ever in scope.
    if not (rec.is_screenshot or "screenshot" in rec.media_types):
        return ScreenshotVerdict(False, [])

    labels = set(rec.labels)

    # (1) Picture-like screenshot (person/pet/food/meme/scenery) -> keep.
    keep_hits = labels.intersection(cfg.keep_labels)
    if keep_hits:
        return ScreenshotVerdict(False, [f"keep-label: {', '.join(sorted(keep_hits))}"])

    tokens = _tokens(rec.detected_text)

    # (1b) Authoritative app identity overrides everything below.
    #   WhatsApp/Instagram/Facebook/... -> private (keep).
    #   Slack/Teams/... -> work (remove).
    priv_app = tokens.intersection(PRIVATE_APPS)
    if priv_app:
        return ScreenshotVerdict(False, [f"private-app: {', '.join(sorted(priv_app))}"])
    work_app_chat = tokens.intersection(WORK_CHAT_APPS)
    if work_app_chat and not _suppressed("chat-app"):
        return ScreenshotVerdict(True, [f"work-chat-app: {', '.join(sorted(work_app_chat))}"],
                                 kind="chat-app")

    # work signal
    data_hits = labels.intersection(cfg.work_labels)
    app_hits = _hits(tokens, WORK_APPS)
    vocab_hits = _hits(tokens, WORK_DEV | WORK_BIZ)
    work_score = 2 * len(data_hits) + 3 * len(app_hits) + len(vocab_hits)

    # private signal
    ui_hits = _hits(tokens, PRIVATE_UI)
    casual_hits = _hits(tokens, PRIVATE_CASUAL)
    private_score = 2 * len(ui_hits) + len(casual_hits)

    reasons: list[str] = []
    if data_hits:
        reasons.append(f"data-label: {', '.join(sorted(data_hits))}")
    if app_hits:
        reasons.append(f"work-app: {', '.join(sorted(app_hits))}")
    if vocab_hits:
        reasons.append(f"work-words: {', '.join(sorted(list(vocab_hits)[:6]))}")
    if private_score:
        priv = sorted(list(ui_hits) + list(casual_hits))[:6]
        reasons.append(f"private-signal: {', '.join(priv)}")
    reasons.append(f"score work={work_score} vs private={private_score}")

    # The dominant signal names the "kind" the learning loop tracks: if past
    # reviews show you consistently KEEP screenshots flagged for this reason,
    # the kind is suppressed and no longer flagged.
    kind = "app" if app_hits else ("label" if data_hits else "words")

    # (2) Clear, dominant work signal -> remove.
    if work_score >= cfg.work_min_score and work_score > private_score:
        if _suppressed(kind):
            reasons.append(f"{kind}: learned-keep (you usually keep these)")
            return ScreenshotVerdict(False, reasons, work_score, private_score, kind)
        return ScreenshotVerdict(True, reasons, work_score, private_score, kind)

    # (3) Any personal markers -> keep (this is what protects private chats).
    if private_score > 0:
        return ScreenshotVerdict(False, reasons, work_score, private_score)

    # (4) Fallback: an impersonal, picture-less, dense text document reads as
    #     a work/informational document. (No personal markers reached here.)
    if cfg.enable_doc_fallback and not _suppressed("document"):
        chars = len(rec.detected_text.strip())
        words = len(tokens)
        is_doc = ("document" in labels) or bool(data_hits)
        if is_doc and chars >= cfg.doc_fallback_min_chars and words >= cfg.doc_fallback_min_words:
            reasons.append(f"impersonal text document ({chars} chars, {words} words)")
            return ScreenshotVerdict(True, reasons, work_score, private_score, "document")

    return ScreenshotVerdict(False, reasons, work_score, private_score)


def _suppressed(kind: str) -> bool:
    from .feedback import screenshot_suppressed_kinds
    return kind in screenshot_suppressed_kinds()
