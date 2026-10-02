"""Read-only health checks for a KnowItAll2 installation."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

from .agents import AGENT_NAMES, Check, adapter_for, probe_server, server_launch
from .paths import data_home, database_path, in_package_storage
from .store import Store, StoreError


def run_checks(*, agents: Iterable[str] = AGENT_NAMES, user_home: Path | None = None) -> list[Check]:
    launch = server_launch()
    checks = [_python_check(), _fts5_check(), _location_check(), _store_check(), _sharing_check(),
              _engine_check(), probe_server(launch)]
    for name in agents:
        checks.extend(adapter_for(name, user_home=user_home).checks(launch))
    if user_home is None:
        checks.append(_app_check())
    return checks


def _app_check() -> Check:
    """The KnowItAll2 app's shortcut, installed by setup."""

    from .app.shortcut import describe

    try:
        ok, detail, fix = describe()
    except Exception as exc:
        return Check("app shortcut", False, str(exc), "Run: knowitall2 app --shortcut")
    return Check("app shortcut", ok, detail, fix)


def _engine_check() -> Check:
    """Whether learning, when it is on, can find the engine it runs on."""

    from .learning.extractor import find_claude_cli, find_codex_cli
    from .learning.state import load_settings

    settings = load_settings()
    name = {"claude-cli": "Claude Code", "codex-cli": "Codex"}.get(settings.backend, settings.backend)
    if not settings.enabled:
        return Check("learning engine", True, "learning is off")
    found = find_codex_cli() if settings.backend == "codex-cli" else find_claude_cli()
    if found is None:
        return Check("learning engine", False, f"learning is on, but {name} was not found on this computer, so "
                     "nothing new is learned", f"Make sure {name} is installed and opens, then run doctor again.")
    return Check("learning engine", True, f"{name} at {found}")


def _sharing_check() -> Check:
    """Whether memory is kept here only, or shared through a server that answers."""

    from . import connected

    if not connected.is_connected():
        return Check("shared memory", True, "kept on this computer only (not connected to a KnowItAll2 server)")
    healthy, lines = connected.describe_status()
    detail = " ".join(lines)
    if healthy:
        return Check("shared memory", True, detail)
    fix = ("Make a new join code on the server's page (Add an agent) and run: knowitall2 server connect <agent> "
           "--join-code <code>" if "no longer accepts" in detail or "No agent here" in detail
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


def _store_check() -> Check:
    path = database_path()
    if not path.exists():
        return Check("memory store", True, f"not created yet; it will be created at {path} on first use")
    try:
        store = Store.open(path)
    except StoreError as exc:
        return Check("memory store", False, str(exc), f"Move {path} aside and let KnowItAll2 create a new one.")
    try:
        stats = store.stats()
    finally:
        store.close()
    return Check("memory store", True, f"{stats['active']} active memories in {path}")
