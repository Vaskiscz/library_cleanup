#!/bin/bash
# One-time signing setup for Library Cleanup (Developer ID + notarization).
#
# The app is signed with the Apple Developer ID Application certificate from
# the login keychain and notarized with notarytool. This script checks the
# certificate is installed and stores the notary credentials as a keychain
# profile, so builds never need credentials in the environment.
#
# Run once:  bash app/scripts/setup-signing.sh
# It will interactively ask for:
#   - your Apple ID (developer account email)
#   - an APP-SPECIFIC password (create at appleid.apple.com ▸ Sign-In and
#     Security ▸ App-Specific Passwords — NOT your Apple ID password)
#
# History: before 0.8.0 the app was signed with a local self-signed cert in a
# dedicated keychain (LC_KEYCHAIN_PW). That flow is retired; the old keychain
# can be removed with:
#   security delete-keychain "$HOME/Library/Keychains/library-cleanup-signing.keychain-db"
set -euo pipefail

IDENTITY="Developer ID Application: VÁCLAV TRNKA (993Q8KJAJS)"
TEAM_ID="993Q8KJAJS"
NOTARY_PROFILE="library-cleanup-notary"

if ! security find-identity -v -p codesigning | grep -q "Developer ID Application: VÁCLAV TRNKA"; then
  echo "ERROR: '$IDENTITY' is not in the keychain." >&2
  echo "Install it via Xcode ▸ Settings ▸ Accounts ▸ Manage Certificates," >&2
  echo "or download it from https://developer.apple.com/account/resources/certificates" >&2
  exit 1
fi
echo "Signing identity present: $IDENTITY"

# Store notary credentials as a keychain profile. Prefer the App Store Connect
# API key already on this machine (shared with the Selects TestFlight flow) —
# no app-specific password needed. Fall back to the interactive Apple ID flow.
ASC_KEY="$HOME/.appstoreconnect/private_keys/AuthKey_4C2QG76G9T.p8"
ASC_KEY_ID="4C2QG76G9T"
ASC_ISSUER_ID="dd1a4dbf-926e-4e94-8cff-8485db2bda93"
echo "Storing notary credentials as keychain profile '$NOTARY_PROFILE' ..."
if [ -f "$ASC_KEY" ]; then
  xcrun notarytool store-credentials "$NOTARY_PROFILE" \
    --key "$ASC_KEY" --key-id "$ASC_KEY_ID" --issuer "$ASC_ISSUER_ID"
else
  echo "(ASC API key not found at $ASC_KEY; falling back to Apple ID +"
  echo " app-specific password — create one at appleid.apple.com)"
  xcrun notarytool store-credentials "$NOTARY_PROFILE" --team-id "$TEAM_ID"
fi

echo "Done. Build with: bash app/scripts/build-signed-dmg.sh"
