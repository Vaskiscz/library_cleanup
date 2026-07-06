"""App-owned review state — a local SQLite database.

The Mac app keeps its own decision/review state here instead of writing
``cleanup:*`` keywords back to Photos (the CLI's mechanism). Everything is
on-device. The CLI and the app can both run against the same library
independently; the app additionally honours the library's ``reviewed:keep``
keyword so anything the CLI already locked is never re-shown.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

# verdict values stored in the `decision` table
KEEP = "keep"
DISCARD = "discard"


def default_db_path() -> str:
    """Per-user app data location (created on first use)."""
    base = os.path.expanduser("~/Library/Application Support/Library Cleanup")
    return os.path.join(base, "state.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Decision:
    uuid: str
    layer: str
    verdict: str
    group_key: Optional[str]
    suggested: bool
    decided_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision (
    uuid       TEXT NOT NULL,
    layer      TEXT NOT NULL,
    group_key  TEXT,
    suggested  INTEGER NOT NULL DEFAULT 0,
    verdict    TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (uuid, layer)
);
CREATE TABLE IF NOT EXISTS reviewed (
    uuid      TEXT PRIMARY KEY,
    layer     TEXT,
    locked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_layer ON decision(layer);
"""


class Store:
    """Thin SQLite wrapper. Safe to construct per request; cheap to open."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_db_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        # check_same_thread=False so a single uvicorn worker can share it across
        # the threadpool; every method opens its own short-lived cursor.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        finally:
            cur.close()

    # ---- decisions ---------------------------------------------------------
    def record_decisions(self, layer: str, items: Iterable[dict]) -> int:
        """Upsert verdicts for a layer. Each item: {uuid, verdict, group_key?,
        suggested?}. Returns the number of rows written."""
        ts = _now()
        rows = []
        for it in items:
            verdict = it["verdict"]
            if verdict not in (KEEP, DISCARD):
                raise ValueError(f"bad verdict: {verdict!r}")
            rows.append((it["uuid"], layer, it.get("group_key"),
                         1 if it.get("suggested") else 0, verdict, ts))
        with self._tx() as cur:
            cur.executemany(
                "INSERT INTO decision (uuid, layer, group_key, suggested, verdict, decided_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(uuid, layer) DO UPDATE SET "
                "verdict=excluded.verdict, group_key=excluded.group_key, "
                "suggested=excluded.suggested, decided_at=excluded.decided_at",
                rows,
            )
        return len(rows)

    def decisions(self, layer: Optional[str] = None) -> list[Decision]:
        q = "SELECT * FROM decision"
        args: tuple = ()
        if layer:
            q += " WHERE layer = ?"
            args = (layer,)
        cur = self._conn.execute(q, args)
        return [Decision(r["uuid"], r["layer"], r["verdict"], r["group_key"],
                         bool(r["suggested"]), r["decided_at"]) for r in cur]

    def decided_uuids(self, layer: str) -> set[str]:
        cur = self._conn.execute("SELECT uuid FROM decision WHERE layer = ?", (layer,))
        return {r["uuid"] for r in cur}

    def clear_decisions(self, layer: str, uuids: Iterable[str]) -> int:
        """Drop decision rows for the given uuids in a layer, once they've been
        acted on. Prevents a finalized round's verdicts from lingering and being
        silently re-applied (or re-deleted) in a later, unrelated round."""
        rows = [(layer, u) for u in uuids]
        if not rows:
            return 0
        with self._tx() as cur:
            cur.executemany("DELETE FROM decision WHERE layer = ? AND uuid = ?", rows)
        return len(rows)

    # ---- reviewed (permanent exclusion, app equivalent of reviewed:keep) ----
    def mark_reviewed(self, uuids: Iterable[str], layer: Optional[str] = None) -> int:
        ts = _now()
        rows = [(u, layer, ts) for u in uuids]
        with self._tx() as cur:
            cur.executemany(
                "INSERT INTO reviewed (uuid, layer, locked_at) VALUES (?,?,?) "
                "ON CONFLICT(uuid) DO NOTHING",
                rows,
            )
        return len(rows)

    def reviewed_uuids(self) -> set[str]:
        cur = self._conn.execute("SELECT uuid FROM reviewed")
        return {r["uuid"] for r in cur}

    # ---- summary -----------------------------------------------------------
    def counts(self) -> dict:
        d = self._conn.execute(
            "SELECT layer, verdict, COUNT(*) n FROM decision GROUP BY layer, verdict"
        ).fetchall()
        by_layer: dict[str, dict[str, int]] = {}
        for r in d:
            by_layer.setdefault(r["layer"], {})[r["verdict"]] = r["n"]
        reviewed = self._conn.execute("SELECT COUNT(*) n FROM reviewed").fetchone()["n"]
        return {"decisions": by_layer, "reviewed": reviewed}
