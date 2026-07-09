"""Release-hygiene guards: the things that only break at build/ship time.

The app version is declared twice (``__init__.__version__`` for the footer and
updater, ``[tool.briefcase] version`` for the bundle) and kept in lockstep by
scripts/bump-version.py — a hand edit to one of them makes the self-updater
compare the wrong version. Briefcase also resolves its ``requires`` at build
time with no lockfile, so the pins there must match what the root uv.lock (and
therefore the test suite) actually exercises. Both are cheap to guard here.
"""
import os
import re
import tomllib

import photocleanup

APP_DIR = os.path.join(os.path.dirname(__file__), "..")
REPO_ROOT = os.path.join(APP_DIR, "..")


def _briefcase_config() -> dict:
    with open(os.path.join(APP_DIR, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def test_version_declared_in_lockstep():
    cfg = _briefcase_config()
    assert cfg["tool"]["briefcase"]["version"] == photocleanup.__version__, (
        "app/pyproject.toml [tool.briefcase] version and photocleanup.__version__ "
        "have drifted — always bump via app/scripts/bump-version.py"
    )


def test_briefcase_pins_match_uv_lock():
    with open(os.path.join(REPO_ROOT, "uv.lock"), "rb") as f:
        locked = {p["name"]: p["version"] for p in tomllib.load(f)["package"]}
    requires = _briefcase_config()["tool"]["briefcase"]["app"]["photocleanup"]["requires"]
    pins = dict(re.match(r"([A-Za-z0-9_.-]+)==(.+)", r).groups()
                for r in requires if "==" in r)
    assert pins, "expected ==-pinned requirements in [tool.briefcase] requires"
    for name, version in pins.items():
        assert locked.get(name) == version, (
            f"{name}=={version} in app/pyproject.toml but uv.lock resolves "
            f"{locked.get(name)} — the shipped app would differ from what the "
            f"tests exercised; re-pin from the lock"
        )
