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

# Utility words grouped by type (drives the per-type age threshold).
WIFI_WORDS = {"wifi", "wi-fi", "ssid", "password", "heslo", "network", "síť",
              "router", "hotspot", "login"}
RECEIPT_WORDS = {"receipt", "účtenka", "uctenka", "invoice", "faktura", "total",
                 "celkem", "subtotal", "tax", "vat", "dph", "change", "cash", "amount"}
TRAVEL_WORDS = {"parking", "parkoviště", "parkovani", "parkování", "garage", "boarding"}
ORDER_WORDS = {"ticket", "lístek", "vstupenka", "confirmation", "potvrzení",
               "objednávka", "otp", "verification", "voucher"}

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
    kind: str = ""          # detected utility type (drives the age threshold)


def _utility_type(labels: set, words: set) -> Optional[str]:
    """Classify the utility kind (most specific first); None if no signal."""
    if "identity document" in labels:
        return "ID document"
    if "business card" in labels:
        return "business card"
    if "receipt" in labels or (words & RECEIPT_WORDS):
        return "receipt"
    if words & WIFI_WORDS:
        return "wifi"
    if words & TRAVEL_WORDS:
        return "parking/boarding"
    if words & ORDER_WORDS:
        return "ticket/order"
    if "qr code" in labels or "barcode" in labels:
        return "qr/barcode"
    return None   # generic doc + utility text, no specific kind


def classify_expired(rec: Record, cfg: Config, now: Optional[float] = None) -> ExpiredVerdict:
    if rec.timestamp is None:
        return ExpiredVerdict(False, [])
    now = now if now is not None else time.time()
    age = (now - rec.timestamp) / YEAR_SECONDS

    labels = set(rec.labels)
    # Memory guard: people/pets/food/scenery/art are kept regardless.
    keep_hits = labels.intersection(cfg.keep_labels)
    if keep_hits:
        return ExpiredVerdict(False, [f"keep-label: {', '.join(sorted(keep_hits))}"], age)

    strong = labels.intersection(STRONG_UTILITY_LABELS)
    weak = labels.intersection(WEAK_UTILITY_LABELS)
    words = _tokens(rec.detected_text).intersection(UTILITY_WORDS)

    # Evidence: a specific utility label; OR clear utility text (2+ words); OR a
    # broad label corroborated by at least one utility word.
    has_signal = bool(strong) or len(words) >= 2 or (bool(weak) and len(words) >= 1)
    if not has_signal:
        return ExpiredVerdict(False, [], age)

    kind = _utility_type(labels, words) or "generic"

    # Learned correction: if past iterations show you keep this type too often,
    # stop flagging it.
    from .feedback import expired_suppressed_kinds
    if kind in expired_suppressed_kinds():
        return ExpiredVerdict(False, [f"{kind}: learned-keep (you usually keep these)"], age, kind)

    threshold = cfg.expired_age_by_type.get(kind, cfg.expired_min_age_years)
    if age < threshold:
        return ExpiredVerdict(False, [f"{kind}: too recent ({age:.1f}y < {threshold}y)"], age, kind)

    reasons = [f"{kind} · age {age:.1f}y (≥{threshold}y)"]
    if strong:
        reasons.append(f"label: {', '.join(sorted(strong))}")
    elif weak:
        reasons.append(f"label+text: {', '.join(sorted(weak))}")
    if words:
        reasons.append(f"words: {', '.join(sorted(list(words)[:6]))}")
    return ExpiredVerdict(True, reasons, age, kind)
