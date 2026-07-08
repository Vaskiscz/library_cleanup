"""Mac app shell: starts the local FastAPI service in a background thread and
points a WebView at it. All processing is on-device; the server binds to
localhost only.
"""
import threading

import toga
from toga.style.pack import COLUMN, Pack

HOST, PORT = "127.0.0.1", 8765


def _serve():
    """Run uvicorn in this (daemon) thread, serving the local API + UI."""
    from photocleanup.diagnostics import log_failure, setup_logging
    setup_logging()
    try:
        import uvicorn

        from photocleanup.server import create_app
        uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")
    except BaseException as e:  # noqa: BLE001
        log_failure("server startup", e)
        raise


class PhotoCleanup(toga.App):
    def startup(self):
        threading.Thread(target=_serve, daemon=True).start()
        self.web = toga.WebView(style=Pack(flex=1))
        self._clear_web_cache()
        self.main_window = toga.MainWindow(title=self.formal_name, size=(1200, 860))
        self.main_window.content = toga.Box(children=[self.web], style=Pack(direction=COLUMN, flex=1))
        self.main_window.show()
        # Ask for Photos (read-write) access here, on the MAIN thread — the dialog
        # can't be presented from the server's background thread (such a request is
        # recorded as a denial and then sticks). Fire-and-forget; analyze just reads
        # the resulting status later.
        try:
            from photocleanup.delete import request_access_async
            request_access_async()
        except Exception:  # noqa: BLE001 — PhotoKit may be unavailable; never block launch
            pass
        # Navigate only once the server is actually accepting connections, so a
        # slow cold start never leaves a blank/error page.
        threading.Thread(target=self._open_when_ready, daemon=True).start()

    def _clear_web_cache(self):
        """WKWebView caches UI assets in the app container, which is keyed to the
        (stable) bundle id — so after an auto-update the previous app.js/app.css can
        be served from cache and the UI keeps running the OLD version even though the
        service is new. Drop the WebView's HTTP cache once at launch so the running
        build's assets always win. Best-effort; never block startup."""
        try:
            from Foundation import NSDate
            from WebKit import WKWebsiteDataStore
            # allWebsiteDataTypes() covers the HTTP disk/memory cache (plus cookies,
            # local storage, etc., of which this UI has none). Since 1970 = everything.
            types = WKWebsiteDataStore.allWebsiteDataTypes()
            WKWebsiteDataStore.defaultDataStore().removeDataOfTypes_modifiedSince_completionHandler_(
                types, NSDate.dateWithTimeIntervalSince1970_(0), lambda: None)
        except Exception:  # noqa: BLE001 — WebKit/pyobjc unavailable; never block launch
            pass

    def _open_when_ready(self):
        import socket
        import time
        healthy = False
        for _ in range(150):                    # up to ~30s
            try:
                with socket.create_connection((HOST, PORT), timeout=0.3):
                    # A socket alone isn't enough — a *different* process could be
                    # holding 8765. Only trust it once /api/health confirms it's
                    # OUR server of the right version (audit #11).
                    if self._health_ok():
                        healthy = True
                        break
            except OSError:
                pass
            time.sleep(0.2)
        self.loop.call_soon_threadsafe(self._open if healthy else self._open_error)

    def _health_ok(self) -> bool:
        import json
        import urllib.request
        from . import __version__
        try:
            with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health", timeout=0.5) as r:
                d = json.load(r)
            return bool(d.get("ok")) and d.get("version") == __version__
        except Exception:
            return False

    def _open(self):
        self.web.url = f"http://{HOST}:{PORT}/"

    def _open_error(self):
        """The local service never came up healthy (port taken, bind failed, or a
        foreign listener on 8765). Show an explanation instead of a blank page."""
        html = (
            "<!doctype html><meta charset='utf-8'>"
            "<style>body{font:15px -apple-system,system-ui,sans-serif;color:#1d1d1f;"
            "background:#f4f4f6;margin:0;height:100vh;display:flex;align-items:center;"
            "justify-content:center;text-align:center}div{max-width:420px;padding:24px}"
            "h1{font-size:19px}p{color:#6e6e73;line-height:1.5}</style>"
            "<div><h1>Couldn’t start Library Cleanup</h1>"
            "<p>The app’s local service didn’t start — usually because port 8765 is "
            "already in use by another program. Quit any other copy of Library Cleanup "
            "(or the app using that port) and reopen.</p></div>")
        try:
            self.web.set_content(f"http://{HOST}:{PORT}/", html)
        except Exception:  # noqa: BLE001 — set_content API varies; fall back to a data URL
            import urllib.parse
            self.web.url = "data:text/html;charset=utf-8," + urllib.parse.quote(html)


def main():
    return PhotoCleanup("Library Cleanup", "cz.vaskiscz.photocleanup")
