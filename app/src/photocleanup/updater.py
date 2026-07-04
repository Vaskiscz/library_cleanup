"""In-app updater: check GitHub Releases for a newer build, download the DMG,
swap the installed .app in place, and relaunch.

Privacy: the ONLY network call this makes is an anonymous GET to GitHub's public
Releases API (and the release asset download). No account, no telemetry, nothing
about your photos ever leaves the device — the on-device rule for library data is
untouched. The check runs at launch and the user is always prompted before any
download.

The app is self-signed with a *stable* identity, so swapping the bundle keeps the
existing Full Disk Access / Photos grants (TCC matches the signing identity, not
its trust chain) and, because we strip the download's quarantine flag, Gatekeeper
won't re-prompt on relaunch.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import tempfile
import threading
import urllib.request
from typing import Callable, Optional

from . import __version__


def _ssl_context() -> ssl.SSLContext:
    """A context with a real CA bundle. Bundled/macOS Pythons often can't find
    system CAs for urllib (CERTIFICATE_VERIFY_FAILED), so prefer certifi's."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()

REPO = "Vaskiscz/library_cleanup"
ASSET_NAME = "Library-Cleanup.dmg"           # the stable DMG name the build produces
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_ALLOWED_PREFIX = f"https://github.com/{REPO}/releases/download/"


def _parse(tag: str) -> Optional[tuple]:
    """'v0.2.0' / '0.2.0' -> (0, 2, 0). None if it isn't a plain numeric tag."""
    s = (tag or "").strip().lstrip("vV")
    parts = s.split(".")
    try:
        return tuple(int(p) for p in parts) if parts and all(parts) else None
    except ValueError:
        return None


def is_newer(latest: tuple, current: tuple) -> bool:
    """True if `latest` is a strictly higher version than `current` (any length)."""
    n = max(len(latest), len(current))
    lp = tuple(latest) + (0,) * (n - len(latest))
    cp = tuple(current) + (0,) * (n - len(current))
    return lp > cp


def app_bundle_path() -> Optional[str]:
    """Absolute path of the enclosing `*.app` bundle, or None when running from
    source (dev) — in which case we can't self-install and only offer a link."""
    p = os.path.abspath(__file__)
    while p and p != "/":
        if p.endswith(".app") and os.path.isdir(p):
            return p
        p = os.path.dirname(p)
    return None


def check() -> dict:
    """Ask GitHub for the latest release and compare to the running version.

    Returns {available, current, latest, url, size, notes, html_url, can_install}.
    Never raises — on any failure (offline, rate-limited, malformed) returns
    {available: False, error: ...} so the UI just stays quiet.
    """
    current = _parse(__version__) or (0,)
    try:
        req = urllib.request.Request(
            API_URL, headers={"User-Agent": f"LibraryCleanup/{__version__}",
                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=6, context=_ssl_context()) as r:  # nosec
            data = json.load(r)
    except Exception as e:  # noqa: BLE001 — offline / rate-limit / bad JSON: stay silent
        return {"available": False, "current": __version__, "error": str(e)}

    tag = data.get("tag_name") or ""
    latest = _parse(tag)
    assets = data.get("assets") or []
    asset = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    url = asset.get("browser_download_url") if asset else None
    available = bool(latest) and is_newer(latest, current) and bool(url)
    return {
        "available": available,
        "current": __version__,
        "latest": tag.lstrip("vV") if tag else None,
        "url": url,
        "size": asset.get("size") if asset else None,
        "notes": (data.get("body") or "").strip(),
        "html_url": data.get("html_url"),
        "can_install": app_bundle_path() is not None,
    }


def download_dmg(url: str, progress: Optional[Callable[[int, int], None]] = None) -> str:
    """Download the release DMG to a temp file and return its path. Only accepts
    URLs under this repo's release-download prefix (defence-in-depth even though
    the URL comes from our own check())."""
    if not url or not url.startswith(_ALLOWED_PREFIX):
        raise ValueError("refusing to download an unexpected URL")
    fd, dest = tempfile.mkstemp(prefix="library-cleanup-update-", suffix=".dmg")
    os.close(fd)
    req = urllib.request.Request(url, headers={"User-Agent": f"LibraryCleanup/{__version__}"})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as r, \
            open(dest, "wb") as out:  # nosec
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return dest


# Bash helper: waits for the running app to quit, swaps the bundle, relaunches.
# Kept minimal and defensive — it must never leave the user without an app.
_HELPER = r"""#!/bin/bash
set -u
PID="$1"; DMG="$2"; APP="$3"; SELF="$0"
# 1) wait (up to ~20s) for the running app to exit so `open` relaunches fresh
for _ in $(seq 1 100); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done
MP="$(mktemp -d /tmp/lc-update.XXXXXX)"
if hdiutil attach -nobrowse -noautoopen -quiet -mountpoint "$MP" "$DMG"; then
  SRC="$(/usr/bin/find "$MP" -maxdepth 1 -name '*.app' -print -quit)"
  if [ -n "$SRC" ]; then
    rm -rf "$APP.new" "$APP.bak" 2>/dev/null
    if ditto "$SRC" "$APP.new"; then          # aborts here if it can't write: original intact
      mv "$APP" "$APP.bak" && mv "$APP.new" "$APP" || mv "$APP.bak" "$APP"
      rm -rf "$APP.bak" 2>/dev/null
      xattr -dr com.apple.quarantine "$APP" 2>/dev/null
    fi
  fi
  hdiutil detach "$MP" -quiet 2>/dev/null || hdiutil detach "$MP" -force -quiet 2>/dev/null
fi
rmdir "$MP" 2>/dev/null
rm -f "$DMG" 2>/dev/null
open "$APP" 2>/dev/null
rm -f "$SELF" 2>/dev/null
"""


def apply_update(dmg_path: str, exit_delay: float = 1.5) -> str:
    """Spawn the detached swap-and-relaunch helper, then schedule this process to
    exit so the helper can replace the (now-quit) bundle. Returns the app path.
    Raises if we can't locate the bundle (dev/source run — nothing to replace)."""
    app_path = app_bundle_path()
    if not app_path:
        raise RuntimeError("not running from an .app bundle — cannot self-install")

    fd, script = tempfile.mkstemp(prefix="library-cleanup-relaunch-", suffix=".sh")
    with os.fdopen(fd, "w") as fh:
        fh.write(_HELPER)
    os.chmod(script, 0o755)

    subprocess.Popen(                                  # detached: survives our exit
        ["/bin/bash", script, str(os.getpid()), dmg_path, app_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)

    # Quit shortly after, so the /api/update/status poll can show "Relaunching…"
    # before the connection drops. os._exit ends every thread (incl. uvicorn).
    threading.Timer(exit_delay, os._exit, args=(0,)).start()
    return app_path


def open_release_page(url: str) -> bool:
    """Open the release page in the default browser (manual-download fallback)."""
    if not url or not url.startswith(f"https://github.com/{REPO}/"):
        return False
    subprocess.run(["open", url], check=False)
    return True
