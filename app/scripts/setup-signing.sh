#!/bin/bash
# Create a STABLE self-signed code-signing identity for Library Cleanup.
#
# Why: an unsigned (ad-hoc) app gets a new code hash on every build, so macOS
# Full Disk Access / Photos grants stop matching after a rebuild. Signing every
# build with the same self-signed cert gives a stable identity, so those grants
# persist across rebuilds. (It is NOT notarized — recipients still do the
# one-time right-click->Open.)
#
# Run once:  bash app/scripts/setup-signing.sh
# Then:      cd app && uvx briefcase package macOS -i "Library Cleanup Self-Signed" --no-notarize
#
# Undo:      security delete-keychain "$HOME/Library/Keychains/library-cleanup-signing.keychain-db"
set -euo pipefail

CERT_CN="Library Cleanup Self-Signed"
KC="$HOME/Library/Keychains/library-cleanup-signing.keychain-db"
KCPW="${LC_KEYCHAIN_PW:-libraryclean}"   # local dev keychain password (not secret)
if [ -z "${LC_KEYCHAIN_PW:-}" ]; then
  echo "WARNING: using the default signing-keychain password. On a shared or CI host, set" >&2
  echo "         LC_KEYCHAIN_PW to a private value so the signing key can't be trivially unlocked." >&2
fi

if security find-identity -v -p codesigning 2>/dev/null | grep -q "$CERT_CN"; then
  echo "Identity already present: $CERT_CN"
  exit 0
fi

# 1) Dedicated keychain with a password we know, so codesign never needs your
#    login password and never prompts.
security create-keychain -p "$KCPW" "$KC" 2>/dev/null || true
security set-keychain-settings -lt 3600 "$KC"   # auto-lock after 1h idle / on sleep (audit #18)
security unlock-keychain -p "$KCPW" "$KC"

# 2) Self-signed certificate with the code-signing extended key usage.
TMP="$(mktemp -d)"
cat > "$TMP/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = $CERT_CN
[v3]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -config "$TMP/cert.cnf" 2>/dev/null

# 3) Import the key + cert as separate PEMs (avoids the OpenSSL-3/macOS PKCS12
#    MAC incompatibility), let codesign use the key without prompting, and add
#    the keychain to the search list.
security import "$TMP/key.pem"  -k "$KC" -A
security import "$TMP/cert.pem" -k "$KC" -A
security set-key-partition-list -S apple-tool:,apple: -s -k "$KCPW" "$KC" >/dev/null 2>&1
security list-keychains -d user -s login.keychain-db "$KC"
rm -rf "$TMP"

echo "Created code-signing identity:"
security find-identity -v -p codesigning | grep "$CERT_CN"
