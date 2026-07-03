"""On-device diagnostic logging. Writes to a standard macOS app-log location so
a user can grab the file and send it to the developer when something fails.

Nothing leaves the device automatically — this only writes a local file.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import traceback

LOG_DIR = os.path.expanduser("~/Library/Logs/Library Cleanup")
LOG_PATH = os.path.join(LOG_DIR, "library-cleanup.log")

_LOGGER = "librarycleanup"


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER)


def setup_logging() -> logging.Logger:
    """Idempotent: attach a rotating file handler the first time it's called."""
    log = get_logger()
    if log.handlers:
        return log
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        # The core library logs its swallowed-but-interesting failures (Vision
        # errors, unreadable feedback files, …) to "photo_cleanup" — capture them
        # in the same file so app diagnostics show the whole story.
        core = logging.getLogger("photo_cleanup")
        core.addHandler(handler)
        core.setLevel(logging.INFO)
    except Exception:
        pass  # never let logging setup crash the app
    return log


def log_environment() -> None:
    try:
        from . import __version__
        version = __version__
    except Exception:
        version = "?"
    get_logger().info(
        "=== Library Cleanup v%s | Python %s | macOS %s | %s ===",
        version, platform.python_version(), platform.mac_ver()[0] or "?", platform.machine())


def library_access_ok() -> tuple[bool, str]:
    """Best-effort check that we can actually read the Photos library DB. A
    failure here is almost always missing Full Disk Access."""
    db = os.path.expanduser("~/Pictures/Photos Library.photoslibrary/database/Photos.sqlite")
    try:
        with open(db, "rb") as fh:
            fh.read(16)
        return True, db
    except Exception as e:  # noqa: BLE001
        return False, f"{db}: {type(e).__name__}: {e}"


def _scrub(s: str) -> str:
    """Replace the user's home path with ~ so a shared log doesn't leak the
    username / home layout."""
    return s.replace(os.path.expanduser("~"), "~")


def log_failure(context: str, exc: BaseException) -> str:
    """Record a failure (traceback + access diagnosis). Returns the log path."""
    setup_logging()
    log = get_logger()
    log.error("FAILURE during %s: %s: %s", context, type(exc).__name__, _scrub(str(exc)))
    log.error("%s", _scrub("".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()))
    ok, detail = library_access_ok()
    log.error("Full Disk Access (library readable): %s — %s", ok, _scrub(detail))
    return LOG_PATH
