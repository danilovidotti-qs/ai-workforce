"""SQLite persistence for run state.

Stores the full graph state for each run so it can be resumed after
failures, review loop limits, or restarts.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.getenv("AI_WORKFORCE_DB", "/app/data/runs.db")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id       TEXT PRIMARY KEY,
            project      TEXT NOT NULL,
            task         TEXT NOT NULL,
            workspace    TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'running',
            state_json   TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_run(run_id: str, project: str, task: str, workspace: str,
             status: str, state: dict):
    """Insert or update a run's full state."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    conn.execute("""
        INSERT INTO runs (run_id, project, task, workspace, status, state_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status = excluded.status,
            state_json = excluded.state_json,
            updated_at = excluded.updated_at
    """, (run_id, project, task, workspace, status, json.dumps(state), now, now))
    conn.commit()
    conn.close()


def get_run(run_id: str) -> Optional[dict]:
    """Get a run by ID. Returns None if not found."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "run_id": row["run_id"],
        "project": row["project"],
        "task": row["task"],
        "workspace": row["workspace"],
        "status": row["status"],
        "state": json.loads(row["state_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_runs(project: Optional[str] = None, limit: int = 20) -> list[dict]:
    """List recent runs, optionally filtered by project."""
    conn = _get_conn()
    if project:
        rows = conn.execute(
            "SELECT run_id, project, task, status, created_at, updated_at "
            "FROM runs WHERE project = ? ORDER BY created_at DESC LIMIT ?",
            (project, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT run_id, project, task, status, created_at, updated_at "
            "FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
