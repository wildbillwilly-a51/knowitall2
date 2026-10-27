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
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable, Sequence

from . import journal
from .question_schema import check_question
from .secrets import contains_secret
from .store import APPLYING_KEY, SYNCED_TABLES, TRACK_KEY, Store, SyncedTable, scrub

MAX_TEXT = 20_000
CURSOR_KEY = "sync.cursor"
SOURCE_KEY = "sync.source"
EPOCH_KEY = "sync.server_epoch"

# The wire contract follows the fields' readers, not SQLite's permissive types.
# It applies to both directions; omitted columns still support older peers.
_NULLABLE = {
    "projects": {"remote"},
    "systems": {"profiled"},
    "records": {"project_id", "source_agent", "source_session", "confirmed_at", "superseded_by",
                "retired_reason", "last_used_at", "source_computer"},
    "record_notes": {"system_id"},
    "questions": {"answered_at", "answer"},
    "question_stages": {"plain", "reason"},
    "agent_tasks": {"question_id", "system_id", "project_id", "facet", "result"},
}
_INTEGERS = {("records", "recall_count"), ("agent_tasks", "offered"), ("reviews", "failures")}


def _check_json(table: str, column: str, value: str) -> None:
    try:
        decoded = json.loads(value)
    except (ValueError, RecursionError):
        raise SyncError(f"{table}.{column} must hold JSON text") from None
    if (table, column) == ("question_stages", "labels"):
        valid = isinstance(decoded, dict) and all(isinstance(label, str) for label in decoded.values())
    elif (table, column) == ("questions", "options"):
        valid = isinstance(decoded, list) and all(
            isinstance(option, dict) and isinstance(option.get("key"), str)
            and isinstance(option.get("label"), str) for option in decoded
        )
    else:
        valid = isinstance(decoded, list) and all(isinstance(item, str) for item in decoded)
    if not valid:
        raise SyncError(f"{table}.{column} has the wrong JSON structure")


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

    # SQLite checks INSERT's required fields before reaching ON CONFLICT.
    # Merge first so valid partial updates work even for required columns.
    row = {**(read_row(connection, table, row[table.key]) or {}), **row}
    if table.name == "questions":
        _check_question_row(row)
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


def check_row(table: SyncedTable, key: str, row: object, *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
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
        field = f"{table.name}.{column}"
        if value is None:
            if column not in _NULLABLE.get(table.name, set()):
                raise SyncError(f"{field} may not be empty")
            cleaned[column] = None
            continue
        if (table.name, column) in _INTEGERS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SyncError(f"{field} must be a nonnegative whole number")
        elif not isinstance(value, str):
            raise SyncError(f"{field} must be text")
        if isinstance(value, str) and len(value) > MAX_TEXT:
            raise SyncError(f"{field} is longer than {MAX_TEXT} characters")
        if column in table.json_columns:
            _check_json(table.name, column, value)
        if column.endswith("_at"):
            try:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
                    raise ValueError("noncanonical time")
                datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                raise SyncError(f"{field} must be a UTC time in YYYY-MM-DDTHH:MM:SSZ form") from None
        cleaned[column] = value
    if table.name == "questions":
        _check_question_row({**(existing or {}), **cleaned})
    return cleaned


def screen_row(table: SyncedTable, row: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    """Screen a checked row's new or changed free text for secrets, as the store screens what is written here.

    Memory content (``table.content``) holding one is refused; a secret in what was said about a memory
    (``table.prose``) is redacted. Text kept unchanged passes, so retiring a memory saved before the screening
    knew a shape is never refused (review 2026-10-24, R4). JSON columns are read string by string.
    """

    content = []
    for column in table.content:
        if not isinstance(row.get(column), str) or (existing is not None and row[column] == existing.get(column)):
            continue
        if column in table.json_columns:
            # Element by element: a list that keeps an old tag while losing another (closing a question takes off
            # "disputed") is a cleanup, and the tag it keeps was there before (Codex's review of step A).
            before = _decoded_list(existing.get(column)) if existing is not None else []
            content.extend(item for item in _decoded_list(row[column]) if item not in before)
        else:
            content.append(row[column])
    if contains_secret("\n".join(str(text) for text in content)):
        raise SyncError("the memory looks like it holds a secret; save where the secret is kept instead")
    redact_prose(table, row, existing)
    return row


def redact_prose(table: SyncedTable, row: dict[str, Any], existing: dict[str, Any] | None = None) -> bool:
    """Redact any secret in the row's prose columns that are new or changed against ``existing``; whether any was."""

    redacted = False
    for column in table.prose:
        value = row.get(column)
        if not isinstance(value, str) or (existing is not None and value == existing.get(column)):
            continue
        if column in table.json_columns:
            decoded = json.loads(value)
            cleaned = scrub(decoded)
            if cleaned != decoded:
                row[column], redacted = json.dumps(cleaned), True
        else:
            cleaned = scrub(value)
            if cleaned != value:
                row[column], redacted = cleaned, True
    return redacted


def _decoded_list(value: object) -> list[Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else []
    except ValueError:
        return []
    return decoded if isinstance(decoded, list) else [decoded]


def _check_question_row(row: dict[str, Any]) -> None:
    if "kind" not in row:
        return  # a partial update is checked against the stored kind by write_row
    try:
        fields = {column: json.loads(row[column]) for column in ("record_ids", "options") if column in row}
        check_question(row["kind"], **fields)
    except ValueError as exc:
        raise SyncError(str(exc)) from None


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


def apply_pulled(store: Store, changes: Sequence[dict[str, Any]], *, cursor: int | None = None,
                 hold: list[int] | None = None, set_aside: Sequence[dict[str, Any]] | None = None) -> int:
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

    The cursor stays behind a row left alone for an unsent change here
    (review 2026-10-24, F-11b): the server may answer that change from its
    record of an earlier send, without the newer version another computer
    made meanwhile, so the next fetch must bring that version again.
    ``hold`` collects where it stays, across the pages of one fetch.
    ``set_aside``, when given, replaces the rows set aside
    (:func:`keep_set_aside`) in the same transaction as the cursor.
    """

    connection = store.connection
    changed = 0
    hold = [] if hold is None else hold
    redacted: list[tuple[str, str]] = []
    with store.transaction():
        connection.execute("PRAGMA defer_foreign_keys = ON")
        # Read inside the transaction, so a change saved here a moment ago is never overwritten.
        mine = pending_keys(store)
        unsent = unsent_keys(store)
        store.set_meta(APPLYING_KEY, "1")
        for item in changes:
            name = item.get("table")
            table = SYNCED_TABLES.get(name) if isinstance(name, str) else None
            if table is None:
                continue  # shared by a newer server; this version has nowhere to keep it
            key = check_key(item.get("key"))
            if (table.name, key) in mine:
                seq = item.get("seq")
                if (table.name, key) in unsent and isinstance(seq, int) and not isinstance(seq, bool):
                    hold.append(seq - 1)
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
                row = check_row(table, key, row, existing=current)
                # A secret in a reason or finding that reached the server before it screened them (an older
                # version) is redacted here too, and the cleaned row sent back, so every copy loses it (Codex's
                # review of step A). Memory text is left as it came: the store refuses new secrets, not old ones.
                if redact_prose(table, row, current) and store.get_meta(TRACK_KEY) is not None:
                    redacted.append((table.name, key))
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
            store.set_meta(CURSOR_KEY, str(int(min([cursor, *hold]))))
        if set_aside is not None:
            keep_set_aside(store, set_aside)
        connection.execute("DELETE FROM meta WHERE key = ?", (APPLYING_KEY,))
        for name, key in redacted:  # sent back without the secret, based on the version fetched
            connection.execute("INSERT INTO changes (tbl, key, op) VALUES (?, ?, 'upsert')", (name, key))
    return changed


def parent_links(table: SyncedTable) -> list[tuple[str, SyncedTable]]:
    """The links from a shared table's rows to the shared rows they belong to: (column, parent table).

    Declared with the table (``SyncedTable.parents``), not read from SQLite's foreign keys, which name only a
    memory's project (review 2026-10-24, F-14).
    """

    return [(column, SYNCED_TABLES[parent]) for column, parent in table.parents]


def parents_of(table: SyncedTable, row: dict[str, Any]) -> list[tuple[str, str]]:
    """The (table, key) of each row this row belongs to."""

    return [(parent.name, row[column]) for column, parent in parent_links(table)
            if isinstance(row.get(column), str) and row[column]]


def usable(store: Store, changes: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[tuple[dict, str]]]:
    """Split fetched changes into those this version can save and those it cannot, each with the reason.

    One row a newer server shares in a form this version does not accept (such as a new kind of question) is
    set aside on its own, instead of failing the page and every sync after it (review 2026-10-24, F-12).
    """

    good: list[dict[str, Any]] = []
    bad: list[tuple[dict, str]] = []
    for item in changes:
        if not isinstance(item, dict):
            raise SyncError("each fetched change must be an object")
        table = SYNCED_TABLES.get(item.get("table")) if isinstance(item.get("table"), str) else None
        if table is None:
            good.append(item)  # skipped when applied: this version has nowhere to keep it
            continue
        try:
            key = check_key(item.get("key"))
            if item.get("op") != "delete":
                row = item.get("row")
                if isinstance(row, dict):
                    row = {column: value for column, value in row.items() if column in table.columns}
                check_row(table, key, row, existing=read_row(store.connection, table, key))
        except SyncError as exc:
            bad.append((item, str(exc)))
            continue
        good.append(item)
    return good, bad


# Rows fetched from the server that this computer could not save yet, each with what it needs: a newer version of
# KnowItAll2, or a row it belongs to that has not arrived. Kept whole, so nothing is lost when the fetch moves on,
# and tried again at every fetch (review 2026-10-24, F-12).
SET_ASIDE_KEY = "sync.set_aside"


def set_aside(store: Store) -> list[dict[str, Any]]:
    try:
        value = json.loads(store.get_meta(SET_ASIDE_KEY) or "[]")
    except ValueError:
        return []
    return [item for item in value if isinstance(item, dict) and isinstance(item.get("change"), dict)] \
        if isinstance(value, list) else []


def keep_set_aside(store: Store, entries: Sequence[dict[str, Any]]) -> None:
    """Replace the rows set aside, journaling each one newly set aside once."""

    before = {(item["change"].get("table"), item["change"].get("key"), item["change"].get("seq"))
              for item in set_aside(store)}
    with store.transaction():
        store.set_meta(SET_ASIDE_KEY, json.dumps(list(entries), ensure_ascii=False))
    for entry in entries:
        change = entry["change"]
        if (change.get("table"), change.get("key"), change.get("seq")) not in before:
            journal.problem("sync", f"a row from the server was set aside, to be tried again at each sync: "
                                    f"{change.get('table')} {change.get('key')}: {entry.get('reason')}")


def still_wanted(store: Store, change: dict[str, Any]) -> bool:
    """Whether a row set aside earlier may still be saved: nothing newer of it is here or on its way here."""

    table = SYNCED_TABLES.get(change.get("table")) if isinstance(change.get("table"), str) else None
    key, seq = change.get("key"), change.get("seq")
    if table is None or not isinstance(key, str) or (table.name, key) in pending_keys(store):
        return False
    seen = store.connection.execute("SELECT seq FROM sync_seen WHERE tbl = ? AND key = ?", (table.name, key)).fetchone()
    return not (seen is not None and isinstance(seq, int) and seen[0] >= seq)


def without_parents(store: Store, changes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fetched changes that must wait: their row belongs to a row neither here nor among ``changes``.

    Such as a memory whose project comes on a later page: saving it now
    would break the link, and the whole page with it.
    """

    # Validate before dependency inspection too: malformed foreign keys must not
    # reach hashing or SQLite parameter binding before check_row sees them.
    recognized = []
    for item in changes:
        if not isinstance(item, dict):
            raise SyncError("each fetched change must be an object")
        table = SYNCED_TABLES.get(item.get("table")) if isinstance(item.get("table"), str) else None
        if table is None:
            continue
        key = check_key(item.get("key"))
        if item.get("op") != "delete":
            row = item.get("row")
            if isinstance(row, dict):
                row = {column: value for column, value in row.items() if column in table.columns}
            check_row(table, key, row)
        recognized.append(item)
    changes = recognized
    connection = store.connection
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
            for column, parent in parent_links(table):
                value = row.get(column)
                if value is None or (parent.name, value) in arriving:
                    continue
                if connection.execute(f"SELECT 1 FROM {parent.name} WHERE {parent.key} = ?",
                                      (value,)).fetchone() is None:
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


def unsent_keys(store: Store) -> set[tuple[str, str]]:
    """The rows (table, key) with a change made here that the server has not settled yet."""

    return {(row[0], row[1]) for row in store.connection.execute("SELECT DISTINCT tbl, key FROM changes").fetchall()}


def pending_keys(store: Store) -> set[tuple[str, str]]:
    """The rows a fetch leaves alone: unsent ones, and those the server refused, kept here until resolved."""

    return unsent_keys(store) | {(item["table"], item["key"]) for item in rejections(store)}


REJECTIONS_KEY = "sync.rejected_operations"
ARCHIVED_REJECTIONS_KEY = "sync.archived_rejections"


def rejections(store: Store) -> list[dict[str, Any]]:
    try:
        value = json.loads(store.get_meta(REJECTIONS_KEY) or "[]")
        if not isinstance(value, list) or any(not isinstance(item, dict) or
                not all(key in item for key in ("id", "table", "key")) for item in value):
            raise ValueError("invalid rejection list")
        return value
    except (ValueError, TypeError) as exc:
        raise SyncError("the local rejected-change queue needs repair; it has been preserved") from exc


def quarantine(store: Store, operation: dict, reason: str, *, waits_for: tuple[str, str] | None = None) -> None:
    """Retain terminal refusals locally, without publishing or repeatedly sending poison content.

    ``waits_for`` names the refused row (table, key) this one belongs to: sending it again is retried with it.
    """
    from .secrets import redact
    items = [item for item in rejections(store)
             if (item["table"], item["key"]) != (operation["table"], operation["key"])]
    sensitive = contains_secret(json.dumps(operation, ensure_ascii=False))
    entry = {"id": operation["op_id"], "table": operation["table"], "key": operation["key"],
             "reason": redact(str(reason))[:600], "at": journal.utc_now(), "sensitive": sensitive,
             "operation": None if sensitive else operation}
    if waits_for is not None:
        entry["waits_for"] = list(waits_for)
    items.append(entry)
    store.set_meta(REJECTIONS_KEY, json.dumps(items))


def refused_parent(store: Store, table: SyncedTable, row: dict[str, Any], *,
                   also: Iterable[tuple[str, str]] = ()) -> tuple[str, str] | None:
    """The row this one belongs to that the server refused and that is kept here unresolved, if any.

    Sending the row would only be refused again, every sync, until that row is resolved (review 2026-10-24, F-13).
    ``also``: rows refused in the batch being settled, not yet among the refusals kept here.
    """

    refused = {(item["table"], item["key"]) for item in rejections(store)} | set(also)
    return next((parent for parent in parents_of(table, row) if parent in refused), None)


def resolve_rejection(store: Store, identifier: str, *, retry: bool = False) -> None:
    from .secrets import contains_secret
    items = rejections(store)
    item = next((item for item in items if item["id"] == identifier), None)
    if item is None:
        raise SyncError("no unresolved rejection has that id")
    with store.transaction():
        if retry:
            table = table_for(item["table"])
            current = read_row(store.connection, table, item["key"])
            if current is None or contains_secret(json.dumps(current, ensure_ascii=False)):
                raise SyncError("correct or remove the sensitive/missing local content before retrying")
            store.connection.execute("INSERT INTO changes (tbl, key, op) VALUES (?, ?, 'upsert')",
                                     (item["table"], item["key"]))
            # The rows set aside because the server refused this one go again with it.
            children = [old for old in items if old.get("waits_for") == [item["table"], item["key"]]]
            for child in children:
                store.connection.execute("INSERT INTO changes (tbl, key, op) VALUES (?, ?, 'upsert')",
                                         (child["table"], child["key"]))
            items = [old for old in items if old not in children]
        else:
            # Dismiss means keep the server's version. Re-fetch it even if our pull cursor passed it.
            store.set_meta(CURSOR_KEY, "0")
        store.set_meta(REJECTIONS_KEY, json.dumps([old for old in items if old["id"] != identifier]))


def clear_rejections(store: Store, operation: dict) -> None:
    items = rejections(store)
    kept = [item for item in items if (item["table"], item["key"]) != (operation["table"], operation["key"])]
    if kept != items:
        store.set_meta(REJECTIONS_KEY, json.dumps(kept))


def archive_rejections(store: Store) -> None:
    """Keep old-server evidence without protecting its rows from a new connection's pulls."""
    items = rejections(store)
    if items:
        previous = json.loads(store.get_meta(ARCHIVED_REJECTIONS_KEY) or "[]")
        store.set_meta(ARCHIVED_REJECTIONS_KEY, json.dumps([*previous, *items]))
        store.set_meta(REJECTIONS_KEY, "[]")


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
        store.set_meta(SET_ASIDE_KEY, "[]")  # fetched again from the start
        for name in SYNCED_TABLES:  # parents first
            note_all(store, name)


def note_parents(store: Store, table: SyncedTable, row: dict[str, Any]) -> None:
    """Note the rows a row belongs to (such as a memory's project) as changed here, when they are here and
    not noted already, so a sync sends them and the row waiting for them on the server can follow.

    A row the server refused is not noted again: it stays with the refusals until it is resolved."""

    connection = store.connection
    refused = {(item["table"], item["key"]) for item in rejections(store)}
    with store.transaction():
        for column, parent in parent_links(table):
            value = row.get(column)
            if value is None or (parent.name, value) in refused:
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
