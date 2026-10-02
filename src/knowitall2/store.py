"""Local SQLite storage with full-text search for KnowItAll2 memories."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .identity import ProjectIdentity

SCHEMA_VERSION = 3

# Statements that bring an existing database up to each schema version. A new
# database gets the current schema directly from _SCHEMA.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE records ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE records ADD COLUMN last_used_at TEXT",
    ),
    3: ("ALTER TABLE records ADD COLUMN source_computer TEXT",),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    remote TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_paths (
    project_id TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (project_id, path)
);
CREATE TABLE IF NOT EXISTS records (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    subjects TEXT NOT NULL,
    tags TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id TEXT REFERENCES projects (id),
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'retired')),
    verification TEXT NOT NULL CHECK (verification IN ('user_stated', 'observed', 'unverified')),
    source_kind TEXT NOT NULL,
    source_agent TEXT,
    source_session TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    superseded_by TEXT,
    retired_reason TEXT,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    source_computer TEXT,
    CHECK ((scope = 'project') = (project_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS records_by_scope ON records (status, scope, project_id);
CREATE INDEX IF NOT EXISTS records_by_hash ON records (content_hash, status);
CREATE TABLE IF NOT EXISTS usage (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('recall', 'briefing')),
    agent TEXT,
    project_id TEXT,
    query TEXT,
    result_count INTEGER NOT NULL,
    result_ids TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_by_time ON usage (at);
CREATE TABLE IF NOT EXISTS questions (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    prompt TEXT NOT NULL,
    record_ids TEXT NOT NULL,
    options TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'answered')),
    created_at TEXT NOT NULL,
    answered_at TEXT,
    answer TEXT
);
CREATE INDEX IF NOT EXISTS questions_by_status ON questions (status, seq);
CREATE TABLE IF NOT EXISTS reviews (
    fingerprint TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('reviewed', 'failed')),
    failures INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reviews_by_scope ON reviews (scope_key, outcome, reviewed_at);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT,
    agent TEXT,
    project_id TEXT,
    session TEXT,
    run_id TEXT,
    summary TEXT NOT NULL,
    record_ids TEXT NOT NULL DEFAULT '[]',
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_by_time ON events (at);
CREATE INDEX IF NOT EXISTS events_by_kind ON events (kind, at);
CREATE INDEX IF NOT EXISTS events_by_run ON events (run_id);
CREATE TABLE IF NOT EXISTS systems (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    area TEXT NOT NULL,
    kind TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    gaps TEXT NOT NULL DEFAULT '[]',
    profiled TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS record_notes (
    record_id TEXT PRIMARY KEY,
    headline TEXT NOT NULL,
    system_id TEXT,
    facet TEXT NOT NULL,
    written_by TEXT NOT NULL,
    written_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS record_notes_by_system ON record_notes (system_id);
CREATE TABLE IF NOT EXISTS question_stages (
    question_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    plain TEXT,
    labels TEXT NOT NULL DEFAULT '{}',
    reason TEXT,
    findings TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    question_id TEXT,
    system_id TEXT,
    project_id TEXT,
    facet TEXT,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    offered INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_tasks_by_status ON agent_tasks (status, kind);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5 (
    text, subjects, tags,
    content = 'records',
    content_rowid = 'seq',
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3'
);
CREATE TRIGGER IF NOT EXISTS records_fts_insert AFTER INSERT ON records BEGIN
    INSERT INTO records_fts (rowid, text, subjects, tags)
    VALUES (new.seq, new.text, new.subjects, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS records_fts_delete AFTER DELETE ON records BEGIN
    INSERT INTO records_fts (records_fts, rowid, text, subjects, tags)
    VALUES ('delete', old.seq, old.text, old.subjects, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS records_fts_update AFTER UPDATE OF text, subjects, tags ON records BEGIN
    INSERT INTO records_fts (records_fts, rowid, text, subjects, tags)
    VALUES ('delete', old.seq, old.text, old.subjects, old.tags);
    INSERT INTO records_fts (rowid, text, subjects, tags)
    VALUES (new.seq, new.text, new.subjects, new.tags);
END;
CREATE TABLE IF NOT EXISTS changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL,
    key TEXT NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
    by_connection TEXT,
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS changes_by_key ON changes (tbl, key, seq);
CREATE TABLE IF NOT EXISTS sync_seen (
    tbl TEXT NOT NULL,
    key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (tbl, key)
);
"""


@dataclass(frozen=True)
class SyncedTable:
    """A table a KnowItAll2 server shares between computers.

    ``columns`` is what travels (the table's own ``seq`` never does); a change
    to any of ``tracked`` counts as a change worth sharing. ``stamp`` names the
    columns that say which of two versions is newer (the first one set counts).
    """

    name: str
    key: str
    columns: tuple[str, ...]
    tracked: tuple[str, ...]
    stamp: tuple[str, ...]
    json_columns: tuple[str, ...] = ()
    deletable: bool = False


def _synced(name: str, key: str, columns: str, *, quiet: str = "", stamp: str, json_columns: str = "",
            deletable: bool = False) -> SyncedTable:
    names = tuple(column.strip() for column in columns.split(","))
    silent = {column.strip() for column in quiet.split(",") if column.strip()}
    return SyncedTable(
        name, key, names, tuple(column for column in names if column not in silent and column != key),
        tuple(column.strip() for column in stamp.split(",")),
        tuple(column.strip() for column in json_columns.split(",") if column.strip()), deletable,
    )


# Parents before children, so a full copy can be sent in this order.
SYNCED_TABLES: dict[str, SyncedTable] = {table.name: table for table in (
    _synced("projects", "id", "id, name, remote, created_at, last_seen_at", quiet="created_at, last_seen_at",
            stamp="last_seen_at"),
    _synced("systems", "id", "id, name, area, kind, aliases, summary, gaps, profiled, created_at, updated_at",
            stamp="updated_at", json_columns="aliases, gaps"),
    _synced("records", "id", "id, kind, text, subjects, tags, scope, project_id, status, verification, "
            "source_kind, source_agent, source_session, content_hash, created_at, updated_at, confirmed_at, "
            "superseded_by, retired_reason, recall_count, last_used_at, source_computer",
            quiet="recall_count, last_used_at",
            stamp="updated_at", json_columns="subjects, tags"),
    _synced("record_notes", "record_id", "record_id, headline, system_id, facet, written_by, written_at",
            stamp="written_at"),
    _synced("questions", "id", "id, kind, prompt, record_ids, options, status, created_at, answered_at, answer",
            stamp="answered_at, created_at", json_columns="record_ids, options"),
    _synced("question_stages", "question_id", "question_id, stage, plain, labels, reason, findings, updated_at",
            stamp="updated_at", json_columns="labels, findings"),
    _synced("agent_tasks", "id", "id, kind, question_id, system_id, project_id, facet, prompt, status, offered, "
            "result, created_at, updated_at", stamp="updated_at"),
    _synced("reviews", "fingerprint", "fingerprint, scope_key, reviewed_at, outcome, failures",
            stamp="reviewed_at", deletable=True),
)}

# Change tracking is off unless the database says so: a server always tracks,
# a computer connected to one tracks its own changes to send. While a
# computer applies changes that came from the server, it does not track them.
TRACK_KEY = "changes.track"
APPLYING_KEY = "changes.applying"
BY_KEY = "changes.by"


def _change_triggers() -> str:
    when = (f"EXISTS (SELECT 1 FROM meta WHERE key = '{TRACK_KEY}') "
            f"AND NOT EXISTS (SELECT 1 FROM meta WHERE key = '{APPLYING_KEY}')")
    by = f"(SELECT value FROM meta WHERE key = '{BY_KEY}')"
    statements = []
    for table in SYNCED_TABLES.values():
        changed = " OR ".join(f"old.{column} IS NOT new.{column}" for column in table.tracked)
        for event, row, op, condition in (
            ("INSERT", "new", "upsert", when),
            ("UPDATE", "new", "upsert", f"{when} AND ({changed})"),
            ("DELETE", "old", "delete", when),
        ):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS changes_{table.name}_{event.lower()} AFTER {event} ON {table.name} "
                f"WHEN {condition} BEGIN INSERT INTO changes (tbl, key, op, by_connection) "
                f"VALUES ('{table.name}', {row}.{table.key}, '{op}', {by}); END;"
            )
    return "\n".join(statements)


_CHANGE_TRIGGERS = _change_triggers()
_TRIGGERS_VERSION = hashlib.sha256(_CHANGE_TRIGGERS.encode("utf-8")).hexdigest()[:16]
TRIGGERS_KEY = "changes.triggers"


def computer_name() -> str | None:
    """This computer's name, kept with each memory it saves, so a shared memory says where it came from."""

    name = platform.node().strip()
    return name[:80] or None

_COLUMNS = (
    "r.id, r.kind, r.text, r.subjects, r.tags, r.scope, r.project_id, p.name AS project_name, "
    "r.status, r.verification, r.source_kind, r.source_agent, r.source_session, "
    "r.created_at, r.updated_at, r.confirmed_at, r.superseded_by, r.retired_reason, r.source_computer"
)
_FROM = "FROM records r LEFT JOIN projects p ON p.id = r.project_id"


class StoreError(RuntimeError):
    """The local memory store could not be opened or used."""


@dataclass(frozen=True)
class RecordRow:
    id: str
    kind: str
    text: str
    subjects: tuple[str, ...]
    tags: tuple[str, ...]
    scope: str
    project_id: str | None
    project_name: str | None
    status: str
    verification: str
    source_kind: str
    source_agent: str | None
    source_session: str | None
    created_at: str
    updated_at: str
    confirmed_at: str | None
    superseded_by: str | None
    retired_reason: str | None = None
    source_computer: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RecordRow":
        return cls(
            id=row["id"],
            kind=row["kind"],
            text=row["text"],
            subjects=tuple(json.loads(row["subjects"])),
            tags=tuple(json.loads(row["tags"])),
            scope=row["scope"],
            project_id=row["project_id"],
            project_name=row["project_name"],
            status=row["status"],
            verification=row["verification"],
            source_kind=row["source_kind"],
            source_agent=row["source_agent"],
            source_session=row["source_session"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            confirmed_at=row["confirmed_at"],
            superseded_by=row["superseded_by"],
            retired_reason=row["retired_reason"],
            source_computer=row["source_computer"],
        )


class Store:
    """All reads and writes of the memory database.

    Multi-step writes run inside :meth:`transaction`; single statements are
    atomic on their own. Several agent processes may share one database file.
    """

    def __init__(self, connection: sqlite3.Connection, path: Path | None) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self.path = path

    @classmethod
    def open(cls, path: Path) -> "Store":
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"cannot open the memory store at {path}: {exc}") from exc
        store = cls(connection, path)
        try:
            store._initialize(file_backed=True)
        except BaseException:
            connection.close()
            raise
        return store

    @classmethod
    def in_memory(cls) -> "Store":
        store = cls(sqlite3.connect(":memory:", isolation_level=None), None)
        store._initialize(file_backed=False)
        return store

    def close(self) -> None:
        self._connection.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The raw connection, for the sharing layer (``sync``) that works on whole rows."""

        return self._connection

    def _initialize(self, *, file_backed: bool) -> None:
        try:
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if file_backed:
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(_SCHEMA)
            row = self._connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise StoreError(
                    "this Python's SQLite lacks FTS5 full-text search, which KnowItAll2 requires"
                ) from exc
            raise StoreError(f"cannot initialize the memory store: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"the memory store is unreadable: {exc}") from exc
        if row is None:
            self._connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        elif int(row["value"]) > SCHEMA_VERSION:
            # Most often a long-running agent session whose server started before an
            # update; the memories are fine, and a new session loads the new version.
            raise StoreError(
                f"the memory store was upgraded to schema {row['value']} by a newer KnowItAll2 than this process "
                f"(schema {SCHEMA_VERSION}). If KnowItAll2 was updated while this session was running, restart the "
                "session or the agent app to load the new version; the memories are safe. Otherwise, update "
                "KnowItAll2 (see docs/install-for-agents.md)"
            )
        elif int(row["value"]) < SCHEMA_VERSION:
            self._migrate()
        self._ensure_change_triggers()

    def _ensure_change_triggers(self) -> None:
        """Create the change triggers, or rebuild them when their definition changed (such as a new column)."""

        if self.get_meta(TRIGGERS_KEY) == _TRIGGERS_VERSION:
            return
        try:
            with self.transaction():
                if self.get_meta(TRIGGERS_KEY) == _TRIGGERS_VERSION:
                    return
                for (name,) in self._connection.execute(
                    r"SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'changes\_%' ESCAPE '\'",
                ).fetchall():
                    self._connection.execute(f"DROP TRIGGER {name}")
                for statement in _CHANGE_TRIGGERS.splitlines():
                    self._connection.execute(statement)
                self.set_meta(TRIGGERS_KEY, _TRIGGERS_VERSION)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot prepare the memory store for sharing: {exc}") from exc

    def _migrate(self) -> None:
        """Bring an older database up to date, once, even if several processes open it together.

        A file-backed database is first copied beside itself with SQLite's
        backup API, so the upgrade can always be undone.
        """

        try:
            if self.path is not None:
                version = self._connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
                backup_path = self.path.with_name(f"{self.path.stem}.schema{version}-backup{self.path.suffix}")
                target = sqlite3.connect(str(backup_path))
                try:
                    self._connection.backup(target)
                finally:
                    target.close()
            with self.transaction():
                version = int(self._connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'",
                ).fetchone()["value"])
                for target in range(version + 1, SCHEMA_VERSION + 1):
                    for statement in _MIGRATIONS.get(target, ()):
                        self._connection.execute(statement)
                self._connection.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"cannot upgrade the memory store: {exc}") from exc

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run a block atomically; a nested block joins the outer transaction."""

        if self._connection.in_transaction:
            yield
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def ensure_project(self, identity: ProjectIdentity, now: str) -> None:
        """Record a project known only by its remote, leaving any existing entry as it is."""

        self._connection.execute(
            "INSERT INTO projects (id, name, remote, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO NOTHING",
            (identity.id, identity.name, identity.remote, now, now),
        )

    def touch_project(self, identity: ProjectIdentity, now: str) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO projects (id, name, remote, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET name = excluded.name, remote = excluded.remote, "
                "last_seen_at = excluded.last_seen_at",
                (identity.id, identity.name, identity.remote, now, now),
            )
            self._connection.execute(
                "INSERT INTO project_paths (project_id, path, last_seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT (project_id, path) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (identity.id, identity.path_key, now),
            )

    def insert_record(
        self,
        *,
        record_id: str,
        kind: str,
        text: str,
        subjects: Sequence[str],
        tags: Sequence[str],
        scope: str,
        project_id: str | None,
        verification: str,
        source_kind: str,
        source_agent: str | None,
        content_hash: str,
        now: str,
        source_session: str | None = None,
        source_computer: str | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO records (id, kind, text, subjects, tags, scope, project_id, status, verification, "
            "source_kind, source_agent, source_session, content_hash, created_at, updated_at, confirmed_at, "
            "source_computer) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, kind, text, json.dumps(list(subjects)), json.dumps(list(tags)), scope, project_id,
                verification, source_kind, source_agent, source_session, content_hash, now, now, now,
                source_computer or computer_name(),
            ),
        )

    def get(self, record_id: str) -> RecordRow | None:
        row = self._connection.execute(f"SELECT {_COLUMNS} {_FROM} WHERE r.id = ?", (record_id,)).fetchone()
        return RecordRow.from_row(row) if row else None

    def find_active_duplicate(self, content_hash: str) -> RecordRow | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} {_FROM} WHERE r.content_hash = ? AND r.status = 'active' LIMIT 1",
            (content_hash,),
        ).fetchone()
        return RecordRow.from_row(row) if row else None

    def confirm(self, record_id: str, *, verification: str, now: str) -> None:
        self._connection.execute(
            "UPDATE records SET verification = ?, confirmed_at = ?, updated_at = ? WHERE id = ?",
            (verification, now, now, record_id),
        )

    def supersede(self, old_id: str, new_id: str, *, now: str, reason: str | None = None) -> None:
        self._connection.execute(
            "UPDATE records SET status = 'superseded', superseded_by = ?, retired_reason = ?, updated_at = ? "
            "WHERE id = ? AND status = 'active'",
            (new_id, reason, now, old_id),
        )

    def restore(self, record_id: str, *, verification: str, now: str) -> bool:
        """Make a retired or superseded memory active again; True if it changed."""

        cursor = self._connection.execute(
            "UPDATE records SET status = 'active', superseded_by = NULL, retired_reason = NULL, "
            "verification = ?, confirmed_at = ?, updated_at = ? WHERE id = ? AND status != 'active'",
            (verification, now, now, record_id),
        )
        return cursor.rowcount == 1

    def list_inactive(self, *, limit: int) -> list[RecordRow]:
        """Retired and superseded memories, most recently changed first."""

        rows = self._connection.execute(
            f"SELECT {_COLUMNS} {_FROM} WHERE r.status != 'active' ORDER BY r.updated_at DESC, r.seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [RecordRow.from_row(row) for row in rows]

    def active_scopes(self) -> list[tuple[str, str | None]]:
        """Each scope that holds active memories: global first, then projects by size."""

        rows = self._connection.execute(
            "SELECT scope, project_id, COUNT(*) AS total FROM records WHERE status = 'active' "
            "GROUP BY scope, project_id ORDER BY scope = 'project', total DESC, project_id",
        ).fetchall()
        return [(row["scope"], row["project_id"]) for row in rows]

    def retire(self, record_id: str, *, reason: str | None, now: str) -> bool:
        cursor = self._connection.execute(
            "UPDATE records SET status = 'retired', retired_reason = ?, updated_at = ? "
            "WHERE id = ? AND status = 'active'",
            (reason, now, record_id),
        )
        return cursor.rowcount == 1

    def search(
        self, match: str, *, project_id: str | None, scope: str, limit: int,
    ) -> list[tuple[RecordRow, float]]:
        """Full-text search; returns rows with their bm25 rank (lower is better)."""

        where, parameters = _scope_filter(scope, project_id)
        if where is None:
            return []
        sql = (
            f"SELECT {_COLUMNS}, bm25(records_fts, 1.0, 2.0, 1.5) AS rank "
            "FROM records_fts JOIN records r ON r.seq = records_fts.rowid "
            "LEFT JOIN projects p ON p.id = r.project_id "
            f"WHERE records_fts MATCH ? AND r.status = 'active' AND {where} "
            "ORDER BY rank LIMIT ?"
        )
        try:
            rows = self._connection.execute(sql, (match, *parameters, limit)).fetchall()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
                return []
            raise
        return [(RecordRow.from_row(row), float(row["rank"])) for row in rows]

    def count_matching(self, match: str, *, project_id: str | None, scope: str) -> int:
        """How many active memories in a recall scope match a full-text query."""

        where, parameters = _scope_filter(scope, project_id)
        if where is None or not match:
            return 0
        try:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM records_fts JOIN records r ON r.seq = records_fts.rowid "
                f"WHERE records_fts MATCH ? AND r.status = 'active' AND {where}",
                (match, *parameters),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
                return 0
            raise
        return int(row[0])

    def substring_search(
        self, needle: str, *, project_id: str | None, scope: str, limit: int,
    ) -> list[RecordRow]:
        where, parameters = _scope_filter(scope, project_id)
        if where is None:
            return []
        pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} {_FROM} WHERE r.status = 'active' AND {where} "
            "AND (r.text LIKE ? ESCAPE '\\' OR r.subjects LIKE ? ESCAPE '\\') "
            "ORDER BY r.updated_at DESC LIMIT ?",
            (*parameters, pattern, pattern, limit),
        ).fetchall()
        return [RecordRow.from_row(row) for row in rows]

    def list_active(
        self,
        *,
        project_id: str | None,
        scope: str,
        kinds: Sequence[str] = (),
        exclude_kinds: Sequence[str] = (),
        limit: int,
    ) -> list[RecordRow]:
        where, parameters = _kind_filter(scope, project_id, kinds, exclude_kinds)
        if where is None:
            return []
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} {_FROM} WHERE r.status = 'active' AND {where} "
            "ORDER BY COALESCE(r.confirmed_at, r.updated_at) DESC, r.seq DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [RecordRow.from_row(row) for row in rows]

    def count_active(
        self,
        *,
        project_id: str | None,
        scope: str,
        kinds: Sequence[str] = (),
        exclude_kinds: Sequence[str] = (),
    ) -> int:
        where, parameters = _kind_filter(scope, project_id, kinds, exclude_kinds)
        if where is None:
            return 0
        row = self._connection.execute(
            f"SELECT COUNT(*) AS total {_FROM} WHERE r.status = 'active' AND {where}", parameters,
        ).fetchone()
        return int(row["total"])

    def add_question(
        self, *, question_id: str, kind: str, prompt: str, record_ids: Sequence[str],
        options: Sequence[dict[str, str]], now: str,
    ) -> bool:
        """Queue a question unless an open one already covers the same memories; True if added."""

        key = json.dumps(sorted(record_ids))
        for row in self._connection.execute(
            "SELECT record_ids FROM questions WHERE status = 'open' AND kind = ?", (kind,),
        ).fetchall():
            if json.dumps(sorted(json.loads(row[0]))) == key:
                return False
        self._connection.execute(
            "INSERT INTO questions (id, kind, prompt, record_ids, options, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (question_id, kind, prompt, json.dumps(list(record_ids)), json.dumps(list(options)), now),
        )
        return True

    def open_questions(self, *, limit: int) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT id, kind, prompt, record_ids, options, created_at FROM questions "
            "WHERE status = 'open' ORDER BY seq LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"id": row[0], "kind": row[1], "prompt": row[2], "record_ids": json.loads(row[3]),
             "options": json.loads(row[4]), "created_at": row[5]}
            for row in rows
        ]

    def question(self, question_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT id, kind, prompt, record_ids, options, status, answer FROM questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "kind": row[1], "prompt": row[2], "record_ids": json.loads(row[3]),
                "options": json.loads(row[4]), "status": row[5], "answer": row[6]}

    def close_question(self, question_id: str, *, answer: str, now: str) -> None:
        self._connection.execute(
            "UPDATE questions SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
            (answer, now, question_id),
        )

    def answered_questions(self, *, limit: int) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT id, kind, prompt, record_ids, answer, answered_at FROM questions WHERE status = 'answered' "
            "ORDER BY answered_at DESC, seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"id": row[0], "kind": row[1], "prompt": row[2], "record_ids": json.loads(row[3]), "answer": row[4],
             "answered_at": row[5]}
            for row in rows
        ]

    # --- The catalog: systems, and each memory's note (plain summary, system, part of the profile) ---

    def systems_list(self) -> list[dict[str, Any]]:
        rows = self._connection.execute("SELECT * FROM systems ORDER BY name COLLATE NOCASE").fetchall()
        return [_system_dict(row) for row in rows]

    def system(self, system_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM systems WHERE id = ?", (system_id,)).fetchone()
        return _system_dict(row) if row else None

    def upsert_system(
        self, *, system_id: str, name: str, area: str, kind: str, aliases: Sequence[str], now: str,
    ) -> None:
        """Add a system, or update its name, area, and kind and add to its aliases."""

        existing = self.system(system_id)
        merged = list(dict.fromkeys([*(existing["aliases"] if existing else []), *aliases]))
        self._connection.execute(
            "INSERT INTO systems (id, name, area, kind, aliases, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, area = excluded.area, kind = excluded.kind, "
            "aliases = excluded.aliases, updated_at = excluded.updated_at",
            (system_id, name, area, kind, json.dumps(merged), now, now),
        )

    def set_system_profile(self, system_id: str, *, summary: str, gaps: Sequence[str], profiled: str, now: str) -> None:
        self._connection.execute(
            "UPDATE systems SET summary = ?, gaps = ?, profiled = ?, updated_at = ? WHERE id = ?",
            (summary, json.dumps(list(gaps)), profiled, now, system_id),
        )

    def set_system_gaps(self, system_id: str, *, gaps: Sequence[str], now: str) -> None:
        """Replace only what a system's profile says is missing, such as after a gap was filled."""

        self._connection.execute(
            "UPDATE systems SET gaps = ?, updated_at = ? WHERE id = ?", (json.dumps(list(gaps)), now, system_id),
        )

    def set_note(
        self, record_id: str, *, headline: str, system_id: str | None, facet: str, written_by: str, now: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO record_notes (record_id, headline, system_id, facet, written_by, written_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (record_id) DO UPDATE SET headline = excluded.headline, "
            "system_id = excluded.system_id, facet = excluded.facet, written_by = excluded.written_by, "
            "written_at = excluded.written_at",
            (record_id, headline, system_id, facet, written_by, now),
        )

    def notes_for(self, record_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not record_ids:
            return {}
        rows = self._connection.execute(
            f"SELECT n.*, s.name AS system_name, s.area AS system_area FROM record_notes n "
            f"LEFT JOIN systems s ON s.id = n.system_id WHERE n.record_id IN ({', '.join('?' * len(record_ids))})",
            tuple(record_ids),
        ).fetchall()
        return {row["record_id"]: dict(row) for row in rows}

    def unnoted_records(self, *, limit: int) -> list[RecordRow]:
        """Active memories that have no note yet, oldest first."""

        rows = self._connection.execute(
            f"SELECT {_COLUMNS} {_FROM} WHERE r.status = 'active' "
            "AND NOT EXISTS (SELECT 1 FROM record_notes n WHERE n.record_id = r.id) ORDER BY r.seq LIMIT ?",
            (limit,),
        ).fetchall()
        return [RecordRow.from_row(row) for row in rows]

    def count_unnoted(self) -> int:
        return int(self._connection.execute(
            "SELECT COUNT(*) FROM records r WHERE r.status = 'active' "
            "AND NOT EXISTS (SELECT 1 FROM record_notes n WHERE n.record_id = r.id)",
        ).fetchone()[0])

    def system_records(self, system_id: str | None) -> list[dict[str, Any]]:
        """The active memories filed under a system (None: filed under no system), with their notes."""

        condition = "n.system_id = ?" if system_id is not None else "n.system_id IS NULL"
        rows = self._connection.execute(
            f"SELECT {_COLUMNS}, r.recall_count, r.last_used_at, n.headline, n.facet {_FROM} "
            f"JOIN record_notes n ON n.record_id = r.id WHERE r.status = 'active' AND {condition} "
            "ORDER BY r.seq",
            (system_id,) if system_id is not None else (),
        ).fetchall()
        found = []
        for row in rows:
            item = _record_dict(row)
            item["headline"], item["facet"] = row["headline"], row["facet"]
            found.append(item)
        return found

    def contents(
        self, project_id: str, *, extra_systems: Sequence[str] = (), since: str | None = None,
        include_rules: bool = False,
    ) -> list[dict[str, Any]]:
        """What is known for a project: its own active memories, and the shared (global) memories filed under
        the systems its own memories are filed under or under ``extra_systems``, newest first.

        Each item has the memory (``record``), its note (``headline``, ``facet``, ``system_id``,
        ``system_name``; empty until the memory is catalogued), and ``weight``: how many of the project's
        own memories are filed under the same system. ``since`` keeps only memories added from then on.
        """

        extra = tuple(extra_systems)
        conditions = ["r.status = 'active'"]
        parameters: list[Any] = [project_id, project_id]
        if not include_rules:
            conditions.append("r.kind != 'rule'")
        if since is not None:
            conditions.append("r.created_at >= ?")  # the same second as ``since`` counts; callers skip what they showed
            parameters.append(since)
        shared = "n.system_id IN (SELECT system_id FROM mine)"
        if extra:
            shared = f"({shared} OR n.system_id IN ({', '.join('?' * len(extra))}))"
        rows = self._connection.execute(
            "WITH mine AS (SELECT n.system_id AS system_id, COUNT(*) AS weight FROM records r "
            "JOIN record_notes n ON n.record_id = r.id WHERE r.status = 'active' AND r.scope = 'project' "
            "AND r.project_id = ? AND n.system_id IS NOT NULL GROUP BY n.system_id) "
            f"SELECT {_COLUMNS}, n.headline, n.facet, n.system_id, s.name AS system_name, "
            "COALESCE(m.weight, 0) AS weight "
            f"{_FROM} LEFT JOIN record_notes n ON n.record_id = r.id LEFT JOIN systems s ON s.id = n.system_id "
            "LEFT JOIN mine m ON m.system_id = n.system_id "
            f"WHERE {' AND '.join(conditions)} AND (r.project_id = ? OR (r.scope = 'global' AND {shared})) "
            "ORDER BY COALESCE(r.confirmed_at, r.updated_at) DESC, r.seq DESC",
            (parameters[0], *parameters[2:], parameters[1], *extra),
        ).fetchall()
        return [
            {"record": RecordRow.from_row(row), "headline": row["headline"], "facet": row["facet"],
             "system_id": row["system_id"], "system_name": row["system_name"], "weight": int(row["weight"])}
            for row in rows
        ]

    def set_tags(self, record_id: str, tags: Sequence[str], *, now: str) -> None:
        self._connection.execute(
            "UPDATE records SET tags = ?, updated_at = ? WHERE id = ?",
            (json.dumps(list(tags)), now, record_id),
        )

    def project_system_ids(self, project_id: str) -> set[str]:
        """The systems a project's own active memories are filed under."""

        rows = self._connection.execute(
            "SELECT DISTINCT n.system_id FROM records r JOIN record_notes n ON n.record_id = r.id "
            "WHERE r.status = 'active' AND r.scope = 'project' AND r.project_id = ? AND n.system_id IS NOT NULL",
            (project_id,),
        ).fetchall()
        return {row[0] for row in rows}

    def move_record(self, record_id: str, *, project_id: str, content_hash: str, now: str) -> bool:
        """File an active project memory under another project."""

        cursor = self._connection.execute(
            "UPDATE records SET project_id = ?, content_hash = ?, updated_at = ? "
            "WHERE id = ? AND status = 'active' AND scope = 'project'",
            (project_id, content_hash, now, record_id),
        )
        return cursor.rowcount == 1

    def candidate_origin(self, record_id: str) -> tuple[str | None, str | None]:
        """The agent and project of the chat a memory was learned from, from its learning candidate."""

        row = self._connection.execute(
            "SELECT e.agent, p.name FROM events e LEFT JOIN projects p ON p.id = e.project_id "
            "WHERE e.kind = 'candidate' AND e.record_ids LIKE ? ORDER BY e.seq LIMIT 1",
            (f'%"{record_id}"%',),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def learned_from(self, session_id: str) -> bool:
        """Whether learning ever proposed a memory from this session, or any memory came from it."""

        row = self._connection.execute(
            "SELECT EXISTS (SELECT 1 FROM events WHERE kind = 'candidate' AND session = ?) "
            "OR EXISTS (SELECT 1 FROM records WHERE source_session = ?)", (session_id, session_id),
        ).fetchone()
        return bool(row[0])

    def project_named(self, name: str) -> dict[str, Any] | None:
        """The project called ``name`` (ignoring case), when exactly one is."""

        rows = self._connection.execute(
            "SELECT * FROM projects WHERE name = ? COLLATE NOCASE", (name,),
        ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def all_project_paths(self) -> list[tuple[str, str, str]]:
        """Every folder any project was seen in, with the project's id and name."""

        rows = self._connection.execute(
            "SELECT pp.project_id, p.name, pp.path FROM project_paths pp JOIN projects p ON p.id = pp.project_id",
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def project_paths(self, project_id: str) -> list[str]:
        """The folders a project was seen in, most recently seen first."""

        rows = self._connection.execute(
            "SELECT path FROM project_paths WHERE project_id = ? ORDER BY last_seen_at DESC", (project_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def system_memory_counts(self) -> dict[str | None, int]:
        rows = self._connection.execute(
            "SELECT n.system_id, COUNT(*) FROM record_notes n JOIN records r ON r.id = n.record_id "
            "WHERE r.status = 'active' GROUP BY n.system_id",
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    # --- Question stages and agent tasks ---

    def question_stage(self, question_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM question_stages WHERE question_id = ?", (question_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "labels": json.loads(row["labels"]), "findings": json.loads(row["findings"])}

    def set_question_stage(
        self, question_id: str, *, stage: str, now: str, plain: str | None = None,
        labels: dict[str, str] | None = None, reason: str | None = None, finding: str | None = None,
    ) -> None:
        """Move a question to a stage, keeping earlier plain wording and findings unless new ones are given."""

        current = self.question_stage(question_id) or {"plain": None, "labels": {}, "reason": None, "findings": []}
        findings = current["findings"] + ([finding] if finding else [])
        self._connection.execute(
            "INSERT INTO question_stages (question_id, stage, plain, labels, reason, findings, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (question_id) DO UPDATE SET stage = excluded.stage, "
            "plain = excluded.plain, labels = excluded.labels, reason = excluded.reason, "
            "findings = excluded.findings, updated_at = excluded.updated_at",
            (question_id, stage, plain if plain is not None else current["plain"],
             json.dumps(labels if labels is not None else current["labels"]),
             reason if reason is not None else current["reason"], json.dumps(findings[-10:]), now),
        )

    def add_task(
        self, *, task_id: str, kind: str, prompt: str, now: str, question_id: str | None = None,
        system_id: str | None = None, project_id: str | None = None, facet: str | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO agent_tasks (id, kind, question_id, system_id, project_id, facet, prompt, status, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (task_id, kind, question_id, system_id, project_id, facet, prompt, now, now),
        )

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self, *, status: str | None = "open", kind: str | None = None, question_id: str | None = None,
              system_id: str | None = None) -> list[dict[str, Any]]:
        clauses, parameters = ["1 = 1"], []
        for column, value in (("status", status), ("kind", kind), ("question_id", question_id),
                              ("system_id", system_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        rows = self._connection.execute(
            f"SELECT * FROM agent_tasks WHERE {' AND '.join(clauses)} ORDER BY created_at, id", parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def update_task(self, task_id: str, *, now: str, status: str | None = None, offered: bool = False,
                    result: str | None = None) -> None:
        self._connection.execute(
            "UPDATE agent_tasks SET status = COALESCE(?, status), offered = offered + ?, "
            "result = COALESCE(?, result), updated_at = ? WHERE id = ?",
            (status, 1 if offered else 0, result, now, task_id),
        )

    def open_question_record_ids(self) -> set[str]:
        ids: set[str] = set()
        for row in self._connection.execute("SELECT record_ids FROM questions WHERE status = 'open'").fetchall():
            ids.update(json.loads(row[0]))
        return ids

    def review(self, fingerprint: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT scope_key, reviewed_at, outcome, failures FROM reviews WHERE fingerprint = ?", (fingerprint,),
        ).fetchone()
        return dict(row) if row else None

    def last_review(self, scope_key: str | None = None) -> str | None:
        """When a group (in one scope, or anywhere) was last reviewed successfully."""

        sql = "SELECT MAX(reviewed_at) FROM reviews WHERE outcome = 'reviewed'"
        parameters: tuple[Any, ...] = ()
        if scope_key is not None:
            sql += " AND scope_key = ?"
            parameters = (scope_key,)
        return self._connection.execute(sql, parameters).fetchone()[0]

    def record_review(self, fingerprint: str, *, scope_key: str, outcome: str, now: str) -> None:
        """Remember a group's review; a failure counts toward giving up on that exact group."""

        self._connection.execute(
            "INSERT INTO reviews (fingerprint, scope_key, reviewed_at, outcome, failures) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (fingerprint) DO UPDATE SET reviewed_at = excluded.reviewed_at, "
            "outcome = excluded.outcome, failures = reviews.failures + excluded.failures",
            (fingerprint, scope_key, now, outcome, 1 if outcome == "failed" else 0),
        )

    def prune_reviews(self, *, before: str) -> None:
        self._connection.execute("DELETE FROM reviews WHERE reviewed_at < ?", (before,))

    def count_changed_by(self, reason_prefix: str, *, since: str) -> int:
        """Memories retired or superseded since ``since`` with a reason starting with ``reason_prefix``."""

        return int(self._connection.execute(
            "SELECT COUNT(*) FROM records WHERE status != 'active' AND retired_reason LIKE ? AND updated_at >= ?",
            (reason_prefix.replace("%", "") + "%", since),
        ).fetchone()[0])

    def count_user_questions(self, *, overdue_before: str) -> int:
        """Open questions waiting for the user: handed over, or not reviewed since ``overdue_before``."""

        return int(self._connection.execute(
            "SELECT COUNT(*) FROM questions q LEFT JOIN question_stages s ON s.question_id = q.id "
            "WHERE q.status = 'open' AND (s.stage = 'ask_user' "
            "OR (COALESCE(s.stage, 'review') = 'review' AND q.created_at < ?))",
            (overdue_before,),
        ).fetchone()[0])

    def count_open_questions(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM questions WHERE status = 'open'").fetchone()[0])

    def get_meta(self, key: str) -> str | None:
        row = self._connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def set_verification(self, record_id: str, verification: str, *, now: str) -> None:
        self._connection.execute(
            "UPDATE records SET verification = ?, confirmed_at = ?, updated_at = ? WHERE id = ?",
            (verification, now, now, record_id),
        )

    def log_usage(
        self,
        *,
        at: str,
        operation: str,
        agent: str | None,
        project_id: str | None,
        query: str | None,
        record_ids: Sequence[str],
    ) -> None:
        """Record one recall or briefing and count each memory it returned."""

        with self.transaction():
            self._connection.execute(
                "INSERT INTO usage (at, operation, agent, project_id, query, result_count, result_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (at, operation, agent, project_id, query, len(record_ids), json.dumps(list(record_ids))),
            )
            if record_ids:
                self._connection.execute(
                    f"UPDATE records SET recall_count = recall_count + 1, last_used_at = ? "
                    f"WHERE id IN ({', '.join('?' * len(record_ids))})",
                    (at, *record_ids),
                )

    def add_event(
        self,
        *,
        at: str,
        kind: str,
        summary: str,
        outcome: str | None = None,
        agent: str | None = None,
        project_id: str | None = None,
        session: str | None = None,
        run_id: str | None = None,
        record_ids: Sequence[str] = (),
        details: dict[str, Any] | None = None,
    ) -> None:
        """Add one entry to the activity journal (see ``journal``, which cleans it first)."""

        self._connection.execute(
            "INSERT INTO events (at, kind, outcome, agent, project_id, session, run_id, summary, record_ids, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at, kind, outcome, agent, project_id, session, run_id, summary,
             json.dumps(list(record_ids)), json.dumps(details or {}, ensure_ascii=False)),
        )

    def events(
        self,
        *,
        kinds: Sequence[str] = (),
        outcome: str | None = None,
        run_id: str | None = None,
        record_id: str | None = None,
        since: str | None = None,
        before: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Journal entries, newest first; ``before`` is a ``seq`` for paging back."""

        clauses: list[str] = []
        parameters: list[Any] = []
        if kinds:
            clauses.append(f"e.kind IN ({', '.join('?' * len(kinds))})")
            parameters.extend(kinds)
        for column, value in (("e.outcome", outcome), ("e.run_id", run_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if record_id is not None:
            clauses.append("e.record_ids LIKE ?")
            parameters.append(f'%"{record_id}"%')
        if since is not None:
            clauses.append("e.at >= ?")
            parameters.append(since)
        if before is not None:
            clauses.append("e.seq < ?")
            parameters.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT e.seq, e.at, e.kind, e.outcome, e.agent, e.project_id, p.name AS project_name, e.session, "
            f"e.run_id, e.summary, e.record_ids, e.details FROM events e LEFT JOIN projects p ON p.id = e.project_id "
            f"{where} ORDER BY e.seq DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [
            {**dict(row), "record_ids": json.loads(row["record_ids"]), "details": json.loads(row["details"])}
            for row in rows
        ]

    def event_counts(self, *, since: str, kinds: Sequence[str] = ()) -> dict[tuple[str, str | None], int]:
        """How many journal entries of each kind and outcome there are since ``since``."""

        sql = "SELECT kind, outcome, COUNT(*) FROM events WHERE at >= ?"
        parameters: list[Any] = [since]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
            parameters.extend(kinds)
        rows = self._connection.execute(sql + " GROUP BY kind, outcome", parameters).fetchall()
        return {(row[0], row[1]): int(row[2]) for row in rows}

    def candidate_summary(self, *, since: str) -> dict[str, dict[str, int]]:
        """What happened to learning candidates since ``since``: counts by outcome and by rejection reason."""

        outcomes = {
            str(row[0]): int(row[1]) for row in self._connection.execute(
                "SELECT outcome, COUNT(*) FROM events WHERE kind = 'candidate' AND at >= ? GROUP BY outcome", (since,),
            ).fetchall()
        }
        reasons = {
            str(row[0] or "unknown"): int(row[1]) for row in self._connection.execute(
                "SELECT json_extract(details, '$.reason'), COUNT(*) FROM events "
                "WHERE kind = 'candidate' AND outcome = 'rejected' AND at >= ? GROUP BY 1 ORDER BY 2 DESC", (since,),
            ).fetchall()
        }
        return {"outcomes": outcomes, "reasons": reasons}

    def prune_events(self, *, before: str) -> int:
        return self._connection.execute("DELETE FROM events WHERE at < ?", (before,)).rowcount

    def browse(
        self,
        *,
        status: str = "active",
        project_id: str | None = None,
        global_only: bool = False,
        kind: str | None = None,
        verification: str | None = None,
        origin: str | None = None,
        unused: bool = False,
        system: str | None = None,
        ids: Sequence[str] | None = None,
        order: str = "recent",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Memories for the app's list, with how often each was used, and the total that match.

        ``origin`` is user, learner, import, or agent; ``ids`` limits the list to
        those memories, in the given order when ``order`` is "ids".
        """

        clauses = ["r.status = 'active'" if status == "active" else "r.status != 'active'"]
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("r.project_id = ?")
            parameters.append(project_id)
        elif global_only:
            clauses.append("r.scope = 'global'")
        for column, value in (("r.kind", kind), ("r.verification", verification)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if origin is not None:
            clauses.append(_ORIGIN_FILTERS[origin])
        if unused:
            clauses.append("r.recall_count = 0")
        if system == "none":
            clauses.append("NOT EXISTS (SELECT 1 FROM record_notes n WHERE n.record_id = r.id AND n.system_id IS NOT NULL)")
        elif system is not None:
            clauses.append("EXISTS (SELECT 1 FROM record_notes n WHERE n.record_id = r.id AND n.system_id = ?)")
            parameters.append(system)
        if ids is not None:
            if not ids:
                return [], 0
            clauses.append(f"r.id IN ({', '.join('?' * len(ids))})")
            parameters.extend(ids)
        where = " AND ".join(clauses)
        total = int(self._connection.execute(f"SELECT COUNT(*) {_FROM} WHERE {where}", parameters).fetchone()[0])
        ordering = _BROWSE_ORDERS.get(order, _BROWSE_ORDERS["recent"])
        rows = self._connection.execute(
            f"SELECT {_COLUMNS}, r.recall_count, r.last_used_at {_FROM} WHERE {where} ORDER BY {ordering} "
            "LIMIT ? OFFSET ?",
            (*parameters, limit if order != "ids" else len(ids or ()), offset if order != "ids" else 0),
        ).fetchall()
        found = [_record_dict(row) for row in rows]
        if order == "ids" and ids is not None:
            position = {record_id: index for index, record_id in enumerate(ids)}
            found.sort(key=lambda item: position.get(item["id"], len(position)))
            found = found[offset:offset + limit]
        return found, total

    def record_details(self, record_id: str) -> dict[str, Any] | None:
        """One memory with its use: the counts behind "used N times", split by briefing and recall."""

        row = self._connection.execute(
            f"SELECT {_COLUMNS}, r.recall_count, r.last_used_at {_FROM} WHERE r.id = ?", (record_id,),
        ).fetchone()
        if row is None:
            return None
        details = _record_dict(row)
        details["uses"] = {
            operation: int(count) for operation, count in self._connection.execute(
                "SELECT operation, COUNT(*) FROM usage WHERE result_ids LIKE ? GROUP BY operation",
                (f'%"{record_id}"%',),
            ).fetchall()
        }
        details["replaced"] = [
            RecordRow.from_row(item).__dict__ for item in self._connection.execute(
                f"SELECT {_COLUMNS} {_FROM} WHERE r.superseded_by = ? ORDER BY r.updated_at DESC", (record_id,),
            ).fetchall()
        ]
        return details

    def project_list(self) -> list[dict[str, Any]]:
        """Every known project with its number of active memories, most memories first."""

        rows = self._connection.execute(
            "SELECT p.id, p.name, p.remote, p.last_seen_at, "
            "(SELECT COUNT(*) FROM records r WHERE r.project_id = p.id AND r.status = 'active') AS memories "
            "FROM projects p ORDER BY memories DESC, p.name COLLATE NOCASE",
        ).fetchall()
        return [dict(row) for row in rows]

    def daily_additions(self, *, since: str, shift_minutes: int) -> list[tuple[str, str, int]]:
        """Memories added per local day since ``since``: (day, origin, count).

        ``shift_minutes`` moves UTC times to the viewer's local time. The origin
        is user, learner, import, or agent.
        """

        rows = self._connection.execute(
            "SELECT date(created_at, ?) AS day, CASE WHEN source_kind = 'user' THEN 'user' "
            "WHEN source_agent = 'learner' THEN 'learner' WHEN source_agent LIKE 'import:%' THEN 'import' "
            "ELSE 'agent' END AS origin, COUNT(*) FROM records WHERE created_at >= ? GROUP BY day, origin",
            (f"{shift_minutes:+d} minutes", since),
        ).fetchall()
        return [(row[0], row[1], int(row[2])) for row in rows]

    def daily_usage(self, *, since: str, shift_minutes: int) -> list[tuple[str, str, int, int]]:
        """Briefings and recalls per local day: (day, operation, count, count that returned something)."""

        rows = self._connection.execute(
            "SELECT date(at, ?) AS day, operation, COUNT(*), SUM(result_count > 0) FROM usage WHERE at >= ? "
            "GROUP BY day, operation",
            (f"{shift_minutes:+d} minutes", since),
        ).fetchall()
        return [(row[0], row[1], int(row[2]), int(row[3] or 0)) for row in rows]

    def usage_counts(self, *, since: str) -> dict[str, dict[str, int]]:
        """Briefings and recalls since ``since``, each with how many returned something."""

        return {
            row[0]: {"count": int(row[1]), "with_results": int(row[2] or 0)}
            for row in self._connection.execute(
                "SELECT operation, COUNT(*), SUM(result_count > 0) FROM usage WHERE at >= ? GROUP BY operation",
                (since,),
            ).fetchall()
        }

    def usage_summary(self, *, since: str) -> dict[str, Any]:
        """How KnowItAll2 has been used since ``since``, for measuring its value."""

        by_operation = self.usage_counts(since=since)
        top = [
            {"id": row[0], "count": int(row[1]), "text": row[2]}
            for row in self._connection.execute(
                "SELECT id, recall_count, text FROM records WHERE status = 'active' AND recall_count > 0 "
                "ORDER BY recall_count DESC, last_used_at DESC LIMIT 5"
            ).fetchall()
        ]
        never = int(self._connection.execute(
            "SELECT COUNT(*) FROM records WHERE status = 'active' AND recall_count = 0",
        ).fetchone()[0])
        sources = {
            str(row[0]): int(row[1])
            for row in self._connection.execute(
                "SELECT CASE WHEN source_kind = 'user' THEN 'user' ELSE COALESCE(source_agent, 'agent') END, COUNT(*) "
                "FROM records WHERE status = 'active' GROUP BY 1"
            ).fetchall()
        }
        return {"operations": by_operation, "top": top, "never_used": never, "sources": sources}

    def stats(self) -> dict[str, Any]:
        def grouped(sql: str) -> dict[str, int]:
            return {row[0]: int(row[1]) for row in self._connection.execute(sql).fetchall()}

        by_status = grouped("SELECT status, COUNT(*) FROM records GROUP BY status")
        return {
            "active": by_status.get("active", 0),
            "superseded": by_status.get("superseded", 0),
            "retired": by_status.get("retired", 0),
            "by_kind": grouped("SELECT kind, COUNT(*) FROM records WHERE status = 'active' GROUP BY kind"),
            "by_verification": grouped(
                "SELECT verification, COUNT(*) FROM records WHERE status = 'active' GROUP BY verification"
            ),
            "projects": int(self._connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]),
        }


_ORIGIN_FILTERS = {
    "user": "r.source_kind = 'user'",
    "learner": "r.source_kind != 'user' AND r.source_agent = 'learner'",
    "import": "r.source_kind != 'user' AND r.source_agent LIKE 'import:%'",
    "agent": "r.source_kind != 'user' AND COALESCE(r.source_agent, '') != 'learner' "
             "AND COALESCE(r.source_agent, '') NOT LIKE 'import:%'",
}
_BROWSE_ORDERS = {
    "recent": "COALESCE(r.confirmed_at, r.updated_at) DESC, r.seq DESC",
    "changed": "r.updated_at DESC, r.seq DESC",
    "used": "r.recall_count DESC, r.last_used_at DESC, r.seq DESC",
    "oldest": "r.created_at ASC, r.seq ASC",
    "ids": "r.seq",
}


def _system_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {**dict(row), "aliases": json.loads(row["aliases"]), "gaps": json.loads(row["gaps"])}


def _record_dict(row: sqlite3.Row) -> dict[str, Any]:
    details = RecordRow.from_row(row).__dict__.copy()
    details["subjects"] = list(details["subjects"])
    details["tags"] = list(details["tags"])
    details["recall_count"] = int(row["recall_count"])
    details["last_used_at"] = row["last_used_at"]
    return details


def _scope_filter(scope: str, project_id: str | None) -> tuple[str | None, tuple[Any, ...]]:
    """SQL for a recall scope; ``None`` means the scope cannot match anything."""

    if scope == "everywhere":
        return "1 = 1", ()
    if scope == "global":
        return "r.scope = 'global'", ()
    if scope == "project":
        if project_id is None:
            return None, ()
        return "r.scope = 'project' AND r.project_id = ?", (project_id,)
    if project_id is None:
        return "r.scope = 'global'", ()
    return "(r.scope = 'global' OR r.project_id = ?)", (project_id,)


def _kind_filter(
    scope: str, project_id: str | None, kinds: Sequence[str], exclude_kinds: Sequence[str],
) -> tuple[str | None, tuple[Any, ...]]:
    where, parameters = _scope_filter(scope, project_id)
    if where is None:
        return None, ()
    if kinds:
        where += f" AND r.kind IN ({', '.join('?' * len(kinds))})"
        parameters += tuple(kinds)
    if exclude_kinds:
        where += f" AND r.kind NOT IN ({', '.join('?' * len(exclude_kinds))})"
        parameters += tuple(exclude_kinds)
    return where, parameters
