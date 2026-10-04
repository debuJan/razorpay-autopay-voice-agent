"""SQLite storage. Four domain tables plus a call_events table for call traces."""
import json
import sqlite3
from datetime import datetime, timezone

from .config import default_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    merchant_name TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    failure_date TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    last4 TEXT NOT NULL,
    language TEXT NOT NULL,
    verification_suffix TEXT NOT NULL,
    attempt_no INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    provider_call_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration INTEGER,
    outcome TEXT,
    summary TEXT,
    verified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT,
    tool_name TEXT NOT NULL,
    arguments TEXT NOT NULL,
    result TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT,
    customer_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    type TEXT NOT NULL,
    data TEXT,
    timestamp TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | None = None) -> str:
    path = db_path or default_db_path()
    conn = get_connection(path)
    try:
        conn.executescript(SCHEMA)
        # Lightweight migration for databases created before the `verified`
        # column existed.
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already present
        conn.commit()
    finally:
        conn.close()
    return path


def record_event(conn: sqlite3.Connection, call_id: str, event_type: str, data: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO call_events (call_id, type, data, timestamp) VALUES (?, ?, ?, ?)",
        (call_id, event_type, json.dumps(data or {}), utcnow()),
    )
    conn.commit()
