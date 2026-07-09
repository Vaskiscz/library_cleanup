# Distributing Library Cleanup

The app is **ad-hoc signed, not notarized** (no Apple Developer account). It runs
fine, but because Apple hasn't notarized it, macOS Gatekeeper warns on first
open and a recipient has to allow it once. Everything still runs **on-device** —
nothing is uploaded.

Artifact: `dist/Library Cleanup-<version>.dmg` (~45 MB).

**Requirements for recipients:** an **Apple Silicon** Mac (the build is arm64
only) on a recent macOS, plus the two permissions below (Full Disk Access is
mandatory — the app can't read the library without it).

---

## For people you share it with

1. **Open the `.dmg`** and drag **Library Cleanup** to **Applications**.
2. **Allow it to open once.** macOS will say it "can't be opened" / "Apple cannot
   check it for malicious software." Either:
   - **System Settings ▸ Privacy & Security**, scroll down, click **Open Anyway**
     next to Library Cleanup, then open it again; **or**
   - if it says **"damaged"** / won't open (common for ad-hoc apps after
     download), open **Terminal** and run:
     ```sh
     xattr -cr "/Applications/Library Cleanup.app"
     ```
     then open it normally. (This just removes the download-quarantine flag; it
     doesn't change the app.)
3. **Grant Full Disk Access** — System Settings ▸ Privacy & Security ▸ **Full
   Disk Access** ▸ add **Library Cleanup**. This is required so it can read the
   Photos library on-device. (FDA is never auto-prompted; it must be added here.)
4. **Use it.** The first time you confirm a removal, macOS asks for **Photos**
   access — click **Allow**. Removed items go to **Recently Deleted** (30 days).

> Why the friction: without notarization macOS can't verify the developer.
> All processing is local; the app only talks to `127.0.0.1`.

---

## For the maintainer

The DMG is signed with a **stable self-signed identity** (not notarized). The
stable identity matters: macOS binds Full Disk Access / Photos grants to the
signing identity, so those grants **persist across rebuilds** (an ad-hoc build
gets a new hash each time and silently loses the grants).

Both scripts require `LC_KEYCHAIN_PW` — the password of the dedicated signing
keychain. It must be a private value: this key is the trust anchor the in-app
updater pins, so anything that can unlock the keychain can sign an update every
user would install.

```sh
# one-time per machine: create the self-signed code-signing cert
LC_KEYCHAIN_PW='<private value>' bash app/scripts/setup-signing.sh

# build + sign + package (also bumps the patch version automatically;
# pass --minor for a public release)
LC_KEYCHAIN_PW='<private value>' bash app/scripts/build-signed-dmg.sh
# -> app/dist/Library-Cleanup.dmg (stable file name; versioned volume label)
```

The version is bumped automatically by the build script (via
`scripts/bump-version.py`, which keeps `pyproject.toml` and `__init__.py` in
lockstep — never edit either by hand). The cert is kept **untrusted** (never a
system trust root) — that's all TCC needs. Remove it with:
`security delete-keychain ~/Library/Keychains/library-cleanup-signing.keychain-db`.
Change the keychain password without touching the key (grants + identity pin
survive) with:
`security set-keychain-password ~/Library/Keychains/library-cleanup-signing.keychain-db`.

---

## Upgrade path: signed + notarized (no warnings)

When you get a paid **Apple Developer Program** membership ($99/yr) and a
**Developer ID Application** certificate in your keychain:

```sh
# one-time: store notarization creds in the keychain
xcrun notarytool store-credentials briefcase-LibraryCleanup \
  --apple-id "you@example.com" --team-id "<TEAMID>" --password "<app-specific-password>"

# sign + notarize + staple, then package
cd app
uvx briefcase package macOS \
  --identity "Developer ID Application: Your Name (TEAMID)"
```

briefcase will sign the bundle, submit it to Apple's notary service, staple the
ticket, and produce a DMG that opens with **no Gatekeeper warning** — recipients
can skip steps 2 above entirely (Full Disk Access + Photos access are still
required, as for any app that touches the library). No code changes are needed;
only the packaging command differs.
