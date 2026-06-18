import time

from photo_cleanup.model import Config
from photo_cleanup.expired import classify_expired
from conftest import mk

CFG = Config()
NOW = 1_000_000_000.0
YEAR = 365.25 * 24 * 3600


def aged(years, **kw):
    return mk(timestamp=NOW - years * YEAR, **kw)


def test_old_receipt_flagged():
    r = aged(3, labels=["receipt"])
    v = classify_expired(r, CFG, now=NOW)
    assert v.is_expired and v.kind == "receipt"


def test_recent_receipt_kept():
    # receipt threshold is 2y; 1y old stays
    assert not classify_expired(aged(1, labels=["receipt"]), CFG, now=NOW).is_expired


def test_wifi_expires_fast():
    # wifi threshold ~0.25y: a 0.5y-old wifi password is expired
    r = aged(0.5, detected_text="wifi network password ssid")
    v = classify_expired(r, CFG, now=NOW)
    assert v.is_expired and v.kind == "wifi"


def test_recent_wifi_kept():
    assert not classify_expired(aged(0.1, detected_text="wifi password ssid"), CFG, now=NOW).is_expired


def test_person_never_expired():
    r = aged(5, labels=["people", "document"], detected_text="receipt total vat")
    assert not classify_expired(r, CFG, now=NOW).is_expired


def test_broad_document_alone_not_flagged():
    # a 'document' label with no utility text must NOT be flagged (avoids posters)
    r = aged(5, labels=["document"], detected_text="lord of the rings fun facts about elves")
    assert not classify_expired(r, CFG, now=NOW).is_expired
