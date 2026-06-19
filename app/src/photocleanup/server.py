"""Local FastAPI service that exposes the photo_cleanup backend to the app's
WebView UI. Binds to localhost only — nothing leaves the device.

The service is the only thing the UI talks to: scan a date range, fetch
candidate groups (with thumbnails), submit keep/discard decisions, finalise
(record reviewed-state + learning), and retrain the model.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from .engine import Engine
from .store import DISCARD, KEEP, Store

LAYERS = ("dedup", "screenshots")


class DecisionIn(BaseModel):
    uuid: str
    verdict: str           # "keep" | "discard"
    group_key: Optional[str] = None
    suggested: bool = False


class DecisionsBody(BaseModel):
    layer: str
    decisions: list[DecisionIn]


class ScanBody(BaseModel):
    since: Optional[str] = None
    until: Optional[str] = None
    rescan: bool = False


class FinalizeBody(BaseModel):
    layer: str = "dedup"


def create_app(store: Optional[Store] = None, engine: Optional[Engine] = None,
               store_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Photo Cleanup", version="0.0.1")
    app.state.store = store or Store(store_path)
    app.state.engine = engine or Engine()

    def _store() -> Store:
        return app.state.store

    def _engine() -> Engine:
        return app.state.engine

    @app.get("/")
    def root():
        return Response(
            "<h2>Photo Cleanup service is running.</h2>"
            "<p>The review UI arrives in Phase 3. API is under <code>/api</code>.</p>",
            media_type="text/html",
        )

    @app.get("/api/health")
    def health():
        return {"ok": True, "layers": LAYERS, "counts": _store().counts()}

    @app.post("/api/scan")
    def scan(body: ScanBody):
        recs = _engine().load_records(body.since, body.until,
                                      excluded=_store().reviewed_uuids(),
                                      force_rescan=body.rescan)
        return {"scanned": len(recs), "since": body.since, "until": body.until}

    @app.get("/api/candidates")
    def candidates(layer: str = "dedup", since: Optional[str] = None,
                   until: Optional[str] = None):
        if layer not in LAYERS:
            raise HTTPException(400, f"unknown layer {layer!r}; use one of {LAYERS}")
        eng = _engine()
        recs = eng.load_records(since, until, excluded=_store().reviewed_uuids())
        if layer == "dedup":
            groups = eng.dedup_payload(eng.dedup_groups(recs))
        else:  # screenshots
            groups = eng.screenshot_payload(eng.screenshot_items(recs))
        # overlay any decisions already made this round
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
        """Lock in this round: record reviewed-state for keeps and capture the
        decisions for learning. Does NOT delete (that's the PhotoKit step) —
        returns the discard uuids so the UI can drive deletion next."""
        if body.layer not in LAYERS:
            raise HTTPException(400, f"unknown layer {body.layer!r}")
        st = _store()
        decs = st.decisions(body.layer)
        keep_ids = [d.uuid for d in decs if d.verdict == KEEP]
        discard_ids = [d.uuid for d in decs if d.verdict == DISCARD]

        feedback_log = None
        if body.layer == "dedup":
            from .learning import write_dedup_feedback
            feedback_log = write_dedup_feedback(st, dbpath=_engine().dbpath)

        st.mark_reviewed(keep_ids, body.layer)
        return {"reviewed": len(keep_ids), "to_delete": discard_ids,
                "feedback_log": feedback_log}

    @app.post("/api/learn")
    def learn():
        from .learning import run_learning
        return run_learning(_engine().dbpath)

    return app
