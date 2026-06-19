"""Packaging spike: a minimal Toga + WebView shell that imports the reused
photo_cleanup backend and reports whether the bundle is wired up correctly.

No library writes, no heavy scan — this only proves the .app can ship the BE.
"""
import html
import sys

import toga
from toga.style.pack import COLUMN, Pack


def _backend_report() -> str:
    """Import the reused backend + key deps; report what loaded (and python info)."""
    lines = [f"Python {sys.version.split()[0]}"]
    checks = [
        ("photo_cleanup", "photo_cleanup"),
        ("osxphotos", "osxphotos"),
        ("numpy", "numpy"),
        ("PIL", "PIL"),
        ("photoscript", "photoscript"),
    ]
    for label, mod in checks:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "ok")
            lines.append(f"✓ {label} {ver}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"✗ {label} — {type(e).__name__}: {e}")
    # Vision is loaded lazily by the BE; confirm the bridge is bundled too.
    try:
        import Vision  # noqa: F401
        lines.append("✓ Vision (pyobjc) bridge")
    except Exception as e:  # noqa: BLE001
        lines.append(f"✗ Vision (pyobjc) — {type(e).__name__}: {e}")
    return "\n".join(lines)


class PhotoCleanup(toga.App):
    def startup(self):
        report = _backend_report()
        # Write to a known file so the spike can verify runtime imports headlessly.
        try:
            import os
            with open(os.path.expanduser("~/pc_spike_report.txt"), "w") as fh:
                fh.write(report + "\n")
        except Exception:
            pass
        body = "".join(f"<li>{html.escape(line)}</li>" for line in report.splitlines())
        page = (
            "<html><body style='font:14px -apple-system;padding:16px'>"
            "<h2>Photo Cleanup — packaging spike</h2>"
            "<p>Backend bundle check:</p>"
            f"<ul>{body}</ul>"
            "<p style='color:#888'>If every line is ✓, briefcase bundling of the "
            "reused backend works.</p></body></html>"
        )
        webview = toga.WebView(style=Pack(flex=1))
        webview.set_content("https://localhost/", page)
        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = toga.Box(children=[webview], style=Pack(direction=COLUMN, flex=1))
        self.main_window.show()


def main():
    return PhotoCleanup("Photo Cleanup", "cz.vaskiscz.photocleanup")
