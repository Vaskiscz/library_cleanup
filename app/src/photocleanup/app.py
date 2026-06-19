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
        self.main_window = toga.MainWindow(title=self.formal_name, size=(1200, 860))
        self.main_window.content = toga.Box(children=[self.web], style=Pack(direction=COLUMN, flex=1))
        self.main_window.show()
        # Navigate only once the server is actually accepting connections, so a
        # slow cold start never leaves a blank/error page.
        threading.Thread(target=self._open_when_ready, daemon=True).start()

    def _open_when_ready(self):
        import socket
        import time
        for _ in range(150):                    # up to ~30s
            try:
                with socket.create_connection((HOST, PORT), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.2)
        self.loop.call_soon_threadsafe(self._open)

    def _open(self):
        self.web.url = f"http://{HOST}:{PORT}/"


def main():
    return PhotoCleanup("Library Cleanup", "cz.vaskiscz.photocleanup")
