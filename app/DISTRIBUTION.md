# Distributing Library Cleanup

The app is **ad-hoc signed, not notarized** (no Apple Developer account). It runs
fine, but because Apple hasn't notarized it, macOS Gatekeeper warns on first
open and a recipient has to allow it once. Everything still runs **on-device** —
nothing is uploaded.

Artifact: `dist/Library Cleanup-<version>.dmg`.

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

Build the DMG (ad-hoc signed, no notarization):

```sh
cd app
uvx briefcase package macOS --adhoc-sign
# -> dist/Library Cleanup-<version>.dmg
```

Bump the version in `app/pyproject.toml` (`[tool.briefcase] version`) before
packaging a new release.

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
