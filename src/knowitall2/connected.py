"""A computer connected to a KnowItAll2 server: connecting, and keeping its copy and the server in step.

The computer keeps its own full copy of the shared memory, and every part of
KnowItAll2 reads and writes that copy as it always has. While connected, the
database notes each change to a shared table (``sync``), and a sync:

1. fetches what changed on the server since the last fetch, leaving alone any
   row this computer changed and has not sent yet;
2. sends this computer's changes; when the server keeps a newer version of a
   row (or refuses a change), the computer takes the server's version;
3. sends the briefings and recalls made here, for the server's use counts.

A sync runs in its own detached process (``knowitall2 sync``), started by
:func:`nudge` after saves, at session start, and when the last fetch is a
minute old; and inline before and after learning. One runs at a time; a
nudge during a sync asks it to go round once more. When the server cannot
be reached, nothing waits on it: unsent changes stay noted and go next time.

State lives in ``server/`` in the data home:
- ``connection.json``: the server's address;
- ``keys/<agent>.key``: each connected agent's key, readable only by the
  user, never shown or stored anywhere else;
- ``status.json``: when the server was last reached, and any problem;
- ``sync.lock``, ``sync.again``, ``last-start``: one sync at a time.

Any connected agent's key can send the computer's changes: they share one
copy, and each memory says which agent saved it. Removing one agent on the
server stops it talking to the server; removing all of a computer's agents
cuts the computer off.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import journal, sync
from .files import write_text_atomic
from .learning.state import LockBusy, RunLock
from .paths import data_home, database_path
from .remote import RemoteClient, RemoteError
from .server import API_VERSION
from .store import SYNCED_TABLES, Store, StoreError

PULL_EVERY_SECONDS = 60
SYNC_STALE_SECONDS = 10 * 60
BATCH = 500
MAX_PAGES = 100
ROUNDS = 3
USAGE_CURSOR_KEY = "sync.usage_cursor"
LEASE_NAME = "maintenance"
LEASE_SECONDS = 3 * 60 * 60


class ConnectError(RuntimeError):
    """Connecting could not be done, said in plain words."""


@dataclass(frozen=True)
class Connection:
    address: str
    keys: dict[str, str]

    def key_for(self, agent: str | None) -> tuple[str, str] | None:
        """This agent's key, or another connected agent's on this computer: (agent, key)."""

        if agent and agent in self.keys:
            return agent, self.keys[agent]
        for name in sorted(self.keys):
            return name, self.keys[name]
        return None


def folder() -> Path:
    return data_home() / "server"


def is_connected() -> bool:
    return (folder() / "connection.json").is_file()


def _agent_file(agent: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", agent.lower())[:40] or "agent"


def load() -> Connection | None:
    """This computer's connection, or None when its memory is kept only here."""

    try:
        settings = json.loads((folder() / "connection.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    address = settings.get("address") if isinstance(settings, dict) else None
    if not isinstance(address, str) or not address:
        return None
    keys = {}
    for path in sorted((folder() / "keys").glob("*.key")):
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if key:
            keys[path.stem] = key
    return Connection(address, keys)


def _private(path: Path) -> None:
    if sys.platform != "win32":
        os.chmod(path, 0o600 if path.is_file() else 0o700)


def save_key(agent: str, key: str) -> None:
    keys = folder() / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    _private(folder())
    _private(keys)
    path = keys / f"{_agent_file(agent)}.key"
    write_text_atomic(path, key + "\n")
    _private(path)


def normalize_address(address: str) -> str:
    address = address.strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", address, re.IGNORECASE):
        raise ConnectError("the server's address must start with http:// or https://, such as https://kia.example.lan")
    return address


def plain_http_warning(address: str) -> str | None:
    """A warning when keys and memories would cross a network unencrypted; None when that is not a concern."""

    parts = urlsplit(address)
    if parts.scheme.lower() != "http":
        return None
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return None
    try:
        found = ipaddress.ip_address(host)
    except ValueError:
        return (f"{address} uses plain http://, so this computer's key and memories travel unencrypted. That is "
                "fine on your own network; anywhere else, put the server behind HTTPS.")
    if found.is_private or found.is_loopback or found.is_link_local:
        return None
    return (f"{address} uses plain http:// to a public address, so this computer's key and memories travel "
            "unencrypted. Put the server behind HTTPS.")


def local_memories(store: Store) -> int:
    return int(store.connection.execute("SELECT COUNT(*) FROM records WHERE status = 'active'").fetchone()[0])


def client_for(connection: Connection, agent: str | None, *, timeout: float = 5.0) -> RemoteClient | None:
    found = connection.key_for(agent)
    if found is None:
        return None
    return RemoteClient(connection.address, key=found[1], agent=found[0], timeout=timeout)


# --- Syncing ---


@dataclass
class SyncReport:
    sent: int = 0
    received: int = 0
    refused: int = 0
    uses: int = 0
    skipped: bool = False
    problem: str | None = None
    status: int | None = None
    details: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.skipped:
            return "A sync is already running; it will pick this up."
        if self.problem:
            return f"Could not sync with the KnowItAll2 server: {self.problem}"
        parts = [f"sent {self.sent} change(s)", f"received {self.received}"]
        if self.refused:
            parts.append(f"{self.refused} refused by the server")
        return "Synced with the KnowItAll2 server: " + ", ".join(parts) + "."


class SyncLock(RunLock):
    """One sync at a time on this computer."""

    def __init__(self) -> None:
        super().__init__(folder() / "sync.lock")

    def _stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > SYNC_STALE_SECONDS
        except OSError:
            return True


def pull(store: Store, remote: RemoteClient) -> int:
    """Fetch and apply what changed on the server, leaving rows with unsent changes here alone."""

    received = 0
    for _ in range(MAX_PAGES):
        page = remote.changes(sync.cursor(store), limit=BATCH)
        mine = sync.pending_keys(store)
        items = [item for item in page.get("changes", []) if (item.get("table"), item.get("key")) not in mine]
        received += sum(1 for item in items if _differs(store, item))  # not this computer's own, coming back
        sync.apply_pulled(store, items, cursor=int(page.get("next", 0)))
        if not page.get("more"):
            break
    return received


def _differs(store: Store, item: dict[str, Any]) -> bool:
    """Whether a fetched change would change this computer's copy."""

    table = SYNCED_TABLES.get(item.get("table")) if isinstance(item.get("table"), str) else None
    if table is None or not isinstance(item.get("key"), str):
        return False
    current = sync.read_row(store.connection, table, item["key"])
    if item.get("op") == "delete":
        return current is not None
    row = item.get("row") if isinstance(item.get("row"), dict) else {}
    quiet = {"recall_count", "last_used_at"}
    return current is None or any(current.get(name) != value for name, value in row.items()
                                  if name in table.columns and name not in quiet)


def _fit(operations: list[dict[str, Any]], columns: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Leave out what an older server does not know: columns it lacks, and tables it does not share."""

    if not columns:
        return operations
    fitted = []
    for operation in operations:
        known = columns.get(operation["table"])
        if known is None:
            continue
        if "row" in operation:
            operation = {**operation, "row": {name: value for name, value in operation["row"].items() if name in known}}
        fitted.append(operation)
    return fitted


def push(store: Store, remote: RemoteClient, columns: dict[str, list[str]]) -> tuple[int, int]:
    """Send this computer's changes; returns (accepted, refused)."""

    sent = refused = 0
    waiting: set[str] = set()
    for _ in range(MAX_PAGES):
        operations = [item for item in sync.pending_operations(store, limit=BATCH) if item["op_id"] not in waiting]
        operations = _fit(operations, columns)
        if not operations:
            break
        answer = remote.push(operations)
        settled, server_rows = [], []
        for operation, result in zip(operations, answer.get("results", [])):
            outcome = result.get("result")
            if outcome == "rejected" and result.get("retry"):
                waiting.add(operation["op_id"])  # such as a memory whose project has not arrived yet
                continue
            settled.append(operation)
            if outcome == "rejected":
                refused += 1
                journal.problem("sync", f"the server refused a change to {operation['table']} {operation['key']}: "
                                        f"{result.get('reason')}")
            else:
                sent += 1
            if isinstance(result.get("row"), dict):
                server_rows.append({"table": operation["table"], "key": operation["key"], "op": "upsert",
                                    "row": result["row"], "seq": result.get("seq")})
        sync.acknowledge(store, settled)
        if server_rows:
            mine = sync.pending_keys(store)
            sync.apply_pulled(store, [item for item in server_rows if (item["table"], item["key"]) not in mine])
        if not settled:
            break
    return sent, refused


def push_usage(store: Store, remote: RemoteClient) -> int:
    """Send the briefings and recalls made here since the last time."""

    prefix = sync.source_id(store)
    added = 0
    for _ in range(MAX_PAGES):
        try:
            last = int(store.get_meta(USAGE_CURSOR_KEY) or 0)
        except ValueError:
            last = 0
        rows = store.connection.execute(
            "SELECT seq, at, operation, agent, project_id, query, result_ids FROM usage WHERE seq > ? "
            "ORDER BY seq LIMIT ?", (last, BATCH),
        ).fetchall()
        if not rows:
            break
        uses = [{"op_id": f"{prefix}-u-{row[0]}", "at": row[1], "operation": row[2], "agent": row[3],
                 "project_id": row[4], "query": row[5], "result_ids": json.loads(row[6])[:100]} for row in rows]
        added += int(remote.usage(uses).get("added", 0))
        store.set_meta(USAGE_CURSOR_KEY, str(rows[-1][0]))
    return added


def _again() -> bool:
    """Whether something asked for another round while this sync ran (and forget that it did)."""

    path = folder() / "sync.again"
    if path.exists():
        path.unlink(missing_ok=True)
        return True
    return False


def write_status(report: SyncReport, *, pending: bool) -> None:
    path = folder() / "status.json"
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            status = {}
    except (OSError, ValueError):
        status = {}
    now = journal.utc_now()
    status["last_attempt"] = now
    if report.problem:
        status.update(problem=report.problem, problem_status=report.status)
    else:
        status.update(last_success=now, problem=None, problem_status=None)
    status["unsent"] = pending
    try:
        write_text_atomic(path, json.dumps(status, indent=2) + "\n")
    except OSError:
        pass


def read_status() -> dict[str, Any]:
    try:
        status = json.loads((folder() / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def sync_now(store: Store, *, agent: str | None = None, remote: RemoteClient | None = None) -> SyncReport:
    """One sync with the server, unless another is running; never raises for the server's sake."""

    report = SyncReport()
    connection = load()
    if connection is None:
        report.problem = "this computer is not connected to a KnowItAll2 server"
        return report
    remote = remote or client_for(connection, agent)
    if remote is None:
        report.problem = "no agent on this computer has a key for the server yet"
        return report
    try:
        with SyncLock():
            try:
                columns = remote.hello().get("columns") or {}
                for _ in range(ROUNDS):
                    report.received += pull(store, remote)
                    sent, refused = push(store, remote, columns)
                    report.sent, report.refused = report.sent + sent, report.refused + refused
                    report.uses += push_usage(store, remote)
                    if not _again():
                        break
            except RemoteError as exc:
                report.problem, report.status = str(exc), exc.status
                if exc.status == 401:
                    journal.problem("sync", "the server no longer accepts this computer's key: the agent may "
                                            "have been removed on the server's page")
            except sync.SyncError as exc:
                report.problem = f"the server sent something this computer could not use: {exc}"
                journal.problem("sync", report.problem)
    except LockBusy:
        (folder() / "sync.again").touch()
        report.skipped = True
        return report
    write_status(report, pending=sync.has_pending(store))
    if report.sent or report.received or report.refused:
        journal.record(store, "sync", report.describe(), outcome="problem" if report.problem else "ok",
                       agent=remote.agent, details={"sent": report.sent, "received": report.received,
                                                    "refused": report.refused})
    return report


def nudge(agent: str | None, *, store: Store | None = None, pull: bool = True,
          launcher: Callable[..., object] = subprocess.Popen, now: float | None = None) -> bool:
    """Start a sync in the background when this computer is connected and one is due; never raises.

    Due: this computer has unsent changes, or (``pull``) the last start was
    over a minute ago. Cheap when not connected: one file check.
    """

    try:
        if not is_connected():
            return False
        moment = time.time() if now is None else now
        stamp = folder() / "last-start"
        try:
            pull_due = pull and moment - stamp.stat().st_mtime >= PULL_EVERY_SECONDS
        except OSError:
            pull_due = pull
        if not pull_due and not _has_changes(store):
            return False
        if SyncLock().busy():
            (folder() / "sync.again").touch()
            return False
        from .hooks import _detached_options, _learner_environment

        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        log = open(folder() / "last-sync.log", "w", encoding="utf-8")
        try:
            launcher(
                [sys.executable, "-B", "-m", "knowitall2", "sync", "--quiet",
                 *(["--agent", agent] if agent else [])],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(data_home()),
                env=_learner_environment(), close_fds=True, **_detached_options(),
            )
        finally:
            log.close()
        return True
    except Exception as exc:
        journal.problem("sync", f"a sync could not start: {type(exc).__name__}: {exc}")
        return False


def _has_changes(store: Store | None) -> bool:
    if store is not None:
        return sync.has_pending(store)
    opened = Store.open(database_path())
    try:
        return sync.has_pending(opened)
    finally:
        opened.close()


@contextmanager
def maintenance_turn() -> Iterator[bool]:
    """Whether this computer may tidy up the memory now: always when not connected; when connected,
    only while it holds the server's maintenance lease (and not at all when the server cannot be reached,
    so two computers working offline never both tidy up)."""

    connection = load()
    if connection is None:
        yield True
        return
    remote = client_for(connection, None)
    granted = False
    if remote is not None:
        try:
            granted = bool(remote.lease(LEASE_NAME, seconds=LEASE_SECONDS).get("granted"))
        except RemoteError:
            granted = False
    try:
        yield granted
    finally:
        if granted and remote is not None:
            try:
                remote.lease(LEASE_NAME, release=True)
            except RemoteError:
                pass


def share_quietly(store: Store, *, agent: str | None = None) -> SyncReport | None:
    """A sync for the learner and other background work: nothing when not connected, never raises."""

    if not is_connected():
        return None
    try:
        return sync_now(store, agent=agent)
    except (StoreError, sqlite3.Error, OSError) as exc:
        journal.problem("sync", f"the sync failed: {type(exc).__name__}: {exc}")
        return None


# --- Connecting and disconnecting ---


def connect(
    store: Store, address: str, agent: str, code: str, *, upload: bool, replace: bool = False,
    remote_factory: Callable[..., RemoteClient] = RemoteClient,
) -> dict[str, Any]:
    """Connect one agent on this computer to a server with its join code.

    The first agent connects the computer: changes are noted from now on;
    this computer's memory is sent first when ``upload`` (the server keeps
    one copy of anything it already has), or set aside when ``replace`` (a
    backup of the database is kept beside it); and then the server's memory
    is fetched. A later agent only gets its own key. Nothing changes here
    unless the server accepts the join code.
    """

    address = normalize_address(address)
    existing = load()
    if existing is not None and existing.address != address:
        raise ConnectError(f"this computer is already connected to {existing.address}; disconnect it first")
    remote = remote_factory(address, agent=agent)
    try:
        health = remote.health()
        if health.get("api") != API_VERSION:
            raise ConnectError(
                f"the server speaks version {health.get('api')} of KnowItAll2's sharing and this computer "
                f"version {API_VERSION}; update whichever is older (the server is KnowItAll2 {health.get('version')})"
            )
        joined = remote.join(code)
    except RemoteError as exc:
        raise ConnectError(str(exc)) from None
    save_key(agent, joined["key"])
    report: dict[str, Any] = {"agent": agent, "connection": joined.get("connection"), "first": existing is None,
                              "uploaded": 0, "received": 0}
    if existing is not None:
        return report
    write_text_atomic(folder() / "connection.json",
                      json.dumps({"address": address, "connected_at": journal.utc_now()}, indent=2) + "\n")
    with store.transaction():
        store.connection.execute("DELETE FROM changes")
        store.connection.execute("DELETE FROM sync_seen")
        store.set_meta(sync.CURSOR_KEY, "0")
        last_use = store.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM usage").fetchone()[0]
        store.set_meta(USAGE_CURSOR_KEY, str(last_use))
    if replace and not upload:
        report["backup"] = str(set_aside(store))
    sync.set_tracking(store, True)
    if not upload:
        # The projects this computer already had go too, so memories saved here under them can follow.
        with store.transaction():
            sync.note_all(store, "projects")
    try:
        if upload:
            report["uploaded"] = upload_all(store, remote)
        report["received"] = pull(store, remote)
    except RemoteError as exc:
        report["problem"] = str(exc)
    return report


# What "use only the server's memory" clears here. Projects stay: their folders on this computer belong to them.
_SET_ASIDE = ("record_notes", "question_stages", "agent_tasks", "reviews", "questions", "records", "systems")


def set_aside(store: Store) -> Path:
    """Back the database up beside itself, then clear the shared memory here so the server's takes its place."""

    path = database_path().with_name("knowitall2.pre-server-backup.db")
    target = sqlite3.connect(str(path))
    try:
        store.connection.backup(target)
    finally:
        target.close()
    with store.transaction():
        for name in _SET_ASIDE:
            store.connection.execute(f"DELETE FROM {name}")
    return path


def remove_agent(store: Store, agent: str) -> bool:
    """Forget one agent's key here; when it was the last, the computer keeps its memory here only again.

    Returns True when the computer was disconnected.
    """

    (folder() / "keys" / f"{_agent_file(agent)}.key").unlink(missing_ok=True)
    connection = load()
    if connection is not None and not connection.keys:
        disconnect(store)
        return True
    return False


def describe_status(*, check: bool = True, timeout: float = 3.0) -> tuple[bool, list[str]]:
    """Plain lines about this computer's sharing, and whether all is well; asks the server when ``check``."""

    connection = load()
    if connection is None:
        return True, ["This computer keeps its memory here only; it is not connected to a KnowItAll2 server."]
    lines = [f"Connected to the KnowItAll2 server at {connection.address}."]
    agents = ", ".join(sorted(connection.keys)) or "none"
    lines.append(f"Agents here with a key: {agents}.")
    healthy = bool(connection.keys)
    status = read_status()
    if status.get("last_success"):
        lines.append(f"Last synced: {status['last_success']}.")
    if status.get("unsent"):
        lines.append("Some changes made here have not reached the server yet; they are sent at the next sync.")
    warning = plain_http_warning(connection.address)
    if warning:
        lines.append(f"Note: {warning}")
    if not connection.keys:
        lines.append("No agent here has a key. Connect one with a join code from the server's page.")
    if check and connection.keys:
        remote = client_for(connection, None, timeout=timeout)
        assert remote is not None
        try:
            hello = remote.hello()
            lines.append(f"The server answers: KnowItAll2 {hello.get('version')}, {hello.get('memories')} memories.")
            if hello.get("api") != API_VERSION:
                healthy = False
                lines.append(f"The server speaks version {hello.get('api')} of KnowItAll2's sharing and this computer "
                             f"version {API_VERSION}; update whichever is older.")
        except RemoteError as exc:
            healthy = False
            if exc.status == 401:
                lines.append("The server no longer accepts this computer's key: the agent was removed on the "
                             "server's page. Make a new join code there and run: knowitall2 server connect")
            else:
                lines.append(f"The server cannot be reached right now ({exc}). Memories saved here wait and are "
                             "sent when it is back.")
    elif status.get("problem"):
        lines.append(f"The last sync did not work: {status['problem']}")
    return healthy, lines


def upload_all(store: Store, remote: RemoteClient) -> int:
    """Send every shared row of this computer's memory; returns how many the server accepted."""

    accepted = 0
    batch: list[dict[str, Any]] = []

    def send() -> int:
        answer = remote.push(batch)
        return sum(1 for result in answer.get("results", []) if result.get("result") in {"applied", "duplicate"})

    for operation in sync.full_copy_operations(store):
        batch.append(operation)
        if len(batch) >= BATCH:
            accepted += send()
            batch = []
    if batch:
        accepted += send()
    return accepted


def disconnect(store: Store) -> None:
    """Keep this computer's memory here only again: its copy stays, the keys and noted changes go."""

    sync.set_tracking(store, False)
    with store.transaction():
        store.connection.execute("DELETE FROM changes")
        store.connection.execute("DELETE FROM sync_seen")
        store.connection.execute("DELETE FROM meta WHERE key IN (?, ?)", (sync.CURSOR_KEY, USAGE_CURSOR_KEY))
    keys = folder() / "keys"
    for path in keys.glob("*.key"):
        path.unlink(missing_ok=True)
    for name in ("connection.json", "status.json", "sync.again", "last-start"):
        (folder() / name).unlink(missing_ok=True)


# --- The command ---


def run_command(arguments: argparse.Namespace) -> int:
    """``knowitall2 sync``: sync with the server now."""

    if not is_connected():
        if not arguments.quiet:
            print("This computer keeps its memory here only; it is not connected to a KnowItAll2 server.")
        return 0
    try:
        store = Store.open(database_path())
    except StoreError as exc:
        print(f"knowitall2: {exc}", file=sys.stderr)
        return 1
    try:
        report = sync_now(store, agent=arguments.agent)
    finally:
        store.close()
    print(report.describe())
    return 1 if report.problem and not arguments.quiet else 0
