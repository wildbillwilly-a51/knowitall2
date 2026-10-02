"""The KnowItAll2 server: one memory shared by agents on several computers.

It runs the same code as every computer, started as ``knowitall2
serve-http``, usually in the container from ``deploy/``. It owns the only
copy of the shared memory; computers keep their own copy and exchange
changes with it (``sync``). It never calls a model: learning stays on the
computers, which send what they learned.

- ``accounts``: agents' connections, their keys, and one-time join codes.
- ``admin``: the one admin account, its sessions, and the ways back in.
- ``exchange``: changes in and out, usage, the maintenance lease, and full
  copies for new computers.
- ``backups``: the daily backup.
- ``web``: the web service agents use; ``admin_routes`` and ``static/``
  add the admin page.

See ``docs/multi-machine-design.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..store import Store
from ..sync import set_tracking

API_VERSION = 1

_SERVER_SCHEMA = """
CREATE TABLE IF NOT EXISTS server_connections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    agent TEXT,
    computer TEXT,
    version TEXT,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    removed_at TEXT
);
CREATE TABLE IF NOT EXISTS server_join_codes (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    cancelled_at TEXT,
    connection_id TEXT
);
CREATE TABLE IF NOT EXISTS server_applied (
    op_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    result TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS server_applied_by_time ON server_applied (at);
CREATE TABLE IF NOT EXISTS server_leases (
    name TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL,
    until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_admin (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    recovery_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_sessions (
    token_hash TEXT PRIMARY KEY,
    form TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_resets (
    value_hash TEXT PRIMARY KEY,
    used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_conflicts (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    tbl TEXT NOT NULL,
    key TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    incoming TEXT NOT NULL,
    kept TEXT NOT NULL
);
"""


def open_store(path: Path) -> Store:
    """Open the server's database: the memory store, its own tables, and change numbering on."""

    store = Store.open(path)
    try:
        store.connection.executescript(_SERVER_SCHEMA)
        if store.get_meta("changes.track") is None:
            set_tracking(store, True)
    except BaseException:
        store.close()
        raise
    return store


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def later(now: str, *, seconds: float) -> str:
    return iso(parse_iso(now) + timedelta(seconds=seconds))
