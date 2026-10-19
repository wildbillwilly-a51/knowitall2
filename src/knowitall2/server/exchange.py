"""What the server exchanges with computers: changes in and out, usage, the lease, and full copies.

The server has the final word on what it accepts:

- It checks every row like a computer would (``sync.check_row``) and screens
  new or changed memory text for secrets again.
- A new memory that another computer already saved is kept as replaced by
  the first one, so one memory stays one memory.
- A row exactly as the server already has it (such as a computer sending
  its whole memory again on reconnecting) is accepted and changes nothing.
- When another agent changed a row since the sender last saw it, the newer
  version wins by the row's own time, and the disagreement is kept in
  ``server_conflicts``. A version's time counts for no later than a few
  minutes after the server got it, so a computer whose clock runs ahead
  cannot win every later disagreement.
- An operation sent twice gets its first answer again. An applied one says
  the row's change number on the server (``seq``), so the sender's next
  change of it is based on that version, whichever agent's key sends it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from ..secrets import contains_secret
from ..quotes import same_statement_text
from ..store import BY_KEY, SYNCED_TABLES, Store, SyncedTable
from ..sync import SyncError, check_key, check_row, delete_row, read_row, stamp, table_for, write_row
from . import later

MAX_OPERATIONS = 500
LEASE_SECONDS = (60, 4 * 60 * 60)
DUPLICATE_REASON = "the same memory was already saved from another computer"
# How far a computer's clock may run ahead of the server's before its rows' times stop counting as later.
CLOCK_SKEW_SECONDS = 5 * 60
EPOCH_KEY = "server.epoch"
RUN_KEY = "server.run"
# Beside the database, outside it: a database put back from a backup, or a new one, does not match it.
RUN_MARKER = "server-run"


def begin_run(store: Store, home: Path) -> str:
    """Note this start of the server, and return its epoch: the id of this run of its memory.

    Computers keep the epoch, and a change tells them the server's memory
    went back (a backup put back, a new volume), so they send theirs again.
    The database and a file beside it hold the same run id; when they
    differ, or the database has no epoch yet, this database is not the one
    that last ran here, and a new epoch begins. A backup's run id is never
    a running server's (``backups.copy_database``), so a backup put back
    always begins one. (A whole computer put back, file and database
    together, is noticed by the computers instead: their next fetch would
    start past the server's newest change.)
    """

    marker = home / RUN_MARKER
    try:
        on_disk = marker.read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeDecodeError):
        on_disk = None
    run = "r-" + uuid.uuid4().hex[:16]
    with store.transaction():
        epoch = store.get_meta(EPOCH_KEY)
        recorded = store.get_meta(RUN_KEY)
        # A database without an epoch (new, or from before epochs) begins one; a computer that never kept an
        # epoch just keeps this one, so only a computer that saw another epoch sends its memory again.
        if epoch is None or (recorded is not None and recorded != on_disk):
            epoch = "e-" + uuid.uuid4().hex[:16]
            store.set_meta(EPOCH_KEY, epoch)
        store.set_meta(RUN_KEY, run)
    try:
        temporary = marker.with_name(marker.name + ".tmp")
        temporary.write_text(run + "\n", encoding="ascii")
        temporary.replace(marker)
    except OSError:
        pass  # the next start then begins a new epoch, which only makes computers send their memory again
    return epoch


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
        before = connection.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0]
        for operation in operations:
            answers.append(_apply_one(store, connection_id, operation, now))
        # When the server got each version, by its own clock, for weighing that version's time later.
        connection.execute("UPDATE changes SET at = ? WHERE seq > ?", (now, before))
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
        if isinstance(exc, InvalidBase):
            refused.update(code="invalid_base", retry=True)
        current = _current(connection, operation)
        if current is not None:
            refused["row"], refused["seq"] = current
        return refused
    if answer["result"] == "applied":
        # Older computers ignore it; a newer one bases its next change of the row on it.
        seq = _latest_seq(connection, operation["table"], operation["key"])
        if seq:
            answer["seq"] = seq
        current = _current(connection, operation)
        requested = operation.get("row")
        if current is not None and isinstance(requested, dict) and any(
            column in current[0] and current[0][column] != value for column, value in requested.items()
        ):
            # Normalization may keep stronger authority or avoid a duplicate
            # cycle without creating a new change. Return the actual row even
            # when the sender's pull cursor already passed its change number.
            answer["row"] = current[0]
    connection.execute(
        "INSERT INTO server_applied (op_id, connection_id, result, at) VALUES (?, ?, ?, ?)",
        (op_id, connection_id, json.dumps(answer), now),
    )
    connection.execute("RELEASE operation")
    return {"op_id": op_id, **answer}


def _latest_seq(connection: sqlite3.Connection, name: str, key: str) -> int:
    return int(connection.execute("SELECT COALESCE(MAX(seq), 0) FROM changes WHERE tbl = ? AND key = ?",
                                  (name, key)).fetchone()[0])


def _current(connection: sqlite3.Connection, operation: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    """The server's own version of the row an operation names, and its newest change number, if it has one."""

    table = SYNCED_TABLES.get(operation.get("table")) if isinstance(operation.get("table"), str) else None
    key = operation.get("key")
    if table is None or not isinstance(key, str):
        return None
    row = read_row(connection, table, key)
    if row is None:
        return None
    return row, _latest_seq(connection, table.name, key)


def _plain_reason(error: Exception) -> str:
    if isinstance(error, sqlite3.IntegrityError):
        text = str(error)
        if "FOREIGN KEY" in text:
            return "it refers to something the server does not have yet, such as its project"
        return f"the server's store refused it: {text}"
    return str(error)


class InvalidBase(SyncError):
    """A client can safely forget this row's claimed revision and resend for conflict resolution."""


def _apply_change(store: Store, connection_id: str, operation: dict[str, Any], now: str) -> dict[str, Any]:
    connection = store.connection
    table = table_for(operation.get("table"))
    key = check_key(operation.get("key"))
    base = operation.get("base")
    if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
        raise SyncError("base must be a change number")
    existing = read_row(connection, table, key)
    if base is not None and (base < 0 or base > _latest_seq(connection, table.name, key)):
        raise InvalidBase("base must be a nonnegative revision no later than this row's server revision")
    if operation.get("op") == "delete":
        if not table.deletable:
            raise SyncError(f"{table.name} are never deleted")
        if existing is not None:
            delete_row(connection, table, key)
        return {"result": "applied"}
    if operation.get("op") != "upsert":
        raise SyncError("op must be upsert or delete")
    row = check_row(table, key, operation.get("row"), existing=existing)
    # Only new or changed text is screened: retiring or replacing a memory kept
    # since before the screening knew its shape must never be refused.
    if table.name == "records":
        replacement = row.get("superseded_by")
        if existing is not None and row.get("status") == "superseded" and replacement:
            target = read_row(connection, table, replacement)
            # A same-text promotion may arrive as a new id followed by a
            # replacement of the old id. If the new id was folded into the
            # old one, that replacement must not turn the pair into a cycle.
            if (target is not None and target.get("retired_reason") == DUPLICATE_REASON
                    and target.get("superseded_by") == key and _same_memory(existing, target)):
                row.update(status=existing["status"], superseded_by=existing["superseded_by"],
                           retired_reason=existing["retired_reason"])
        changed_text = [row[column] for column in ("text", "subjects", "tags")
                        if isinstance(row.get(column), str)
                        and (existing is None or row[column] != existing.get(column))]
        if contains_secret("\n".join(changed_text)):
            raise SyncError("the memory looks like it holds a secret; save where the secret is kept instead")
    if existing is None:
        if table.name == "records" and row.get("status") == "active" and row.get("content_hash"):
            candidates = connection.execute(
                "SELECT id FROM records WHERE content_hash = ? AND status = 'active' AND id != ? ORDER BY seq",
                (row["content_hash"], key),
            ).fetchall()
            first = next((found for candidate in candidates
                          if (found := read_row(connection, table, candidate[0])) and _same_memory(found, row)), None)
            if first is not None:
                _merge_duplicate(connection, table, first, row, now)
                row.update(status="superseded", superseded_by=first["id"], retired_reason=DUPLICATE_REASON)
                write_row(connection, table, row)
                current = _current(connection, operation)
                assert current is not None
                return {"result": "duplicate", "kept": first["id"], "row": current[0], "seq": current[1]}
        write_row(connection, table, row)
        return {"result": "applied"}
    if table.name == "records":
        # An offline peer can promote an id already folded into another id.
        # Follow only server-created duplicate links; ordinary replacements
        # can describe a different statement and must not inherit authority.
        if existing.get("retired_reason") == DUPLICATE_REASON and existing.get("superseded_by"):
            target = existing
            seen = {key}
            while target.get("retired_reason") == DUPLICATE_REASON and target.get("superseded_by") not in seen:
                seen.add(target["superseded_by"])
                following = read_row(connection, table, target["superseded_by"])
                if following is None:
                    break
                target = following
            if row.get("status", existing["status"]) == "active" and _same_memory(target, {**existing, **row}):
                if target["status"] == "active":
                    _merge_duplicate(connection, table, target, {**existing, **row}, now)
                current = _current(connection, operation)
                assert current is not None
                return {"result": "duplicate", "kept": target["id"], "row": current[0], "seq": current[1]}
        # Use counts are the server's own, from the usage computers send.
        row.pop("recall_count", None)
        row.pop("last_used_at", None)
        if existing["status"] == row.get("status", existing["status"]) == "active" and _same_memory(
            existing, {**existing, **row}
        ):
            explicit_rule = (row.get("kind") == "rule" and row.get("verification") == "user_stated"
                             and row.get("source_kind") == "user")
            if (explicit_rule
                    and (existing["kind"] != "rule" or existing["verification"] != "user_stated")):
                # Promotion expresses a user instruction, not a newer snapshot
                # of a note. Converge it before choosing by timestamp, just as
                # for independently created duplicate ids.
                _merge_duplicate(connection, table, existing, {**existing, **row}, now)
                current = _current(connection, operation)
                assert current is not None
                return {"result": "applied", "row": current[0], "seq": current[1]}
            strength = {"unverified": 0, "observed": 1, "user_stated": 2}
            if strength.get(row.get("verification", existing["verification"]), -1) < strength[existing["verification"]]:
                for column in ("verification", "source_kind", "source_agent", "source_session", "source_computer"):
                    row[column] = existing[column]
            if existing["kind"] == "rule" and existing["verification"] == "user_stated":
                row["kind"] = "rule"
                if not explicit_rule:
                    for column in ("source_kind", "source_agent", "source_session", "source_computer", "tags", "subjects"):
                        row[column] = existing[column]
    if all(existing.get(column) == value for column, value in row.items()):
        return {"result": "applied"}  # the server has this version already: nothing to settle or to change
    if _changed_by_others(connection, table, key, base, connection_id):
        merged = {**existing, **row}
        # Each version's time counts for no later than a few minutes after the server got it.
        incoming = _no_later_than(stamp(table, merged), now)
        current = _no_later_than(stamp(table, existing), _arrived(connection, table, key) or now)
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


def _same_memory(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return (first.get("scope") == second.get("scope") and first.get("project_id") == second.get("project_id")
            and isinstance(first.get("text"), str) and isinstance(second.get("text"), str)
            and same_statement_text(first["text"], second["text"]))


def _merge_duplicate(connection: sqlite3.Connection, table: SyncedTable, kept: dict[str, Any],
                     incoming: dict[str, Any], now: str) -> None:
    """Converge authority and user promotion without dropping the canonical id."""
    strength = {"unverified": 0, "observed": 1, "user_stated": 2}
    merged = dict(kept)
    user = incoming.get("verification") == "user_stated" and incoming.get("source_kind") == "user"
    if user and incoming.get("kind") == "rule":
        merged.update(kind="rule", text=incoming["text"])
    if (user and incoming.get("kind") == "rule") or (
        strength.get(incoming.get("verification"), -1) > strength.get(kept.get("verification"), -1)
    ):
        for column in ("verification", "source_kind", "source_agent", "source_session", "source_computer"):
            if column in incoming:
                merged[column] = incoming[column]
    if kept["kind"] != "rule" or user:
        for column in ("tags", "subjects"):
            values = list(dict.fromkeys([*json.loads(kept[column]), *json.loads(incoming.get(column, "[]"))]))
            merged[column] = json.dumps(values, ensure_ascii=False)
    if any(merged[column] != kept[column] for column in kept):
        merged.update(updated_at=now, confirmed_at=now)
        # Two individually valid label arrays can exceed the wire limit when
        # combined. Never publish a row that clients would have to reject.
        check_row(table, kept["id"], {column: value for column, value in merged.items() if value != kept[column]})
        write_row(connection, table, merged)


def _arrived(connection: sqlite3.Connection, table: SyncedTable, key: str) -> str | None:
    """When the server got the version of a row it holds (its newest change), if it noted one."""

    row = connection.execute("SELECT at FROM changes WHERE tbl = ? AND key = ? ORDER BY seq DESC LIMIT 1",
                             (table.name, key)).fetchone()
    return row[0] if row else None


def _no_later_than(value: str | None, arrived: str) -> datetime | None:
    """A version's time, or the moment it arrived plus the allowed clock difference, whichever is earlier.

    Parse both versions, including legacy rows whose dates were not padded.
    """

    if value is None:
        return None
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    try:
        ceiling = datetime.strptime(arrived, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=CLOCK_SKEW_SECONDS)
    except (ValueError, OverflowError):
        return moment
    return min(moment, ceiling)


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
        # The uses are the server's bookkeeping: older ones live on as totals (``Store.roll_up_usage``).
        for name in ("changes", "events", "usage", "usage_totals", "project_paths"):
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
        "epoch": store.get_meta(EPOCH_KEY),
    }
