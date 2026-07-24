#!/bin/bash
# Build Library Cleanup, sign it with the Developer ID Application identity,
# package a DMG, then notarize and staple it so Gatekeeper opens it cleanly.
#
# Prereq (once):  bash app/scripts/setup-signing.sh   (stores notary credentials)
# Run:            bash app/scripts/build-signed-dmg.sh
#
# Flags: --minor (public release bump) | --no-bump (rebuild current version)
# Env:   LC_SKIP_NOTARIZE=1        skip notarization for quick local iterations
#        LC_ALLOW_IDENTITY_CHANGE=1  accept a changed signing identity (see guard)
set -euo pipefail
cd "$(dirname "$0")/.."                      # -> app/

IDENTITY="Developer ID Application: VÁCLAV TRNKA (993Q8KJAJS)"
NOTARY_PROFILE="library-cleanup-notary"      # keychain profile from setup-signing.sh
APP="build/photocleanup/macos/app/Library Cleanup.app"
ENT="build/photocleanup/macos/app/Entitlements.plist"
BUILD_LOG="$(mktemp -t library-cleanup-build)"

fail() { echo "ERROR: $1" >&2; exit 1; }

# Preflight: the signing identity must exist before we spend minutes building.
# Developer ID lives in the login keychain (no dedicated keychain to unlock).
security find-identity -v -p codesigning | grep -q "Developer ID Application: VÁCLAV TRNKA" \
  || fail "Developer ID Application identity not found in the keychain — install it from the Apple Developer portal (or Xcode ▸ Settings ▸ Accounts)"

# Verify the vendored wheel(s) haven't been tampered with (audit #12): the build
# installs a binary committed to the repo, so pin its SHA-256.
if [ -f wheels/SHA256SUMS ]; then
  ( cd wheels && shasum -a 256 -c SHA256SUMS ) >/dev/null \
    || fail "vendored wheel checksum mismatch (wheels/SHA256SUMS) — refusing to build"
else
  echo "WARNING: wheels/SHA256SUMS missing — cannot verify vendored wheel integrity." >&2
fi

# Public release: `build-signed-dmg.sh --minor` bumps the MINOR digit and resets
# patch to 0, so the artifact lands on exactly x.y.0. Default = patch bump.
# `--no-bump` rebuilds at the CURRENT version without touching it — for rebuilding
# a release that failed QA (the version is already committed; don't advance it).
case "${1:-}" in
  --minor|--release) echo "[0/5] Bumping MINOR version (public release) ..."
                     VERSION="$(python3 scripts/bump-version.py --minor)" ;;
  --no-bump)         echo "[0/5] Rebuilding at the current version (no bump) ..."
                     VERSION="$(python3 scripts/bump-version.py --show)" ;;
  "")                echo "[0/5] Bumping patch version ..."
                     VERSION="$(python3 scripts/bump-version.py)" ;;
  *)                 fail "unknown flag '$1' (use --minor for a public release, --no-bump to rebuild the current version, or no flag for a normal build)" ;;
esac
echo "  -> v$VERSION"
VOL="Library Cleanup $VERSION"           # volume label (Finder) stays versioned
DMG="dist/Library-Cleanup.dmg"           # file name is STABLE across builds

echo "[1/5] Building (briefcase, ad-hoc; output -> $BUILD_LOG) ..."
rm -rf build
# Build tools are pinned so a silent upstream release can't change the artifact;
# bump these versions deliberately (and rebuild + retest) when upgrading.
BRIEFCASE_PIN="briefcase==0.4.4"
DMGBUILD_PIN="dmgbuild==1.6.7"
if ! { uvx "$BRIEFCASE_PIN" create macOS --no-input && uvx "$BRIEFCASE_PIN" build macOS --no-input; } >"$BUILD_LOG" 2>&1; then
  echo "--- briefcase failed; last 30 lines of $BUILD_LOG ---" >&2
  tail -30 "$BUILD_LOG" >&2
  fail "briefcase build failed"
fi
[ -d "$APP" ] || fail "build finished but $APP is missing"

echo "[2/5] Re-signing with '$IDENTITY' (inside-out, hardened runtime, secure timestamp) ..."
# Notarization rejects get-task-allow (a debug entitlement) — strip it if the
# briefcase template ever adds one.
/usr/libexec/PlistBuddy -c 'Delete :com.apple.security.get-task-allow' "$ENT" 2>/dev/null || true

# INSIDE-OUT signing. Notarization requires EVERY nested Mach-O binary to carry
# its own Developer ID signature with a secure timestamp; `codesign --deep` does
# not reliably do that (and is deprecated), which Apple rejects with hundreds of
# "not signed with a valid Developer ID certificate" errors. So: sign the nested
# libraries first, then the framework bundle, then the outer app.
#
# python.o is an intermediate object file (LLVM bitcode) shipped for building C
# extensions at runtime, which this app never does. Object files cannot carry a
# signature, so notarization always rejects it: remove it.
find "$APP" -name 'python.o' -delete
NESTED="$(mktemp -t library-cleanup-nested)"
find "$APP" -type f \( -name '*.dylib' -o -name '*.so' \) -print0 >"$NESTED"
NESTED_N="$(tr -cd '\0' <"$NESTED" | wc -c | tr -d ' ')"
echo "  signing $NESTED_N nested libraries ..."
# One codesign call per binary (xargs -n1): a single failure then names the file.
xargs -0 -n1 codesign --force --options runtime --timestamp -s "$IDENTITY" <"$NESTED" \
  || fail "signing a nested library failed (see the path in the error above)"
rm -f "$NESTED"
# Then the containers, innermost first. The framework carries no entitlements;
# only the app bundle does (its main executable is what the entitlements govern).
FW="$APP/Contents/Frameworks/Python.framework"
[ -d "$FW" ] && codesign --force --options runtime --timestamp -s "$IDENTITY" "$FW"
codesign --force --options runtime --timestamp \
  --entitlements "$ENT" -s "$IDENTITY" "$APP"
codesign --verify --strict --deep --verbose=1 "$APP"
codesign -dvv "$APP" 2>&1 | grep -E "Authority=|Signature=" | head -3 || true   # cosmetic log; never fail the build

# Signing-identity drift guard (audit #17): TCC (Full Disk Access / Photos) and
# the in-app updater's identity pin are keyed to this designated requirement. If
# it changes (e.g. a different certificate), every user must re-grant
# permissions after updating and the auto-update identity check will reject the
# build. Record it and shout if it changed since the last release.
REQFILE="scripts/released-requirement.txt"
NEWREQ="$(codesign -d -r- "$APP" 2>/dev/null | sed -n 's/^designated => //p')"
if [ -n "$NEWREQ" ]; then
  if [ ! -f "$REQFILE" ]; then
    printf '%s\n' "$NEWREQ" > "$REQFILE"
    echo "  no signing-requirement baseline found; recorded it -> $REQFILE"
  elif [ "$(cat "$REQFILE")" != "$NEWREQ" ]; then
    if [ "${LC_ALLOW_IDENTITY_CHANGE:-}" = "1" ]; then
      printf '%s\n' "$NEWREQ" > "$REQFILE"
      echo "  LC_ALLOW_IDENTITY_CHANGE=1: signing-requirement baseline updated -> $REQFILE"
    else
      echo "ERROR: signing designated requirement CHANGED since the last release —" >&2
      echo "       users will have to re-grant Full Disk Access / Photos after updating," >&2
      echo "       and auto-update from older builds will be rejected by the identity check." >&2
      echo "       If this change is intentional, re-run with LC_ALLOW_IDENTITY_CHANGE=1" >&2
      echo "       to accept the new identity and update $REQFILE." >&2
      exit 1
    fi
  fi
  # Unchanged: write nothing (identical content; avoid mtime churn).
fi

echo "[3/5] Building DMG (dmgbuild: background + drag-to-Applications layout) ..."
mkdir -p dist
# Stable file name: wipe every prior DMG (incl. legacy versioned ones) so only
# the fresh Library-Cleanup.dmg remains.
find dist -maxdepth 1 -name '*.dmg' -print -delete
rm -f "$DMG"
uvx "$DMGBUILD_PIN" -s scripts/dmg-settings.py -D app="$APP" -D bg="$PWD/assets/dmg-background.png" \
  "$VOL" "$DMG" >/dev/null
[ -s "$DMG" ] || fail "dmgbuild finished but $DMG is missing or empty"
codesign --force --timestamp -s "$IDENTITY" "$DMG"

if [ "${LC_SKIP_NOTARIZE:-}" = "1" ]; then
  echo "[4/5] SKIPPING notarization (LC_SKIP_NOTARIZE=1) — do NOT release this DMG."
else
  echo "[4/5] Notarizing (Apple; usually 1-5 min) ..."
  if ! xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait --output-format json >"$BUILD_LOG.notary" 2>&1; then
    cat "$BUILD_LOG.notary" >&2
    echo "HINT: if the profile is missing, run: bash app/scripts/setup-signing.sh" >&2
    echo "HINT: for a rejection, get details with: xcrun notarytool log <submission-id> --keychain-profile $NOTARY_PROFILE" >&2
    fail "notarization failed"
  fi
  NOTARY_STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$BUILD_LOG.notary")"
  [ "$NOTARY_STATUS" = "Accepted" ] || { cat "$BUILD_LOG.notary" >&2; fail "notarization status: ${NOTARY_STATUS:-unknown} (see notarytool log)"; }
  xcrun stapler staple "$DMG" >/dev/null
  # Gatekeeper's own verdict on the stapled DMG — the check users' Macs will run.
  spctl -a -t open --context context:primary-signature -v "$DMG" 2>&1 | grep -q "accepted" \
    || fail "Gatekeeper assessment failed on the stapled DMG"
  echo "  notarized, stapled, Gatekeeper-accepted"
fi

echo "[5/5] Done -> $DMG"
