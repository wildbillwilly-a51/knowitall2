"""Read-only health checks for a KnowItAll2 installation."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

from .agents import AGENT_NAMES, Check, adapter_for, probe_server, server_launch
from .agents.base import cli_command, was_set_up
from .paths import data_home, database_path, in_package_storage
from .secrets import find_secrets
from .store import SCHEMA_VERSION, StoreError, inspect_database
from .update import update_command

# How many memories that look like secrets are named; the rest are counted.
_SECRETS_SHOWN = 20


def run_checks(*, agents: Iterable[str] | None = None, user_home: Path | None = None) -> list[Check]:
    """Every check; the agents named in ``agents`` in full, or else each agent that was set up here."""

    launch = server_launch()
    checks = [_python_check(), _fts5_check(), _location_check(), *_store_checks(), _sharing_check(),
              _engine_check(), probe_server(launch)]
    for name in agents or AGENT_NAMES:
        adapter = adapter_for(name, user_home=user_home)
        if agents is None and adapter.installed() and not was_set_up(adapter):
            checks.append(Check(adapter.display_name, True, "not set up; skipped"))
            continue
        checks.extend(adapter.checks(launch))
    if user_home is None:
        checks.append(_app_check())
    return checks


def _app_check() -> Check:
    """The KnowItAll2 app's shortcut, installed by setup."""

    from .app.shortcut import describe

    try:
        ok, detail, fix = describe()
    except Exception as exc:
        return Check("app shortcut", False, str(exc), f"Run: {cli_command('app', '--shortcut')}")
    return Check("app shortcut", ok, detail, fix)


def _engine_check() -> Check:
    """Whether learning, when it is on, can find the engine it runs on."""

    from .learning.extractor import find_claude_cli, find_codex_cli
    from .learning.runner import describe_engine
    from .learning.state import load_settings

    settings = load_settings()
    name = {"claude-cli": "Claude Code", "codex-cli": "Codex"}.get(settings.backend, settings.backend)
    if not settings.enabled:
        return Check("learning engine", True, "learning is off")
    found = find_codex_cli() if settings.backend == "codex-cli" else find_claude_cli()
    if found is None:
        return Check("learning engine", False, f"learning is on, but {name} was not found on this computer, so "
                     "nothing new is learned", f"Make sure {name} is installed and opens, then run doctor again.")
    return Check("learning engine", True, f"{name} at {describe_engine(found)}")


def _sharing_check() -> Check:
    """Whether memory is kept here only, or shared through a server that answers."""

    from . import connected

    if not connected.is_connected():
        return Check("shared memory", True, "kept on this computer only (not connected to a KnowItAll2 server)")
    healthy, lines = connected.describe_status()
    detail = " ".join(lines)
    if healthy:
        return Check("shared memory", True, detail)
    fix = ("Make a new join code on the server's page (Add an agent) and run: "
           + cli_command("server", "connect", "<agent>", "--join-code", "<code>")
           if "no longer accepts" in detail or "No agent here" in detail
           else "Check that the KnowItAll2 server is running and reachable; memories saved here are sent when it "
                "is back.")
    return Check("shared memory", False, detail, fix)


def _python_check() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < (3, 12):
        return Check("Python", False, f"{version} at {sys.executable}", "Install Python 3.12 or later.")
    return Check("Python", True, f"{version} at {sys.executable}")


def _fts5_check() -> Check:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5 (text)")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return Check("SQLite full-text search", False, str(exc), "Use a Python build whose SQLite includes FTS5.")
    return Check("SQLite full-text search", True, f"FTS5 available (SQLite {sqlite3.sqlite_version})")


def _location_check() -> Check:
    """Fail when data or code sits in an app's private package storage on Windows."""

    code_root = Path(__file__).resolve().parents[1]
    trapped = [
        f"{label} ({os.path.realpath(path)})"
        for label, path in (("memories", data_home()), ("KnowItAll2 code", code_root))
        if in_package_storage(path)
    ]
    if trapped:
        return Check(
            "storage location", False,
            "inside a Windows app's private package storage: " + "; ".join(trapped)
            + ". Other agents cannot see it, and it is deleted if that app is uninstalled.",
            "Reinstall KnowItAll2 into %USERPROFILE%\\.knowitall2 by following docs/install-for-agents.md.",
        )
    return Check("storage location", True, f"memories in {data_home()}; code in {code_root}")


def _store_checks() -> list[Check]:
    """The memory store, looked at read-only (doctor never upgrades, repairs or writes it), and whether
    any memory in it now looks like a secret."""

    path = database_path()
    if not path.exists():
        return [Check("memory store", True, f"not created yet; it will be created at {path} on first use")]
    try:
        found = inspect_database(path)
    except StoreError as exc:
        return [Check("memory store", False, str(exc), _store_fix(exc.reason, path))]
    version = found["schema_version"]
    later = f" (schema {version}; KnowItAll2 upgrades it the next time it opens it)" if (
        version is not None and version < SCHEMA_VERSION) else ""
    return [Check("memory store", True, f"{found['active']} active memories in {path}{later}"),
            _secrets_check(found["memories"])]


def _store_fix(reason: str, path: Path) -> str:
    """What to do about a store that cannot be used; only an unreadable one is set aside."""

    if reason == "newer_schema":
        return ("The memories are safe. This copy of KnowItAll2 is older than the one that last opened them: update it "
                f"(run: {update_command()}), or run doctor with the KnowItAll2 your agents use.")
    if reason == "no_fts5":
        return "Use a Python build whose SQLite includes FTS5 full-text search (python.org builds do), then set up again."
    if reason == "busy":
        return "Another KnowItAll2 process is using the memory store; wait a moment, then run doctor again."
    if reason == "unreadable":
        return (f"Move {path} aside and let KnowItAll2 create a new one. If this computer shares memory through a "
                "KnowItAll2 server, the server's copy comes back with the next sync; otherwise a backup beside it "
                f"({path.stem}.*backup*.db, made before a schema upgrade or connecting to a server) can take its place.")
    return (f"Check that you can read and write {path} and its folder (permissions, free disk space, or another "
            "program holding it), then run doctor again.")


def _secrets_check(memories: list[tuple[str, str]]) -> Check:
    """Memories saved before the secret screening knew their shape, by id and kind only.

    ``memories`` are (id, the text ``remember`` screens). A note, not a
    failure, and nothing is changed: only the user can tell whether one
    really holds a secret.
    """

    held = []
    for record_id, screened in memories:
        findings = find_secrets(screened)
        if findings:
            held.append(f"{record_id} ({', '.join(sorted({finding.kind for finding in findings}))})")
    if not held:
        return Check("secrets in memories", True, "no active memory looks like it holds a secret")
    shown = "; ".join(held[:_SECRETS_SHOWN])
    if len(held) > _SECRETS_SHOWN:
        shown += f"; and {len(held) - _SECRETS_SHOWN} more"
    count = ("1 active memory looks like it" if len(held) == 1
             else f"{len(held)} active memories look like they")
    return Check(
        "secrets in memories", True, f"{count} may hold a secret: {shown}",
        "Look at each one in the KnowItAll2 app and forget any that holds a secret (knowitall2 forget <id>), "
        "then save where the secret is kept instead. doctor changes nothing itself.",
    )
