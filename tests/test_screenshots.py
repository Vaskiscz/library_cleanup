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


def test_screenshot_learning_suppression(tmp_path, monkeypatch):
    """The learning loop: log flags with explicit kept labels, learn per-kind
    keep-rates, and stop flagging a kind the user consistently keeps."""
    from photo_cleanup import feedback
    from photo_cleanup.model import Config
    from photo_cleanup.screenshots import classify_screenshot
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", str(tmp_path / "fb"))
    monkeypatch.setattr(feedback, "SCREENSHOT_CORRECTIONS", str(tmp_path / "sc.json"))
    feedback.reset_model_cache()

    rec = mk("s1", is_screenshot=True,
             detected_text="jira sprint deploy backlog standup kubernetes")
    v = classify_screenshot(rec, Config())
    assert v.is_work and v.kind == "app"

    # 6 flagged "words" screenshots, user kept ALL of them -> suppress the kind
    class _V:                       # minimal verdict stand-in for the logger
        def __init__(self, kind): self.kind = kind
    flagged = [(mk(f"u{i}"), _V("app")) for i in range(6)]
    feedback.log_screenshots(flagged, "2025-01-01", "2025-12-31",
                             kept=[f"u{i}" for i in range(6)])
    res = feedback.learn_screenshots(present_uuids=set())
    assert res["suppressed"] == ["app"]

    feedback.reset_model_cache()    # reload the suppression list
    v2 = classify_screenshot(rec, Config())
    assert not v2.is_work           # same screenshot, no longer flagged
    feedback.reset_model_cache()    # don't leak tmp state into other tests


def test_explicit_kept_beats_presence_inference(tmp_path, monkeypatch):
    """A file with explicit 'kept' labels must ignore present_uuids (the app
    writes labels at finalize, BEFORE deletion happens — presence would lie)."""
    from photo_cleanup import feedback
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", str(tmp_path / "fb"))
    monkeypatch.setattr(feedback, "SCREENSHOT_CORRECTIONS", str(tmp_path / "sc.json"))

    class _V:
        def __init__(self, kind): self.kind = kind
    flagged = [(mk(f"u{i}"), _V("app")) for i in range(6)]
    # user kept NONE — but all uuids are still "present" (delete hasn't run yet)
    feedback.log_screenshots(flagged, None, None, kept=[])
    res = feedback.learn_screenshots(present_uuids={f"u{i}" for i in range(6)})
    assert res["suppressed"] == []          # 0% kept -> flagging was correct
    assert res["keep_rate"]["app"] == 0.0
    feedback.reset_model_cache()
