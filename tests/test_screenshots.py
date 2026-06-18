from photo_cleanup.model import Config
from photo_cleanup.screenshots import classify_screenshot
from conftest import mk

CFG = Config()


def test_non_screenshot_never_flagged():
    assert not classify_screenshot(mk(is_screenshot=False), CFG).is_work


def test_work_screenshot_by_app_label():
    r = mk(is_screenshot=True, labels=["document", "chart"],
           detected_text="quarterly revenue\nsprint backlog\ndeploy pipeline\nstandup notes")
    assert classify_screenshot(r, CFG).is_work


def test_slack_is_always_work():
    r = mk(is_screenshot=True, labels=["document"], detected_text="slack channel ping")
    assert classify_screenshot(r, CFG).is_work


def test_whatsapp_is_always_private():
    r = mk(is_screenshot=True, labels=["document"],
           detected_text="whatsapp chat dense text total vat invoice")
    assert not classify_screenshot(r, CFG).is_work


def test_person_keep_label_wins():
    r = mk(is_screenshot=True, labels=["people", "document"], detected_text="x" * 300)
    assert not classify_screenshot(r, CFG).is_work


def test_private_czech_chat_kept():
    # casual Czech pronouns/words => private, not work
    r = mk(is_screenshot=True, labels=["document"],
           detected_text="ahoj jak se máš dneska večer doma mám tě rád")
    assert not classify_screenshot(r, CFG).is_work
