"""CLI behavior — finalize semantics, dry-run safety, and date/eligibility filters.

Everything runs through click's CliRunner with the Photos-touching layers
(records_ram + the apply module writers/readers) monkeypatched. A regression in
`finalize` mutates a real library, so its semantics get the widest coverage:
range restriction, favorite-baseline preservation, and the never-lock-a-pending-
delete rule.
"""
import json
import time
from datetime import datetime

import pytest
from click.testing import CliRunner

from photo_cleanup import apply as apply_mod
from photo_cleanup import cli as cli_mod
from photo_cleanup.apply import ApplyResult
from conftest import mk


runner = CliRunner()


def ts(y, m, d):
    """Local-noon epoch timestamp (matches _filter_by_date's local-date logic)."""
    return datetime(y, m, d, 12, 0).timestamp()


@pytest.fixture(autouse=True)
def guard_photos(monkeypatch):
    """No test may open the real Photos library or the osxphotos DB."""
    def boom(*a, **k):
        raise AssertionError("test must not touch the Photos library")
    monkeypatch.setattr(apply_mod, "_library", boom)
    monkeypatch.setattr(cli_mod, "records_ram", boom)


@pytest.fixture
def records(monkeypatch):
    """Install a canned record list as the scan result."""
    def _install(recs):
        monkeypatch.setattr(cli_mod, "records_ram", lambda *a, **k: recs)
        return recs
    return _install


class ApplyRecorder:
    """Replaces the apply-module writers with recorders returning plausible
    ApplyResults (tagged/favorited = len(uuids)), so echoes stay realistic."""

    def __init__(self, monkeypatch, results=None):
        self.calls = []
        self._results = results or {}
        for name in ("add_keyword", "favorite", "clear_keywords_for_uuids",
                     "unfavorite_uuids"):
            monkeypatch.setattr(apply_mod, name, self._make(name))

    def _make(self, name):
        def fn(uuids, *args, apply=False, progress=None, **kw):
            uuids = list(uuids)
            self.calls.append((name, uuids, args, apply))
            if name in self._results:
                return self._results[name]
            return ApplyResult(tagged=len(uuids), favorited=len(uuids))
        return fn

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


# ------------------------------------------------------------- _filter_by_date

def test_filter_by_date_inclusive_bounds():
    recs = [mk("early", timestamp=ts(2023, 1, 1)),
            mk("mid", timestamp=ts(2023, 6, 15)),
            mk("late", timestamp=ts(2023, 12, 31)),
            mk("after", timestamp=ts(2024, 1, 1))]
    out = cli_mod._filter_by_date(recs, "2023-01-01", "2023-12-31")
    assert [r.uuid for r in out] == ["early", "mid", "late"]   # bounds inclusive


def test_filter_by_date_since_only():
    recs = [mk("old", timestamp=ts(2022, 5, 1)), mk("new", timestamp=ts(2023, 5, 1))]
    out = cli_mod._filter_by_date(recs, "2023-01-01", None)
    assert [r.uuid for r in out] == ["new"]


def test_filter_by_date_until_only():
    recs = [mk("old", timestamp=ts(2022, 5, 1)), mk("new", timestamp=ts(2023, 5, 1))]
    out = cli_mod._filter_by_date(recs, None, "2022-12-31")
    assert [r.uuid for r in out] == ["old"]


def test_filter_by_date_no_bounds_passthrough():
    recs = [mk("a", timestamp=None), mk("b", timestamp=ts(2023, 1, 1))]
    assert cli_mod._filter_by_date(recs, None, None) is recs


def test_filter_by_date_drops_undated_when_filtering():
    recs = [mk("undated", timestamp=None), mk("dated", timestamp=ts(2023, 6, 1))]
    out = cli_mod._filter_by_date(recs, "2023-01-01", None)
    assert [r.uuid for r in out] == ["dated"]


# ------------------------------------------------------------- _active_records

def test_active_records_excludes_hidden_and_reviewed(records):
    records([mk("ok", timestamp=ts(2023, 6, 1)),
             mk("hidden", timestamp=ts(2023, 6, 1), is_hidden=True),
             mk("locked", timestamp=ts(2023, 6, 1), keywords=["reviewed:keep"])])
    out = cli_mod._active_records(None, None, None)
    assert [r.uuid for r in out] == ["ok"]


def test_active_records_include_reviewed_flag(records):
    records([mk("ok", timestamp=ts(2023, 6, 1)),
             mk("locked", timestamp=ts(2023, 6, 1), keywords=["reviewed:keep"])])
    out = cli_mod._active_records(None, None, None, include_reviewed=True)
    assert {r.uuid for r in out} == {"ok", "locked"}


def test_active_records_respects_date_scope(records):
    records([mk("in", timestamp=ts(2023, 6, 1)), mk("out", timestamp=ts(2024, 6, 1))])
    out = cli_mod._active_records(None, "2023-01-01", "2023-12-31")
    assert [r.uuid for r in out] == ["in"]


# --------------------------------------------------------------------- finalize
#
# Standard scenario (range 2023): k1 = survivor hearted this round, k2 = survivor
# that was a GENUINE favorite (in baseline), p1 = still tagged (pending delete),
# n1 = plain in-range photo, out1 = survivor OUTSIDE the range.

def _finalize_setup(records, monkeypatch, tmp_path, baseline=("k2",)):
    records([mk("k1", timestamp=ts(2023, 3, 1)),
             mk("k2", timestamp=ts(2023, 4, 1)),
             mk("p1", timestamp=ts(2023, 5, 1)),
             mk("n1", timestamp=ts(2023, 6, 1)),
             mk("out1", timestamp=ts(2024, 6, 1))])
    monkeypatch.setattr(apply_mod, "find_rescue_uuids",
                        lambda prefix, use_favorites=True, **kw: ["k1", "k2", "out1"])
    monkeypatch.setattr(apply_mod, "find_tagged_uuids",
                        lambda prefix, dbpath=None: ["p1", "out_p"])
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps(list(baseline)))
    return ["finalize", "--since", "2023-01-01", "--until", "2023-12-31",
            "--baseline", str(base)]


def test_finalize_dry_run_writes_nothing(records, monkeypatch, tmp_path):
    args = _finalize_setup(records, monkeypatch, tmp_path)
    rec = ApplyRecorder(monkeypatch)
    result = runner.invoke(cli_mod.cli, args)
    assert result.exit_code == 0
    assert rec.calls == []                       # dry run: zero write-backs
    assert "[DRY RUN]" in result.output
    assert "un-tag 2 keepers" in result.output   # k1+k2 (out1 excluded)
    assert "un-favorite 1" in result.output      # k2 preserved by baseline
    assert "add --apply to write" in result.output


def test_finalize_range_and_baseline_semantics(records, monkeypatch, tmp_path):
    args = _finalize_setup(records, monkeypatch, tmp_path)
    rec = ApplyRecorder(monkeypatch)
    result = runner.invoke(cli_mod.cli, args + ["--apply"])
    assert result.exit_code == 0

    # Survivors restricted to the range: out1 favorited+tagged but NOT touched.
    (name, cleared, cargs, applied), = rec.named("clear_keywords_for_uuids")
    assert cleared == ["k1", "k2"] and applied is True
    assert cargs[0] == apply_mod.KW_DUPLICATE    # default --prefix

    # Baseline preservation: only the newly-hearted k1 loses its heart.
    (_, unfav, _, _), = rec.named("unfavorite_uuids")
    assert unfav == ["k1"]

    # reviewed:keep — first the survivors, then the range lock.
    marks = rec.named("add_keyword")
    assert len(marks) == 2
    assert marks[0][1] == ["k1", "k2"] and marks[0][2][0] == apply_mod.KW_REVIEWED
    assert set(marks[1][1]) == {"k1", "k2", "n1"} and marks[1][2][0] == apply_mod.KW_REVIEWED
    assert all(c[3] is True for c in rec.calls)


def test_finalize_never_locks_a_pending_delete(records, monkeypatch, tmp_path):
    args = _finalize_setup(records, monkeypatch, tmp_path)
    rec = ApplyRecorder(monkeypatch)
    result = runner.invoke(cli_mod.cli, args + ["--apply"])
    assert result.exit_code == 0
    # p1 is still tagged cleanup:duplicate — it must appear in NO reviewed:keep call.
    for _, uuids, _cargs, _ in rec.named("add_keyword"):
        assert "p1" not in uuids
    assert "1 still tagged" in result.output     # and the user is told why


def test_finalize_no_lock_skips_the_range_mark(records, monkeypatch, tmp_path):
    args = _finalize_setup(records, monkeypatch, tmp_path)
    rec = ApplyRecorder(monkeypatch)
    result = runner.invoke(cli_mod.cli, args + ["--no-lock", "--apply"])
    assert result.exit_code == 0
    marks = rec.named("add_keyword")
    assert len(marks) == 1                       # survivors only, no range lock
    assert marks[0][1] == ["k1", "k2"]
    # n1 (plain in-range photo) is never marked without --lock
    assert all("n1" not in uuids for _, uuids, _, _ in rec.calls)


def test_finalize_missing_baseline_unfavorites_all_survivors(records, monkeypatch, tmp_path):
    args = _finalize_setup(records, monkeypatch, tmp_path)
    args[args.index("--baseline") + 1] = str(tmp_path / "nonexistent.json")
    rec = ApplyRecorder(monkeypatch)
    result = runner.invoke(cli_mod.cli, args + ["--apply"])
    assert result.exit_code == 0
    (_, unfav, _, _), = rec.named("unfavorite_uuids")
    assert unfav == ["k1", "k2"]                 # no baseline -> nothing preserved


def test_finalize_file_mode_uses_lists_and_never_range_locks(monkeypatch, tmp_path):
    rescue = tmp_path / "rescue.json"
    unfav = tmp_path / "unfav.json"
    rescue.write_text(json.dumps(["a", "b"]))
    unfav.write_text(json.dumps(["a"]))
    rec = ApplyRecorder(monkeypatch)
    result = runner.invoke(cli_mod.cli, [
        "finalize", "--rescue-file", str(rescue), "--unfav-file", str(unfav), "--apply"])
    assert result.exit_code == 0
    (_, cleared, _, _), = rec.named("clear_keywords_for_uuids")
    assert cleared == ["a", "b"]
    (_, unfaved, _, _), = rec.named("unfavorite_uuids")
    assert unfaved == ["a"]
    marks = rec.named("add_keyword")
    assert len(marks) == 1                       # no range -> no lock pass
    assert marks[0][1] == ["a", "b"]


def test_finalize_surfaces_first_error(records, monkeypatch, tmp_path):
    args = _finalize_setup(records, monkeypatch, tmp_path)
    ApplyRecorder(monkeypatch, results={
        "clear_keywords_for_uuids": ApplyResult(tagged=1, errors=1,
                                                first_error="photo not found: k2")})
    result = runner.invoke(cli_mod.cli, args + ["--apply"])
    assert result.exit_code == 0
    assert "errors 1 (first: photo not found: k2)" in result.output


# ------------------------------------------------- dry runs of destructive cmds

def test_apply_command_dry_run(records, monkeypatch):
    records([mk("s1"), mk("s2"), mk("s3")])
    monkeypatch.setattr(cli_mod, "analyze", lambda recs, cfg, **kw: type(
        "F", (), {"work_screenshots": [(mk("s1"), None), (mk("s2"), None)]})())
    result = runner.invoke(cli_mod.cli, ["apply"])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "would tag 2" in result.output
    assert "--apply to write" in result.output


def test_undo_dry_run(monkeypatch):
    monkeypatch.setattr(apply_mod, "find_tagged_uuids",
                        lambda prefix, dbpath=None: ["a", "b"])
    result = runner.invoke(cli_mod.cli, ["undo"])
    assert result.exit_code == 0
    assert "[DRY RUN]" in result.output
    assert "would clear 2 photos" in result.output


def test_clear_tags_dry_run(tmp_path):
    f = tmp_path / "uuids.json"
    f.write_text(json.dumps(["a", "b", "c"]))
    result = runner.invoke(cli_mod.cli, ["clear-tags", "--uuids-file", str(f)])
    assert result.exit_code == 0
    assert "[DRY RUN]" in result.output
    assert "would clear 3" in result.output


def test_unfavorite_dry_run(tmp_path):
    f = tmp_path / "uuids.json"
    f.write_text(json.dumps(["a", "b"]))
    result = runner.invoke(cli_mod.cli, ["unfavorite", "--uuids-file", str(f)])
    assert result.exit_code == 0
    assert "[DRY RUN]" in result.output
    assert "would un-favorite 2" in result.output


def test_mark_reviewed_dry_run_from_file(tmp_path):
    f = tmp_path / "uuids.json"
    f.write_text(json.dumps(["a", "b"]))
    result = runner.invoke(cli_mod.cli, ["mark-reviewed", "--uuids-file", str(f)])
    assert result.exit_code == 0
    assert "[DRY RUN]" in result.output
    assert "would mark 2" in result.output


def test_mark_reviewed_dry_run_range(records):
    records([mk("in", timestamp=ts(2023, 6, 1)), mk("out", timestamp=ts(2024, 6, 1))])
    result = runner.invoke(cli_mod.cli, [
        "mark-reviewed", "--since", "2023-01-01", "--until", "2023-12-31"])
    assert result.exit_code == 0
    assert "marking 1 photos" in result.output
    assert "would mark 1" in result.output


def test_expired_dry_run_tags_nothing(records, monkeypatch, tmp_path):
    year = 365.25 * 24 * 3600
    records([mk("receipt", timestamp=time.time() - 3 * year, labels=["receipt"]),
             mk("recent", timestamp=time.time() - 0.1 * year, labels=["receipt"])])
    # add_keyword must not be called at all on the dry-run path
    def no_write(*a, **k):
        raise AssertionError("expired dry run must not tag")
    monkeypatch.setattr(apply_mod, "add_keyword", no_write)
    report = tmp_path / "expired.html"
    result = runner.invoke(cli_mod.cli, ["expired", "--report", str(report)])
    assert result.exit_code == 0
    assert "1 flagged expired" in result.output
    assert "Dry run — nothing tagged" in result.output
    assert report.exists()


def test_mark_reviewed_apply_surfaces_first_error(monkeypatch, tmp_path):
    f = tmp_path / "uuids.json"
    f.write_text(json.dumps(["a", "b", "c"]))
    monkeypatch.setattr(apply_mod, "add_keyword", lambda *a, **k: ApplyResult(
        tagged=1, errors=2, first_error="AppleEvent timed out (-1712)"))
    result = runner.invoke(cli_mod.cli, ["mark-reviewed", "--uuids-file", str(f), "--apply"])
    assert result.exit_code == 0
    assert "errors 2 (first: AppleEvent timed out (-1712))" in result.output
