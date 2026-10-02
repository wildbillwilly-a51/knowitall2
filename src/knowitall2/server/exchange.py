"""What the server exchanges with computers: changes in and out, usage, the lease, and full copies.

The server has the final word on what it accepts:

- It checks every row like a computer would (``sync.check_row``) and screens
  memories for secrets again.
- A new memory that another computer already saved is kept as replaced by
  the first one, so one memory stays one memory.
- When another agent changed a row since the sender last saw it, the newer
  version wins by the row's own time, and the disagreement is kept in
  ``server_conflicts``.
- An operation sent twice gets its first answer again.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from ..secrets import contains_secret
from ..store import BY_KEY, SYNCED_TABLES, Store, SyncedTable
from ..sync import SyncError, check_key, check_row, delete_row, read_row, stamp, table_for, write_row
from . import later

MAX_OPERATIONS = 500
LEASE_SECONDS = (60, 4 * 60 * 60)
DUPLICATE_REASON = "the same memory was already saved from another computer"


def apply_operations(
    store: Store, connection_id: str, operations: Sequence[object], *, now: str,
) -> list[dict[str, Any]]:
    """Apply one batch from one connection, in order; one answer per operation.

    A rejected operation changes nothing and does not stop the rest.
    """

    if len(operations) > MAX_OPERATIONS:
        raise SyncError(f"send at most {MAX_OPERATIONS} changes at once")
    connection = store.connection
    answers = []
    with store.transaction():
        store.set_meta(BY_KEY, connection_id)
        for operation in operations:
            answers.append(_apply_one(store, connection_id, operation, now))
        connection.execute("DELETE FROM meta WHERE key = ?", (BY_KEY,))
    return answers


def _apply_one(store: Store, connection_id: str, operation: object, now: str) -> dict[str, Any]:
    connection = store.connection
    op_id = operation.get("op_id") if isinstance(operation, dict) else None
    if not isinstance(op_id, str) or not op_id or len(op_id) > 200:
        return {"op_id": op_id if isinstance(op_id, str) else None, "result": "rejected",
                "reason": "each change needs an op_id of at most 200 characters"}
    earlier = connection.execute("SELECT result FROM server_applied WHERE op_id = ?", (op_id,)).fetchone()
    if earlier is not None:
        return {"op_id": op_id, **json.loads(earlier[0])}
    connection.execute("SAVEPOINT operation")
    try:
        answer = _apply_change(store, connection_id, operation, now)
    except (SyncError, sqlite3.IntegrityError) as exc:
        connection.execute("ROLLBACK TO operation")
        connection.execute("RELEASE operation")
        # Not remembered: the same operation may succeed later, such as once its project has arrived.
        refused: dict[str, Any] = {"op_id": op_id, "result": "rejected", "reason": _plain_reason(exc),
                                   "retry": isinstance(exc, sqlite3.IntegrityError) and "FOREIGN KEY" in str(exc)}
        current = _current(connection, operation)
        if current is not None:
            refused["row"], refused["seq"] = current
        return refused
    connection.execute(
        "INSERT INTO server_applied (op_id, connection_id, result, at) VALUES (?, ?, ?, ?)",
        (op_id, connection_id, json.dumps(answer), now),
    )
    connection.execute("RELEASE operation")
    return {"op_id": op_id, **answer}


def _current(connection: sqlite3.Connection, operation: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    """The server's own version of the row an operation names, and its newest change number, if it has one."""

    table = SYNCED_TABLES.get(operation.get("table")) if isinstance(operation.get("table"), str) else None
    key = operation.get("key")
    if table is None or not isinstance(key, str):
        return None
    row = read_row(connection, table, key)
    if row is None:
        return None
    seq = connection.execute("SELECT COALESCE(MAX(seq), 0) FROM changes WHERE tbl = ? AND key = ?",
                             (table.name, key)).fetchone()[0]
    return row, int(seq)


def _plain_reason(error: Exception) -> str:
    if isinstance(error, sqlite3.IntegrityError):
        text = str(error)
        if "FOREIGN KEY" in text:
            return "it refers to something the server does not have yet, such as its project"
        return f"the server's store refused it: {text}"
    return str(error)


def _apply_change(store: Store, connection_id: str, operation: dict[str, Any], now: str) -> dict[str, Any]:
    connection = store.connection
    table = table_for(operation.get("table"))
    key = check_key(operation.get("key"))
    base = operation.get("base")
    if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
        raise SyncError("base must be a change number")
    existing = read_row(connection, table, key)
    if operation.get("op") == "delete":
        if not table.deletable:
            raise SyncError(f"{table.name} are never deleted")
        if existing is not None:
            delete_row(connection, table, key)
        return {"result": "applied"}
    if operation.get("op") != "upsert":
        raise SyncError("op must be upsert or delete")
    row = check_row(table, key, operation.get("row"))
    if table.name == "records" and isinstance(row.get("text"), str) and contains_secret(row["text"]):
        raise SyncError("the memory looks like it holds a secret; save where the secret is kept instead")
    if existing is None:
        if table.name == "records" and row.get("status") == "active" and row.get("content_hash"):
            first = connection.execute(
                "SELECT id FROM records WHERE content_hash = ? AND status = 'active' AND id != ? LIMIT 1",
                (row["content_hash"], key),
            ).fetchone()
            if first is not None:
                row.update(status="superseded", superseded_by=first[0], retired_reason=DUPLICATE_REASON)
                write_row(connection, table, row)
                current = _current(connection, operation)
                assert current is not None
                return {"result": "duplicate", "kept": first[0], "row": current[0], "seq": current[1]}
        write_row(connection, table, row)
        return {"result": "applied"}
    if table.name == "records":
        # Use counts are the server's own, from the usage computers send.
        row.pop("recall_count", None)
        row.pop("last_used_at", None)
    if _changed_by_others(connection, table, key, base, connection_id):
        merged = {**existing, **row}
        incoming, current = stamp(table, merged), stamp(table, existing)
        newer = incoming is not None and (current is None or incoming > current)
        connection.execute(
            "INSERT INTO server_conflicts (at, tbl, key, connection_id, outcome, incoming, kept) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, table.name, key, connection_id, "incoming" if newer else "existing",
             json.dumps(merged, ensure_ascii=False), json.dumps(existing, ensure_ascii=False)),
        )
        if not newer:
            # The sender gets the server's version, which its copy should now hold.
            current = _current(connection, operation)
            assert current is not None
            return {"result": "kept_newer", "row": current[0], "seq": current[1]}
    write_row(connection, table, row)
    return {"result": "applied"}


def _changed_by_others(
    connection: sqlite3.Connection, table: SyncedTable, key: str, base: int | None, connection_id: str,
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM changes WHERE tbl = ? AND key = ? AND seq > ? "
        "AND (by_connection IS NULL OR by_connection != ?) LIMIT 1",
        (table.name, key, base or 0, connection_id),
    ).fetchone()
    return row is not None


def add_usage(store: Store, connection_id: str, rows: Sequence[object], *, now: str) -> int:
    """Record briefings and recalls computers made; returns how many were new."""

    if len(rows) > MAX_OPERATIONS:
        raise SyncError(f"send at most {MAX_OPERATIONS} uses at once")
    connection = store.connection
    added = 0
    with store.transaction():
        for row in rows:
            if not isinstance(row, dict):
                raise SyncError("each use must be an object")
            op_id, at, operation = row.get("op_id"), row.get("at"), row.get("operation")
            ids = row.get("result_ids") or []
            if not isinstance(op_id, str) or not op_id or len(op_id) > 200:
                raise SyncError("each use needs an op_id")
            if operation not in {"recall", "briefing"} or not isinstance(at, str):
                raise SyncError("each use needs an operation (recall or briefing) and a time")
            if not isinstance(ids, list) or len(ids) > 100 or not all(isinstance(item, str) for item in ids):
                raise SyncError("result_ids must be a list of memory ids")
            if connection.execute("SELECT 1 FROM server_applied WHERE op_id = ?", (op_id,)).fetchone():
                continue
            query = row.get("query") if isinstance(row.get("query"), str) else None
            agent = row.get("agent") if isinstance(row.get("agent"), str) else None
            project_id = row.get("project_id") if isinstance(row.get("project_id"), str) else None
            store.log_usage(at=at[:40], operation=operation, agent=(agent or "")[:40] or None,
                            project_id=project_id, query=(query or "")[:500] or None, record_ids=ids)
            connection.execute(
                "INSERT INTO server_applied (op_id, connection_id, result, at) VALUES (?, ?, ?, ?)",
                (op_id, connection_id, json.dumps({"result": "applied"}), now),
            )
            added += 1
    return added


def take_lease(store: Store, name: object, connection_id: str, *, seconds: object, now: str) -> dict[str, Any]:
    """Hold a named job (such as maintenance) for a while, unless another agent holds it."""

    if not isinstance(name, str) or not name or len(name) > 40:
        raise SyncError("a lease needs a short name")
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise SyncError("seconds must be a whole number")
    seconds = min(max(seconds, LEASE_SECONDS[0]), LEASE_SECONDS[1])
    connection = store.connection
    with store.transaction():
        row = connection.execute(
            "SELECT l.connection_id, l.until, c.name FROM server_leases l "
            "LEFT JOIN server_connections c ON c.id = l.connection_id WHERE l.name = ?", (name,),
        ).fetchone()
        if row is not None and row["until"] > now and row["connection_id"] != connection_id:
            return {"granted": False, "holder": row["name"], "until": row["until"]}
        until = later(now, seconds=seconds)
        connection.execute(
            "INSERT INTO server_leases (name, connection_id, until) VALUES (?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET connection_id = excluded.connection_id, until = excluded.until",
            (name, connection_id, until),
        )
    return {"granted": True, "until": until}


def release_lease(store: Store, name: object, connection_id: str) -> bool:
    cursor = store.connection.execute(
        "DELETE FROM server_leases WHERE name = ? AND connection_id = ?", (name, connection_id),
    )
    return cursor.rowcount == 1


def make_copy(source: Path, target: Path) -> int:
    """A full copy of the shared memory for a new computer; returns the change number it is current to.

    The server's own tables (connections, codes, the admin) and anything
    that belongs to one computer or to the server's bookkeeping are removed.
    """

    target.unlink(missing_ok=True)
    origin = sqlite3.connect(str(source), timeout=5.0)
    copy = sqlite3.connect(str(target), isolation_level=None)
    try:
        origin.backup(copy)
        origin.close()
        latest = int(copy.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0])
        tables = [row[0] for row in copy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'server\\_%' ESCAPE '\\'",
        ).fetchall()]
        copy.execute("BEGIN")
        for name in tables:
            copy.execute(f"DROP TABLE {name}")
        for name in ("changes", "events", "usage", "project_paths"):
            copy.execute(f"DELETE FROM {name}")
        copy.execute("DELETE FROM meta WHERE key LIKE 'changes.%' OR key LIKE 'sync.%' OR key LIKE 'server.%'")
        copy.execute("COMMIT")
        copy.execute("VACUUM")
    finally:
        copy.close()
        origin.close()
    return latest


def tidy(store: Store, *, now: str) -> None:
    """Daily housekeeping: keep only each row's newest change, and drop old bookkeeping.

    Any computer still gets every row changed after the number it asks from,
    so dropping a row's older changes loses nothing.
    """

    connection = store.connection
    with store.transaction():
        connection.execute(
            "DELETE FROM changes WHERE seq < (SELECT MAX(d.seq) FROM changes d "
            "WHERE d.tbl = changes.tbl AND d.key = changes.key)",
        )
        connection.execute("DELETE FROM server_applied WHERE at < ?", (later(now, seconds=-30 * 86400),))
        connection.execute("DELETE FROM server_conflicts WHERE at < ?", (later(now, seconds=-90 * 86400),))
        connection.execute("DELETE FROM server_join_codes WHERE used_at IS NULL AND expires_at < ?",
                           (later(now, seconds=-86400),))
        connection.execute("DELETE FROM server_leases WHERE until < ?", (now,))


def summary(store: Store) -> dict[str, Any]:
    """Counts for the server's health: memories, connections, and the newest change."""

    connection = store.connection
    return {
        "memories": int(connection.execute("SELECT COUNT(*) FROM records WHERE status = 'active'").fetchone()[0]),
        "connections": int(connection.execute(
            "SELECT COUNT(*) FROM server_connections WHERE removed_at IS NULL").fetchone()[0]),
        "latest": int(connection.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0]),
        "columns": {name: list(table.columns) for name, table in SYNCED_TABLES.items()},
    }
