# Distributing Library Cleanup

The app is signed with an Apple **Developer ID Application** certificate and
**notarized** by Apple, so macOS opens it with no Gatekeeper warning. Everything
still runs **on-device**; nothing is uploaded (the notary check happens on the
maintainer's machine at build time, not on users' Macs).

Artifact: `dist/Library-Cleanup.dmg` (stable file name; versioned volume label).

**Requirements for recipients:** an **Apple Silicon** Mac (the build is arm64
only) on a recent macOS, plus the two permissions below (Full Disk Access is
mandatory, the app can't read the library without it).

---

## For people you share it with

1. **Open the `.dmg`** and drag **Library Cleanup** to **Applications**, then
   open it normally. No security bypass is needed: the app is notarized.
2. **Grant Full Disk Access**: System Settings ▸ Privacy & Security ▸ **Full
   Disk Access** ▸ add **Library Cleanup**. This is required so it can read the
   Photos library on-device. (FDA is never auto-prompted; it must be added here.)
3. **Use it.** The first time you confirm a removal, macOS asks for **Photos**
   access; click **Allow**. Removed items go to **Recently Deleted** (30 days).

---

## For the maintainer

Signing uses the **Developer ID Application** identity from the login keychain
(`Developer ID Application: VÁCLAV TRNKA (993Q8KJAJS)`), and notarization uses a
notarytool keychain profile (`library-cleanup-notary`). The stable identity
matters twice over: macOS binds Full Disk Access / Photos grants to it (grants
persist across rebuilds), and the in-app updater pins it (an update signed by
anyone else is refused).

```sh
# one-time per machine: verify the cert + store notary credentials
# (asks for your Apple ID and an APP-SPECIFIC password, interactively)
bash app/scripts/setup-signing.sh

# build + sign + notarize + staple + package
# (bumps the patch version automatically; --minor for a public release)
bash app/scripts/build-signed-dmg.sh
# -> app/dist/Library-Cleanup.dmg
```

Notes:

- Notarization adds roughly 1-5 minutes per build. For quick local iterations,
  `LC_SKIP_NOTARIZE=1 bash app/scripts/build-signed-dmg.sh` skips it; never
  release a DMG built that way.
- The version is bumped automatically by the build script (via
  `scripts/bump-version.py`, which keeps `pyproject.toml` and `__init__.py` in
  lockstep; never edit either by hand).
- The build hard-fails if the signing identity's designated requirement differs
  from `scripts/released-requirement.txt` (changing identity breaks auto-update
  and TCC grants for existing users). Accept an intentional change with
  `LC_ALLOW_IDENTITY_CHANGE=1`.
- History: before 0.8.0 the app used a stable self-signed certificate in a
  dedicated keychain (`LC_KEYCHAIN_PW`), and recipients had to bypass Gatekeeper
  once. That flow is retired. Remove the old keychain with:
  `security delete-keychain ~/Library/Keychains/library-cleanup-signing.keychain-db`.
