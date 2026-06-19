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

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .engine import ALL_LAYERS, Engine
from .store import DISCARD, KEEP, Store

LAYERS = ALL_LAYERS
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


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


class FinalizeBody(BaseModel):
    layers: Optional[list[str]] = None


class DeleteBody(BaseModel):
    uuids: list[str]
    dry_run: bool = False


def create_app(store: Optional[Store] = None, engine: Optional[Engine] = None,
               store_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Library Cleanup", version=__version__)
    app.state.store = store or Store(store_path)
    app.state.engine = engine or Engine()
    app.state.job = {"status": "idle"}   # analyze progress (single job at a time)

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
        if job.get("status") == "running":
            return {"started": False, "running": True}
        job.clear()
        job.update({"status": "running", "message": "Starting…", "done": None, "total": None})

        def cb(message, done=None, total=None):
            job.update({"message": message, "done": done, "total": total})

        def run():
            try:
                res = _engine().analyze(body.since, body.until, layers,
                                        excluded=_store().reviewed_uuids(), progress=cb)
                job.update({"status": "done", "summary": res["summary"], "message": "Done"})
            except Exception as e:  # noqa: BLE001
                job.update({"status": "error", "error": str(e)})

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

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

    @app.get("/api/thumb/{uuid}")
    def thumb(uuid: str, px: int = 240):
        data = _engine().thumb_bytes(uuid, px=px)
        if data is None:
            raise HTTPException(404, "no thumbnail available")
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})

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
        for layer in layers:
            for d in st.decisions(layer):
                (keep_ids if d.verdict == KEEP else discard_ids).append(d.uuid)

        feedback_log = None
        if "dedup" in layers:
            from .learning import write_dedup_feedback
            feedback_log = write_dedup_feedback(st, dbpath=_engine().dbpath)

        st.mark_reviewed(keep_ids)
        return {"reviewed": len(keep_ids), "to_delete": discard_ids,
                "feedback_log": feedback_log}

    @app.post("/api/delete")
    def delete(body: DeleteBody):
        """Remove assets from Photos via PhotoKit (macOS shows its own confirm;
        items go to Recently Deleted). Pass dry_run to only resolve/count."""
        from .delete import delete_assets
        return delete_assets(body.uuids, dry_run=body.dry_run)

    @app.post("/api/learn")
    def learn():
        from .learning import run_learning
        return run_learning(_engine().dbpath)

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
