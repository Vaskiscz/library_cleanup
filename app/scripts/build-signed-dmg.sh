#!/bin/bash
# Build Library Cleanup and sign it with the stable self-signed identity, then
# package a DMG. The cert stays UNTRUSTED (never a system trust root) — that's
# enough for macOS to keep Full Disk Access / Photos grants across rebuilds,
# because TCC matches the signing identity, not its trust chain.
#
# Prereq (once):  bash app/scripts/setup-signing.sh
# Run:            bash app/scripts/build-signed-dmg.sh
set -euo pipefail
cd "$(dirname "$0")/.."                      # -> app/

IDENTITY="Library Cleanup Self-Signed"
KC="$HOME/Library/Keychains/library-cleanup-signing.keychain-db"
KCPW="${LC_KEYCHAIN_PW:-libraryclean}"
APP="build/photocleanup/macos/app/Library Cleanup.app"
ENT="build/photocleanup/macos/app/Entitlements.plist"
BUILD_LOG="$(mktemp -t library-cleanup-build)"

fail() { echo "ERROR: $1" >&2; exit 1; }

# Preflight: the signing identity must exist before we spend minutes building.
[ -f "$KC" ] || fail "signing keychain missing ($KC) — run app/scripts/setup-signing.sh first"
[ -n "${LC_KEYCHAIN_PW:-}" ] || echo "WARNING: using the default signing-keychain password (set LC_KEYCHAIN_PW on shared/CI hosts)." >&2
security unlock-keychain -p "$KCPW" "$KC" \
  || fail "couldn't unlock the signing keychain (set LC_KEYCHAIN_PW if you changed the password)"
security set-keychain-settings -lt 3600 "$KC" 2>/dev/null || true   # re-assert auto-lock (audit #18)
security find-identity -p codesigning "$KC" | grep -q "$IDENTITY" \
  || fail "identity '$IDENTITY' not found in $KC — run app/scripts/setup-signing.sh"

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
case "${1:-}" in
  --minor|--release) echo "[0/4] Bumping MINOR version (public release) ..."
                     VERSION="$(python3 scripts/bump-version.py --minor)" ;;
  "")                echo "[0/4] Bumping patch version ..."
                     VERSION="$(python3 scripts/bump-version.py)" ;;
  *)                 fail "unknown flag '$1' (use --minor for a public release, or no flag for a normal build)" ;;
esac
echo "  -> v$VERSION"
VOL="Library Cleanup $VERSION"           # volume label (Finder) stays versioned
DMG="dist/Library-Cleanup.dmg"           # file name is STABLE across builds

echo "[1/4] Building (briefcase, ad-hoc; output -> $BUILD_LOG) ..."
rm -rf build
if ! { uvx briefcase create macOS --no-input && uvx briefcase build macOS --no-input; } >"$BUILD_LOG" 2>&1; then
  echo "--- briefcase failed; last 30 lines of $BUILD_LOG ---" >&2
  tail -30 "$BUILD_LOG" >&2
  fail "briefcase build failed"
fi
[ -d "$APP" ] || fail "build finished but $APP is missing"

echo "[2/4] Re-signing with '$IDENTITY' (hardened runtime, app entitlements) ..."
codesign --force --deep --options runtime --timestamp=none \
  --entitlements "$ENT" -s "$IDENTITY" "$APP"
codesign --verify --strict --verbose=1 "$APP"
codesign -dvv "$APP" 2>&1 | grep -E "Authority=|Signature=" | head -2

# Signing-identity drift guard (audit #17): TCC (Full Disk Access / Photos) and
# the in-app updater's identity pin are keyed to this designated requirement. If
# it changes (e.g. the signing key was regenerated), every user must re-grant
# permissions after updating and the auto-update identity check will reject the
# build. Record it and shout if it changed since the last release.
REQFILE="scripts/released-requirement.txt"
NEWREQ="$(codesign -d -r- "$APP" 2>/dev/null | sed -n 's/^designated => //p')"
if [ -n "$NEWREQ" ]; then
  if [ -f "$REQFILE" ] && [ "$(cat "$REQFILE")" != "$NEWREQ" ]; then
    echo "WARNING: signing designated requirement CHANGED since the last release —" >&2
    echo "         users will have to re-grant Full Disk Access / Photos after updating," >&2
    echo "         and auto-update from older builds will be rejected by the identity check." >&2
  fi
  printf '%s\n' "$NEWREQ" > "$REQFILE"
fi

echo "[3/4] Building DMG (dmgbuild: background + drag-to-Applications layout) ..."
mkdir -p dist
# Stable file name: wipe every prior DMG (incl. legacy versioned ones) so only
# the fresh Library-Cleanup.dmg remains.
find dist -maxdepth 1 -name '*.dmg' -print -delete
rm -f "$DMG"
uvx dmgbuild -s scripts/dmg-settings.py -D app="$APP" -D bg="$PWD/assets/dmg-background.png" \
  "$VOL" "$DMG" >/dev/null
[ -s "$DMG" ] || fail "dmgbuild finished but $DMG is missing or empty"

echo "[4/4] Done -> $DMG"
