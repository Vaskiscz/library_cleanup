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
from photocleanup import updater

APP_DIR = os.path.join(os.path.dirname(__file__), "..")
REPO_ROOT = os.path.join(APP_DIR, "..")


def _script_text(name: str) -> str:
    with open(os.path.join(APP_DIR, "scripts", name)) as f:
        return f.read()


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


def test_build_script_writes_the_dmg_the_updater_downloads():
    # The DMG file name is declared twice: DMG=... in build-signed-dmg.sh and
    # ASSET_NAME in photocleanup/updater.py. If they drift, auto-update looks
    # for a release asset the build never produced.
    script = _script_text("build-signed-dmg.sh")
    expected = "dist/" + updater.ASSET_NAME
    assert "dist/Library-Cleanup.dmg" in script, (
        "build-signed-dmg.sh no longer writes dist/Library-Cleanup.dmg — the "
        "DMG name is deliberately stable; if it must change, change "
        "updater.ASSET_NAME in lockstep"
    )
    assert expected == "dist/Library-Cleanup.dmg", (
        f"updater.ASSET_NAME is {updater.ASSET_NAME!r} but build-signed-dmg.sh "
        f"produces dist/Library-Cleanup.dmg — auto-update would never find the "
        f"release asset"
    )


def test_signing_identity_matches_between_scripts():
    # Both scripts declare the Developer ID identity (build signs with it,
    # setup checks it's installed). If they drift, preflight fails — or worse,
    # a build signs with a different identity and users lose TCC grants.
    build = re.search(r'^IDENTITY="([^"]+)"', _script_text("build-signed-dmg.sh"), re.M)
    setup = re.search(r'^IDENTITY="([^"]+)"', _script_text("setup-signing.sh"), re.M)
    assert build, "IDENTITY=\"...\" not found in build-signed-dmg.sh"
    assert setup, "IDENTITY=\"...\" not found in setup-signing.sh"
    assert build.group(1) == setup.group(1), (
        f"signing identity drift: build-signed-dmg.sh signs with "
        f"{build.group(1)!r} but setup-signing.sh expects {setup.group(1)!r}"
    )
    assert build.group(1).startswith("Developer ID Application:"), (
        "the release must be signed with a Developer ID Application identity "
        "(notarization requires it); self-signed builds are not releasable"
    )


def test_notary_profile_matches_between_scripts():
    # The build submits with --keychain-profile NOTARY_PROFILE; setup-signing.sh
    # creates that profile. Drift means every release build fails at notarization.
    build = re.search(r'^NOTARY_PROFILE="([^"]+)"', _script_text("build-signed-dmg.sh"), re.M)
    setup = re.search(r'^NOTARY_PROFILE="([^"]+)"', _script_text("setup-signing.sh"), re.M)
    assert build and setup and build.group(1) == setup.group(1), (
        "notary keychain-profile name drifted between build-signed-dmg.sh "
        "and setup-signing.sh"
    )
