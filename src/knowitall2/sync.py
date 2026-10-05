"""Sharing memories through a KnowItAll2 server, as whole rows of the shared tables.

Both sides speak in changes: one row of one shared table
(``store.SYNCED_TABLES``) written or deleted. Triggers in the database note
every change (``store.TRACK_KEY``), so the code that saves memories does not
need to know whether a server exists.

- A server numbers every change it accepts. A computer asks for the changes
  since the last number it saw, and gets each changed row once, as it is now.
- A computer sends its own changes as operations, each with an id made on
  the computer, so sending one twice changes nothing.

This module holds what both sides share; the server's side is in ``server``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Iterable, Sequence

from . import journal
from .store import APPLYING_KEY, SYNCED_TABLES, TRACK_KEY, Store, SyncedTable

MAX_TEXT = 20_000
CURSOR_KEY = "sync.cursor"
SOURCE_KEY = "sync.source"
EPOCH_KEY = "sync.server_epoch"


class SyncError(ValueError):
    """A change that cannot be applied, with a reason the sender can show."""


def set_tracking(store: Store, on: bool = True) -> None:
    """Start or stop noting changes to the shared tables in this database."""

    with store.transaction():
        if on:
            store.set_meta(TRACK_KEY, "1")
        else:
            store.connection.execute("DELETE FROM meta WHERE key = ?", (TRACK_KEY,))


def table_for(name: object) -> SyncedTable:
    table = SYNCED_TABLES.get(name) if isinstance(name, str) else None
    if table is None:
        raise SyncError(f"unknown table: {name!r}")
    return table


def read_row(connection: sqlite3.Connection, table: SyncedTable, key: str) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT {', '.join(table.columns)} FROM {table.name} WHERE {table.key} = ?", (key,),
    ).fetchone()
    return dict(zip(table.columns, row)) if row is not None else None


def write_row(connection: sqlite3.Connection, table: SyncedTable, row: dict[str, Any]) -> None:
    """Insert a row, or update the columns it carries; columns it leaves out keep their value."""

    columns = list(row)
    updates = [column for column in columns if column != table.key]
    conflict = (
        "DO UPDATE SET " + ", ".join(f"{column} = excluded.{column}" for column in updates) if updates else "DO NOTHING"
    )
    connection.execute(
        f"INSERT INTO {table.name} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
        f"ON CONFLICT ({table.key}) {conflict}",
        tuple(row[column] for column in columns),
    )


def delete_row(connection: sqlite3.Connection, table: SyncedTable, key: str) -> None:
    connection.execute(f"DELETE FROM {table.name} WHERE {table.key} = ?", (key,))


def check_key(key: object) -> str:
    if not isinstance(key, str) or not key or len(key) > 200:
        raise SyncError("each change needs a key of at most 200 characters")
    return key


def check_row(table: SyncedTable, key: str, row: object) -> dict[str, Any]:
    """A cleaned copy of a row sent for ``table``, or :class:`SyncError` saying what is wrong."""

    if not isinstance(row, dict):
        raise SyncError("a written row must be an object")
    unknown = sorted(set(row) - set(table.columns))
    if unknown:
        raise SyncError(f"{table.name} has no column {', '.join(unknown)}")
    if row.get(table.key, key) != key:
        raise SyncError(f"the row's {table.key} does not match the change's key")
    cleaned = {table.key: key}
    for column, value in row.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
            raise SyncError(f"{table.name}.{column} must be text, a number, or empty")
        if isinstance(value, str) and len(value) > MAX_TEXT:
            raise SyncError(f"{table.name}.{column} is longer than {MAX_TEXT} characters")
        if column in table.json_columns and value is not None:
            try:
                json.loads(value)
            except (TypeError, ValueError):
                raise SyncError(f"{table.name}.{column} must hold JSON text") from None
        cleaned[column] = value
    return cleaned


def stamp(table: SyncedTable, row: dict[str, Any]) -> str | None:
    """When a version of a row was made, by the table's newest-wins columns."""

    for column in table.stamp:
        if row.get(column) is not None:
            return str(row[column])
    return None


def latest_change(store: Store) -> int:
    return int(store.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0])


def changes_since(store: Store, since: int, *, limit: int = 500) -> dict[str, Any]:
    """The rows changed after change ``since``, each once and as it is now, oldest change first.

    ``next`` is the number to ask from next time; ``more`` says whether to ask
    again now. Read in one snapshot, so nothing committed meanwhile is missed.
    """

    connection = store.connection
    connection.execute("BEGIN")
    try:
        found = connection.execute(
            "SELECT c.seq, c.tbl, c.key, c.op FROM changes c WHERE c.seq > ? "
            "AND c.seq = (SELECT MAX(d.seq) FROM changes d WHERE d.tbl = c.tbl AND d.key = c.key) "
            "ORDER BY c.seq LIMIT ?",
            (since, limit + 1),
        ).fetchall()
        more = len(found) > limit
        items: list[dict[str, Any]] = []
        for seq, name, key, op in found[:limit]:
            table = SYNCED_TABLES.get(name)
            if table is None:
                continue
            row = read_row(connection, table, key) if op == "upsert" else None
            item: dict[str, Any] = {"seq": seq, "table": name, "key": key, "op": "upsert" if row else "delete"}
            if row:
                item["row"] = row
            items.append(item)
        latest = latest_change(store)
    finally:
        connection.execute("COMMIT")
    if more:
        next_seq = int(found[limit - 1][0])
    else:
        next_seq = max(since, latest)
    return {"changes": items, "next": next_seq, "latest": latest, "more": more}


# A memory's use counts change with every recall; a change to them alone is not news from the server.
_USE_COUNTS = frozenset({"recall_count", "last_used_at"})


def apply_pulled(store: Store, changes: Sequence[dict[str, Any]], *, cursor: int | None = None) -> int:
    """Apply changes that came from the server to this computer's copy, without noting them as its own.

    Returns how many rows here changed (use counts aside). A row with a
    change made here and not sent yet is left alone: it goes to the server
    next, and the server settles which version stays. Rows arrive newest
    change last, so a memory may arrive before the project it belongs to;
    links are checked when the whole batch is in. Columns and tables this
    version does not know (from a newer server) are left out, and so is a
    delete from a table the server never deletes from. Each row's change
    number is kept (``sync_seen``), so a later change sent from here says
    what it was based on. ``cursor``, when given, is where the next fetch
    starts.
    """

    connection = store.connection
    changed = 0
    with store.transaction():
        connection.execute("PRAGMA defer_foreign_keys = ON")
        # Read inside the transaction, so a change saved here a moment ago is never overwritten.
        mine = pending_keys(store)
        store.set_meta(APPLYING_KEY, "1")
        for item in changes:
            name = item.get("table")
            table = SYNCED_TABLES.get(name) if isinstance(name, str) else None
            if table is None:
                continue  # shared by a newer server; this version has nowhere to keep it
            key = check_key(item.get("key"))
            if (table.name, key) in mine:
                continue
            current = read_row(connection, table, key)
            if item.get("op") == "delete":
                if not table.deletable:
                    journal.problem("sync", f"the server sent a delete of {table.name} {key}, which it never "
                                            "makes; the row was kept")
                    continue
                if current is not None:
                    delete_row(connection, table, key)
                    changed += 1
            else:
                row = item.get("row")
                if isinstance(row, dict):
                    row = {name: value for name, value in row.items() if name in table.columns}
                row = check_row(table, key, row)
                write_row(connection, table, row)
                if current is None or any(current.get(column) != value for column, value in row.items()
                                          if column not in _USE_COUNTS):
                    changed += 1
            seq = item.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                connection.execute(
                    "INSERT INTO sync_seen (tbl, key, seq) VALUES (?, ?, ?) "
                    "ON CONFLICT (tbl, key) DO UPDATE SET seq = MAX(sync_seen.seq, excluded.seq)",
                    (table.name, key, seq),
                )
        if cursor is not None:
            store.set_meta(CURSOR_KEY, str(int(cursor)))
        connection.execute("DELETE FROM meta WHERE key = ?", (APPLYING_KEY,))
    return changed


def parent_links(connection: sqlite3.Connection, table: SyncedTable) -> list[tuple[str, SyncedTable, str]]:
    """The links from a shared table's rows to the shared rows they belong to: (column, parent table, its column)."""

    links = []
    for row in connection.execute(f"PRAGMA foreign_key_list({table.name})").fetchall():
        parent = SYNCED_TABLES.get(row[2])
        if parent is not None:
            links.append((row[3], parent, row[4] or parent.key))
    return links


def without_parents(store: Store, changes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fetched changes that must wait: their row belongs to a row neither here nor among ``changes``.

    Such as a memory whose project comes on a later page: saving it now
    would break the link, and the whole page with it.
    """

    connection = store.connection
    links: dict[str, list[tuple[str, SyncedTable, str]]] = {}
    held: list[dict[str, Any]] = []
    while True:
        waiting = {id(item) for item in held}
        arriving = {(item.get("table"), item.get("key")) for item in changes
                    if item.get("op") != "delete" and id(item) not in waiting}
        found = []
        for item in changes:
            table = SYNCED_TABLES.get(item.get("table")) if isinstance(item.get("table"), str) else None
            row = item.get("row")
            if table is None or item.get("op") == "delete" or not isinstance(row, dict):
                continue
            if table.name not in links:
                links[table.name] = parent_links(connection, table)
            for column, parent, target in links[table.name]:
                value = row.get(column)
                if value is None or (target == parent.key and (parent.name, value) in arriving):
                    continue
                if connection.execute(f"SELECT 1 FROM {parent.name} WHERE {target} = ?", (value,)).fetchone() is None:
                    found.append(item)
                    break
        if len(found) == len(held):  # a parent that waits holds back its own rows too, until nothing changes
            return found
        held = found


def cursor(store: Store) -> int:
    """Where this computer's next fetch of changes starts."""

    try:
        return int(store.get_meta(CURSOR_KEY) or 0)
    except ValueError:
        return 0


def has_pending(store: Store) -> bool:
    """Whether this computer has changes the server has not accepted yet."""

    return store.connection.execute("SELECT 1 FROM changes LIMIT 1").fetchone() is not None


def pending_keys(store: Store) -> set[tuple[str, str]]:
    return {(row[0], row[1]) for row in store.connection.execute("SELECT DISTINCT tbl, key FROM changes").fetchall()}


def note_all(store: Store, name: str) -> None:
    """Note every row of one shared table as changed here, in the order they were made, so the next sync sends it."""

    table = table_for(name)
    store.connection.execute(
        f"INSERT INTO changes (tbl, key, op) SELECT ?, {table.key}, 'upsert' FROM {table.name} ORDER BY rowid",
        (table.name,),
    )


def server_went_back(store: Store, hello: dict[str, Any]) -> bool:
    """Whether the server's memory went back since this computer last synced (a backup put back, a new volume).

    The server's change numbers only grow, so a next fetch that starts past
    the server's newest change says so, with any server; and a server that
    names its epoch says so when the epoch differs from the one kept here.
    """

    latest = hello.get("latest")
    if isinstance(latest, int) and not isinstance(latest, bool) and cursor(store) > latest:
        return True
    epoch, kept = hello.get("epoch"), store.get_meta(EPOCH_KEY)
    return isinstance(epoch, str) and bool(epoch) and kept is not None and kept != epoch


def keep_epoch(store: Store, hello: dict[str, Any]) -> None:
    epoch = hello.get("epoch")
    if isinstance(epoch, str) and epoch and store.get_meta(EPOCH_KEY) != epoch:
        with store.transaction():
            store.set_meta(EPOCH_KEY, epoch)


def send_everything_again(store: Store) -> None:
    """After the server's memory went back: fetch from the start, and note every shared row to be sent again.

    The server keeps one copy of a row it already has exactly, settles a
    row it has another version of as it settles any change, and gets back
    the rows it lost. What this computer saw of the server's numbers no
    longer holds, so nothing sent is based on them.
    """

    with store.transaction():
        store.connection.execute("DELETE FROM sync_seen")
        store.set_meta(CURSOR_KEY, "0")
        for name in SYNCED_TABLES:  # parents first
            note_all(store, name)


def note_parents(store: Store, table: SyncedTable, row: dict[str, Any]) -> None:
    """Note the rows a row belongs to (such as a memory's project) as changed here, when they are here and
    not noted already, so a sync sends them and the row waiting for them on the server can follow."""

    connection = store.connection
    with store.transaction():
        for column, parent, target in parent_links(connection, table):
            value = row.get(column)
            if value is None or target != parent.key:
                continue
            connection.execute(
                f"INSERT INTO changes (tbl, key, op) SELECT ?, {parent.key}, 'upsert' FROM {parent.name} "
                f"WHERE {parent.key} = ? AND NOT EXISTS (SELECT 1 FROM changes WHERE tbl = ? AND key = ?)",
                (parent.name, value, parent.name, value),
            )


def source_id(store: Store) -> str:
    """This database's own id, which makes the ids of the operations it sends."""

    found = store.get_meta(SOURCE_KEY)
    if found:
        return found
    with store.transaction():
        found = store.get_meta(SOURCE_KEY)
        if not found:
            found = "s-" + uuid.uuid4().hex[:12]
            store.set_meta(SOURCE_KEY, found)
    return found


def pending_operations(
    store: Store, *, limit: int = 500, skip: Iterable[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    """This computer's changes not yet accepted by the server, each row once, as it is now.

    Rows go in the order they first changed, so a project goes before the
    memories filed under it. ``through`` is the last local change each
    covers. ``skip`` leaves out rows (table, key), such as those waiting for
    a project the server does not have yet, so the rows after them can go.

    An operation's id is this database's id, the change number, and a short
    digest of the row: sending the same operation again changes nothing,
    while a change number used again with other content (a database put
    back from an older copy) is a new operation.
    """

    connection = store.connection
    prefix = source_id(store)
    left_out = json.dumps([f"{name}/{key}" for name, key in skip])
    operations = []
    for name, key, through, base in connection.execute(
        "SELECT c.tbl, c.key, MAX(c.seq), s.seq FROM changes c "
        "LEFT JOIN sync_seen s ON s.tbl = c.tbl AND s.key = c.key "
        "WHERE c.tbl || '/' || c.key NOT IN (SELECT value FROM json_each(?)) "
        "GROUP BY c.tbl, c.key ORDER BY MIN(c.seq) LIMIT ?", (left_out, limit),
    ).fetchall():
        table = SYNCED_TABLES.get(name)
        if table is None:
            continue
        row = read_row(connection, table, key)
        digest = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
        operation: dict[str, Any] = {
            "op_id": f"{prefix}-{through}-{digest}", "table": name, "key": key, "op": "upsert" if row else "delete",
            "through": int(through),
        }
        if base is not None:
            operation["base"] = int(base)
        if row:
            operation["row"] = row
        operations.append(operation)
    return operations


def acknowledge(
    store: Store, operations: Sequence[dict[str, Any]], *, seen: Sequence[tuple[str, str, int]] = (),
) -> None:
    """Forget the local changes the server accepted (or settled) in these operations.

    ``seen`` holds (table, key, change number) for rows the server applied
    and numbered, kept like a fetched row's (``sync_seen``): the next change
    sent from here, by any agent's key, is then based on that version.
    """

    with store.transaction():
        for operation in operations:
            store.connection.execute(
                "DELETE FROM changes WHERE tbl = ? AND key = ? AND seq <= ?",
                (operation["table"], operation["key"], operation["through"]),
            )
        for name, key, seq in seen:
            store.connection.execute(
                "INSERT INTO sync_seen (tbl, key, seq) VALUES (?, ?, ?) "
                "ON CONFLICT (tbl, key) DO UPDATE SET seq = MAX(sync_seen.seq, excluded.seq)",
                (name, key, seq),
            )
