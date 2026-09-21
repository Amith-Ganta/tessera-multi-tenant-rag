"""Persistent SQLite checkpointer for resumable A2A agent workflows.

The previous guard loop kept its state in memory, so a pod restart, a provider
fallback, or a long-running workflow silently dropped the transcript, retry count,
and last draft. This module replaces that with a durable JSON-over-SQLite store
keyed by ``thread_id``.

The stored state document carries the fields the A2A supervisor needs to resume
exactly where it left off:

    transcript        list[dict]   one entry per drafter/judge/fallback step
    retries           int          number of refine attempts used so far
    last_draft        str          the most recent draft answer
    current_question  str          the question being answered
    tenant_slug       str          the tenant the workflow is scoped to
    created_at        float        epoch seconds when the thread was created

The store is a single SQLite file so it works with zero external services and is
safe to use across the FastAPI worker and the standalone Drafter/Judge agents.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .config import PROJECT_ROOT

_DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "checkpoints.sqlite3"


def _default_db_path() -> Path:
    raw = os.environ.get("TESSERA_CHECKPOINT_DB", "").strip()
    if raw:
        return Path(raw)
    return _DEFAULT_DB_PATH


class SQLiteCheckpointer:
    """Durable JSON document store keyed by thread_id."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # A single lock serialises writes so concurrent requests never interleave
        # a partial JSON document. Connections are opened per operation, which keeps
        # this safe across threads without sharing a connection.
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        thread_id   TEXT PRIMARY KEY,
                        state_json  TEXT NOT NULL,
                        created_at  REAL NOT NULL,
                        updated_at  REAL NOT NULL
                    )
                    """
                )
                conn.commit()

    def save_state(self, thread_id: str, state: dict) -> None:
        """Persist the full agent state for ``thread_id`` (upsert)."""
        self._init_db()
        now = time.time()
        payload = dict(state)
        payload.setdefault("created_at", now)
        serialized = json.dumps(payload, default=str)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO checkpoints (thread_id, state_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        updated_at = excluded.updated_at
                    """,
                    (thread_id, serialized, now, now),
                )
                conn.commit()

    def load_state(self, thread_id: str) -> dict | None:
        """Retrieve state for ``thread_id``, or ``None`` when it does not exist."""
        self._init_db()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT state_json FROM checkpoints WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            # A corrupt document must not crash the resume path; treat as missing.
            return None

    def delete_state(self, thread_id: str) -> bool:
        """Remove the state for ``thread_id`` when a workflow completes."""
        self._init_db()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)
                )
                conn.commit()
                return cur.rowcount > 0

    def delete_tenant_checkpoints(self, tenant: str) -> int:
        """Delete all checkpoints whose state carries tenant_slug == tenant.

        Uses SQLite's json_extract() so no full table scan into Python is needed.
        Returns the number of rows deleted.
        """
        self._init_db()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM checkpoints WHERE json_extract(state_json, '$.tenant_slug') = ?",
                    (tenant,),
                )
                conn.commit()
                return cur.rowcount
