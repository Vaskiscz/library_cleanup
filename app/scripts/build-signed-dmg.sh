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
security unlock-keychain -p "$KCPW" "$KC" \
  || fail "couldn't unlock the signing keychain (set LC_KEYCHAIN_PW if you changed the password)"
security find-identity -p codesigning "$KC" | grep -q "$IDENTITY" \
  || fail "identity '$IDENTITY' not found in $KC — run app/scripts/setup-signing.sh"

echo "[0/4] Bumping patch version ..."
VERSION="$(python3 scripts/bump-version.py)"
echo "  -> v$VERSION"
VOL="Library Cleanup $VERSION"
DMG="dist/Library Cleanup-$VERSION.dmg"

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

echo "[3/4] Building DMG ..."
mkdir -p dist
# Keep only the build we're about to make — delete any older DMGs.
find dist -maxdepth 1 -name 'Library Cleanup-*.dmg' ! -name "$(basename "$DMG")" -print -delete
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDBZ "$DMG" >/dev/null
[ -s "$DMG" ] || fail "hdiutil finished but $DMG is missing or empty"

echo "[4/4] Done -> $DMG"
