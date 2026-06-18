"""Expired-utility classifier — flag aged single-purpose photos.

Targets pictures that had a use at the time but don't make sense to keep months
or years later: receipts, wifi passwords, parking spots, tickets, labels, signs,
whiteboards, confirmations, etc. Uses only Apple's on-device data (OCR text + ML
labels) plus the photo's age. Nothing is decoded; nothing leaves the Mac.

Keep-bias, in order:
  1. Too recent (younger than the age threshold) -> keep.
  2. Shows people / pets / food / scenery / art -> keep (it's a memory).
  3. Only then: a clear utility signal (document/receipt/QR label, or utility
     OCR words like wifi/password/receipt/parking) -> flag cleanup:expired.
Anything ambiguous is kept.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .model import Config, Record
from .screenshots import _tokens

YEAR_SECONDS = 365.25 * 24 * 3600

# STRONG utility labels — unambiguously throwaway; flag on the label alone.
STRONG_UTILITY_LABELS = {
    "receipt", "qr code", "barcode", "business card", "identity document",
}
# WEAK/broad labels — catch lots of keep-worthy stuff (a fun-facts poster, a
# restaurant list), so they only flag WITH corroborating utility text.
WEAK_UTILITY_LABELS = {
    "document", "text", "paper", "menu", "whiteboard", "money", "currency",
    "ticket", "label", "sign", "poster", "advertisement", "calendar",
    "printed page", "spreadsheet", "form",
}

# OCR words (EN + CZ) typical of throwaway utility shots (weight 1).
UTILITY_WORDS = {
    # wifi / network
    "wifi", "wi-fi", "ssid", "password", "heslo", "network", "síť", "router",
    "login", "hotspot",
    # receipts / payments
    "receipt", "účtenka", "uctenka", "invoice", "faktura", "total", "celkem",
    "subtotal", "tax", "vat", "dph", "change", "cash", "amount",
    # parking / travel logistics (specific terms only — dropped ambiguous
    # singletons like level/spot/gate/seat/terminal/platform/reference/code)
    "parking", "parkoviště", "parkovani", "parkování", "boarding",
    # tickets / orders / codes
    "ticket", "lístek", "vstupenka", "confirmation", "potvrzení",
    "objednávka", "otp", "verification", "voucher",
}


@dataclass
class ExpiredVerdict:
    is_expired: bool
    reasons: list[str]
    age_years: float = 0.0


def classify_expired(rec: Record, cfg: Config, now: Optional[float] = None) -> ExpiredVerdict:
    if rec.timestamp is None:
        return ExpiredVerdict(False, [])
    now = now if now is not None else time.time()
    age = (now - rec.timestamp) / YEAR_SECONDS
    if age < cfg.expired_min_age_years:
        return ExpiredVerdict(False, [f"too recent ({age:.1f}y)"], age)

    labels = set(rec.labels)

    # Memory guard: people/pets/food/scenery/art are kept regardless.
    keep_hits = labels.intersection(cfg.keep_labels)
    if keep_hits:
        return ExpiredVerdict(False, [f"keep-label: {', '.join(sorted(keep_hits))}"], age)

    strong = labels.intersection(STRONG_UTILITY_LABELS)
    weak = labels.intersection(WEAK_UTILITY_LABELS)
    words = _tokens(rec.detected_text).intersection(UTILITY_WORDS)

    # Flag when: a specific utility label is present; OR clear utility text
    # (2+ words); OR a broad label corroborated by at least one utility word.
    is_expired = bool(strong) or len(words) >= 2 or (bool(weak) and len(words) >= 1)

    reasons = [f"age {age:.1f}y"]
    if strong:
        reasons.append(f"utility-label: {', '.join(sorted(strong))}")
    elif weak and is_expired:
        reasons.append(f"label+text: {', '.join(sorted(weak))}")
    if words:
        reasons.append(f"utility-words: {', '.join(sorted(list(words)[:6]))}")

    return ExpiredVerdict(is_expired, reasons, age)
