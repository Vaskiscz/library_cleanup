"""Local FastAPI service that exposes the photo_cleanup backend to the app's
WebView UI. Binds to localhost only — nothing leaves the device.

Flow: analyze a scope (heavy, on-device) -> pick categories -> fetch candidate
groups (with thumbnails) -> submit keep/remove decisions -> finalise (record
reviewed-state + learning; returns the uuids to remove via PhotoKit next).
"""
from __future__ import annotations

import os
import threading
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .engine import ALL_LAYERS, Engine
from .store import DISCARD, KEEP, Store

LAYERS = ALL_LAYERS
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
KOFI_URL = "https://ko-fi.com/vaclavtrnka"   # voluntary support (opened in the browser)

# Only one retrain runs at a time; extra finalizes while it's busy are skipped
# (the next finalize learns from the full accumulated feedback anyway).
_learning_lock = threading.Lock()


def _start_learning(dbpath: Optional[str]) -> bool:
    """Retrain the keeper model in the background from accumulated feedback.
    Best-effort: failures never affect the review flow. Returns True if a run
    was started, False if one was already in progress."""
    if not _learning_lock.acquire(blocking=False):
        return False

    def run():
        try:
            from .learning import run_learning
            run_learning(dbpath)
        except Exception:
            pass
        finally:
            _learning_lock.release()

    threading.Thread(target=run, daemon=True, name="keeper-learn").start()
    return True


class DecisionIn(BaseModel):
    uuid: str
    verdict: str           # "keep" | "discard"
    group_key: Optional[str] = None
    suggested: bool = False


class DecisionsBody(BaseModel):
    layer: str
    decisions: list[DecisionIn]


class AnalyzeBody(BaseModel):
    since: Optional[str] = None
    until: Optional[str] = None
    layers: Optional[list[str]] = None
    # True only for the explicit "Re-scan" action: re-read the whole library
    # instead of reusing the in-RAM records memo (implicit refreshes stay fast).
    force: bool = False


class FinalizeBody(BaseModel):
    layers: Optional[list[str]] = None


class DeleteBody(BaseModel):
    uuids: list[str]
    dry_run: bool = False


def create_app(store: Optional[Store] = None, engine: Optional[Engine] = None,
               store_path: Optional[str] = None) -> FastAPI:
    # No interactive API docs (smaller attack surface; the UI is the only client).
    app = FastAPI(title="Library Cleanup", version=__version__,
                  docs_url=None, redoc_url=None, openapi_url=None)
    # Reject requests whose Host isn't loopback — defeats DNS-rebinding attacks
    # where a malicious website resolves a domain to 127.0.0.1 to reach this API.
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    # CSRF/cross-origin guard: without this, any web page the user visits can POST
    # to this loopback API (a "simple request" needs no preflight) and trigger
    # side effects like /api/update/apply or /api/delete (audit #4). For any
    # state-changing method, reject when the request's Origin/Referer is present
    # and NOT loopback. Same-origin requests from our own WebView omit Origin or
    # send the loopback origin; a cross-origin attacker page always sends its own.
    @app.middleware("http")
    async def _cross_origin_guard(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            src = request.headers.get("origin") or request.headers.get("referer")
            if src is not None:
                host = urlparse(src).hostname
                if host not in ("127.0.0.1", "localhost", "::1"):
                    return JSONResponse({"detail": "cross-origin request refused"},
                                        status_code=403)
        resp = await call_next(request)
        # Defense-in-depth headers (audit #13). CSP is also set as a <meta> in
        # index.html; nosniff stops content-type confusion on API/static responses.
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")
        # The bundle id (and thus the WebView's on-disk cache) is stable across
        # auto-updates, so a cached UI asset would otherwise survive a version swap
        # and the app would keep running the OLD app.js/app.css after updating.
        # Force revalidation of the document + static UI so every launch loads the
        # JS/CSS that shipped with the running build. Localhost, tiny files — no cost.
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp
    app.state.store = store or Store(store_path)
    app.state.engine = engine or Engine()
    app.state.job = {"status": "idle"}   # analyze progress (single job at a time)
    app.state.update_job = {"status": "idle"}   # self-update download/install progress
    # Serializes the check-then-set on both jobs so two scans (or a scan racing an
    # update) can't both start and corrupt the shared engine / .npz cache (audit #10/#15).
    app.state.job_lock = threading.Lock()

    _UPDATE_BUSY = ("checking", "downloading", "installing", "relaunching")

    def _store() -> Store:
        return app.state.store

    def _engine() -> Engine:
        return app.state.engine

    # ---- API ---------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "layers": list(LAYERS),
                "counts": _store().counts()}

    @app.get("/api/library-stats")
    def library_stats():
        return _engine().library_stats()

    @app.post("/api/analyze")
    def analyze(body: AnalyzeBody):
        """Start the (heavy) analyze as a background job; the UI polls
        /api/progress for the access request, library connection, and counted/
        total processing, then reads the summary when status == 'done'."""
        layers = body.layers or list(LAYERS)
        bad = [l for l in layers if l not in LAYERS]
        if bad:
            raise HTTPException(400, f"unknown layer(s): {bad}")
        job = app.state.job
        with app.state.job_lock:                       # atomic check-then-start
            if job.get("status") == "running":
                return {"started": False, "running": True}
            if app.state.update_job.get("status") in _UPDATE_BUSY:
                return {"started": False, "updating": True}
            job.clear()
            job.update({"status": "running", "message": "Starting…", "done": None, "total": None})

        def cb(message, done=None, total=None, frac=None):
            job.update({"message": message, "done": done, "total": total, "frac": frac})

        def run():
            from .engine import AnalysisCancelled
            try:
                eng = _engine()
                res = eng.analyze(body.since, body.until, layers,
                                  excluded=_store().reviewed_uuids(), progress=cb,
                                  force=body.force)
                job.update({"status": "done", "summary": res["summary"], "message": "Done"})
                # Pre-render grid thumbs into the RAM cache so Review scrolls
                # instantly — like Photos having its thumbnails ready.
                threading.Thread(target=eng.warm_thumbnails, daemon=True).start()
            except AnalysisCancelled:
                job.update({"status": "cancelled", "message": "Cancelled"})
            except Exception as e:  # noqa: BLE001
                from .diagnostics import LOG_PATH, log_failure
                log_failure("analyze", e)
                job.update({"status": "error", "error": str(e), "log": LOG_PATH})

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @app.post("/api/cancel")
    def cancel():
        """Stop a running scan: the analyze thread aborts at its next checkpoint
        and rolls the engine back to a clean slate (frees CPU/battery — the old
        Cancel only flipped the UI while the scan kept running)."""
        _engine().request_cancel()
        return {"cancelling": True}

    @app.get("/api/progress")
    def progress():
        return app.state.job

    @app.get("/api/candidates")
    def candidates(layer: str = "dedup", since: Optional[str] = None,
                   until: Optional[str] = None):
        if layer not in LAYERS:
            raise HTTPException(400, f"unknown layer {layer!r}; use one of {LAYERS}")
        groups = _engine().candidates(layer, since, until,
                                      excluded=_store().reviewed_uuids())
        decided = {d.uuid: d.verdict for d in _store().decisions(layer)}
        for g in groups:
            for ph in g["photos"]:
                ph["decided"] = decided.get(ph["uuid"])
        return {"layer": layer, "since": since, "until": until, "groups": groups}

    @app.get("/api/all-items")
    def all_items(since: Optional[str] = None, until: Optional[str] = None):
        """Manual review feed: every photo + video in range, chronological."""
        groups = _engine().all_items(since, until)
        return {"layer": "all", "since": since, "until": until, "groups": groups}

    @app.get("/api/thumb/{uuid}")
    def thumb(uuid: str, px: int = 240):
        data = _engine().thumb_bytes(uuid, px=px)
        if data is None:
            raise HTTPException(404, "no thumbnail available")
        # no-store: the WebView must not persist any rendered preview to disk —
        # speed comes from the engine's in-RAM cache, not browser disk caching.
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/video/{uuid}")
    def video(uuid: str):
        """Stream the original video file for in-app playback. FileResponse honours
        Range requests so the <video> element can seek/scrub. Stays on 127.0.0.1 and
        is no-store, so nothing is copied off the device."""
        rec = _engine().record(uuid)
        if rec is None or not rec.is_movie or not rec.path or not os.path.exists(rec.path):
            raise HTTPException(404, "no video")
        return FileResponse(rec.path, headers={"Cache-Control": "no-store"})

    @app.post("/api/decisions")
    def decisions(body: DecisionsBody):
        if body.layer not in LAYERS:
            raise HTTPException(400, f"unknown layer {body.layer!r}")
        n = _store().record_decisions(body.layer, [d.model_dump() for d in body.decisions])
        return {"written": n}

    @app.post("/api/finalize")
    def finalize(body: FinalizeBody):
        """Lock in this round across the given layers: record reviewed-state for
        keeps and capture decisions for learning. Does NOT delete (PhotoKit step)
        — returns the uuids to remove so the UI can drive deletion next."""
        layers = body.layers or list(LAYERS)
        st = _store()
        keep_ids, discard_ids = [], []
        acted: dict[str, list[str]] = {}
        for layer in layers:
            # Scope to THIS round: only act on decisions whose uuid is in the
            # current candidates for the layer. Without this, finalize re-emits
            # every discard ever recorded — so a later round could delete photos
            # the user never reviewed this round (audit #2). Fall back to the
            # legacy all-decisions behaviour only if candidates aren't cached.
            payload = _engine().cached_candidates(layer)
            present = ({p["uuid"] for g in payload for p in g["photos"]}
                       if payload is not None else None)
            layer_acted = []
            for d in st.decisions(layer):
                if present is not None and d.uuid not in present:
                    continue                       # stale decision from a prior round
                (keep_ids if d.verdict == KEEP else discard_ids).append(d.uuid)
                layer_acted.append(d.uuid)
            acted[layer] = layer_acted

        feedback_log = None
        if "dedup" in layers:
            from .learning import write_dedup_feedback
            feedback_log = write_dedup_feedback(st, dbpath=_engine().dbpath)
        # Flat layers learn from explicit verdicts too: which flagged kinds the
        # user keeps (false positives) drives per-kind suppression.
        wrote_flat = False
        for flat in ("screenshots", "expired"):
            if flat in layers:
                from .learning import write_flat_feedback
                payload = _engine().cached_candidates(flat) or []
                kind_map = {p["uuid"]: p.get("kind", "generic")
                            for g in payload for p in g["photos"]}
                wrote_flat = bool(write_flat_feedback(st, flat, kind_map)) or wrote_flat

        st.mark_reviewed(keep_ids)
        # This round is done: drop the acted-on decision rows so they can never be
        # re-applied or re-deleted in a later round (audit #2). Keeps are also in
        # `reviewed` (permanently excluded); discards, if still present next scan,
        # are re-detected fresh rather than silently re-deleted.
        for layer, uuids in acted.items():
            st.clear_decisions(layer, uuids)
        # New keep/discard labels just landed — retrain the keeper model in the
        # background so the next round's suggestions reflect this review.
        learning_started = bool(feedback_log or wrote_flat) and _start_learning(_engine().dbpath)
        return {"reviewed": len(keep_ids), "to_delete": discard_ids,
                "feedback_log": feedback_log, "learning_started": learning_started}

    @app.post("/api/delete")
    def delete(body: DeleteBody):
        """Remove assets from Photos via PhotoKit (macOS shows its own confirm;
        items go to Recently Deleted). Pass dry_run to only resolve/count."""
        from .delete import delete_assets
        result = delete_assets(body.uuids, dry_run=body.dry_run)
        # Prune the deleted assets from the engine's in-RAM records so the next
        # re-scan re-clusters the survivors WITHOUT re-parsing the whole library
        # via osxphotos (identical results, near-instant, still RAM-only). Only
        # the assets actually removed this call (requested minus unmatched).
        if not body.dry_run and result.get("deleted"):
            unmatched = set(result.get("unmatched") or [])
            _engine().forget([u for u in body.uuids if u not in unmatched])
        return result

    @app.get("/api/diagnostics")
    def diagnostics():
        from .diagnostics import LOG_PATH, library_access_ok
        ok, detail = library_access_ok()
        return {"log_path": LOG_PATH, "log_exists": os.path.exists(LOG_PATH),
                "library_readable": ok, "detail": detail}

    @app.post("/api/open-log")
    def open_log():
        """Reveal the diagnostic log in Finder so it's easy to share."""
        import subprocess
        from .diagnostics import LOG_PATH
        if os.path.exists(LOG_PATH):
            subprocess.run(["open", "-R", LOG_PATH], check=False)
            return {"opened": True, "path": LOG_PATH}
        return {"opened": False, "path": LOG_PATH}

    @app.post("/api/donate")
    def donate():
        """Open the Ko-fi support page in the default browser (voluntary tips).
        Opening externally keeps donations out of the app entirely — no payment
        handling, no PII, and nothing loads inside the WebView."""
        import subprocess
        subprocess.run(["open", KOFI_URL], check=False)
        return {"opened": True, "url": KOFI_URL}

    @app.post("/api/learn")
    def learn():
        from .learning import run_learning
        return run_learning(_engine().dbpath)

    # ---- self-update -------------------------------------------------------
    @app.get("/api/update/check")
    def update_check():
        """Compare the running version to GitHub's latest release. Contacts only
        GitHub's public API; never sends anything about the library."""
        from .updater import check
        return check()

    @app.post("/api/update/apply")
    def update_apply():
        """Download the latest release DMG and install it in the background, then
        relaunch. The UI polls /api/update/status. The asset URL is re-derived
        server-side from a fresh check() — never taken from the client."""
        from . import updater
        job = app.state.update_job
        # Atomically reserve the update slot and refuse if a scan is running — the
        # updater ends in os._exit, which would truncate an in-flight .npz write
        # (audit #15). check()/download run OUTSIDE the lock (they do network I/O).
        with app.state.job_lock:
            if job.get("status") in _UPDATE_BUSY:
                return {"started": False, "running": True}
            if app.state.job.get("status") == "running":
                return {"started": False, "scanning": True}
            job.clear()
            job.update({"status": "checking"})
        info = updater.check()
        if not info.get("available") or not info.get("url"):
            job.clear(); job.update({"status": "idle"})
            return {"started": False, "available": False, "error": info.get("error")}
        if not info.get("can_install"):     # dev/source run — offer the page instead
            job.clear(); job.update({"status": "idle"})
            return {"started": False, "can_install": False, "html_url": info.get("html_url")}

        job.clear()
        job.update({"status": "downloading", "frac": 0.0, "message": "Downloading update…"})

        def prog(done, total):
            job.update({"frac": (done / total) if total else None,
                        "done": done, "total": total})

        def run():
            try:
                dmg = updater.download_dmg(info["url"], progress=prog)
                job.update({"status": "installing", "frac": 1.0, "message": "Installing…"})
                updater.apply_update(dmg)          # spawns helper + schedules our exit
                job.update({"status": "relaunching", "message": "Relaunching…"})
            except Exception as e:  # noqa: BLE001
                from .diagnostics import log_failure
                log_failure("update", e)
                job.update({"status": "error", "error": str(e)})

        threading.Thread(target=run, daemon=True, name="self-update").start()
        return {"started": True}

    @app.get("/api/update/status")
    def update_status():
        return app.state.update_job

    @app.post("/api/update/open-page")
    def update_open_page():
        """Open the release page in the browser (manual-download fallback)."""
        from .updater import check, open_release_page
        info = check()
        url = info.get("html_url")
        return {"opened": bool(url) and open_release_page(url), "html_url": url}

    # ---- static UI (served last so /api/* wins) ----------------------------
    if os.path.isdir(os.path.join(WEB_DIR, "static")):
        app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")),
                  name="static")

    @app.get("/")
    def index():
        idx = os.path.join(WEB_DIR, "index.html")
        if os.path.exists(idx):
            return FileResponse(idx)
        return Response("<h2>Library Cleanup service is running.</h2>",
                        media_type="text/html")

    return app
