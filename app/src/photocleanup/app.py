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
    import uvicorn

    from photocleanup.server import create_app
    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")


class PhotoCleanup(toga.App):
    def startup(self):
        threading.Thread(target=_serve, daemon=True).start()
        self.web = toga.WebView(style=Pack(flex=1))
        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = toga.Box(children=[self.web], style=Pack(direction=COLUMN, flex=1))
        self.main_window.show()
        # Navigate once uvicorn has had a beat to bind the port.
        threading.Timer(
            1.0, lambda: self.loop.call_soon_threadsafe(self._open)
        ).start()

    def _open(self):
        self.web.url = f"http://{HOST}:{PORT}/"


def main():
    return PhotoCleanup("Library Cleanup", "cz.vaskiscz.photocleanup")
