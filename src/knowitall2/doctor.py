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
from .store import SCHEMA_VERSION, StoreError, inspect_database, inspect_maintenance
from .update import update_command

# How many memories that look like secrets are named; the rest are counted.
_SECRETS_SHOWN = 20
# Maintenance without a model call for this long, while groups of memories wait for it, is worth a note.
MAINTENANCE_STALLED_DAYS = 3


def run_checks(*, agents: Iterable[str] | None = None, user_home: Path | None = None) -> list[Check]:
    """Every check; the agents named in ``agents`` in full, or else each agent that was set up here."""

    launch = server_launch()
    checks = [_python_check(), _fts5_check(), _location_check(), *_store_checks(), _sharing_check(),
              _engine_check(), _maintenance_check(), _known_files_check(), *_refused_folder_checks(),
              *_cloud_folder_checks(),
              probe_server(launch)]
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


def _maintenance_check() -> Check:
    """Whether maintenance, when learning is on, still tidies memories while groups of them wait for it.

    Read from the journal's learning runs: on 2026-10-11 maintenance had made no call in 13 days while 181
    groups waited, and nothing said so.
    """

    from datetime import datetime, timedelta, timezone

    from .learning.state import load_settings

    name = "maintenance"
    if not load_settings().enabled:
        return Check(name, True, "learning is off, and tidying up memories with it")
    path = database_path()
    if not path.exists():
        return Check(name, True, "nothing to tidy up yet")
    try:
        found = inspect_maintenance(path)
    except StoreError as exc:
        return Check(name, True, f"could not read the journal: {exc}")
    last, waiting = found["last_call"], found["waiting"]
    when = last[:16].replace("T", " ") + " UTC" if last else "never"
    since = (datetime.now(timezone.utc) - timedelta(days=MAINTENANCE_STALLED_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not waiting or (last or "") >= since or (found["first_run"] or since) >= since:
        detail = f"last tidied up memories {when}" + (f"; {waiting} groups wait for it" if waiting else "")
        return Check(name, True, detail)
    return Check(
        name, True,
        f"tidying up memories (merging duplicates, settling outdated ones) has made no model call since {when}, "
        f"and {waiting} groups of memories wait for it",
        f"Run: {cli_command('maintain')} to tidy up now; each run reviews a few groups, so run it again until "
        "none wait.",
    )


def _sharing_check() -> Check:
    """Whether memory is kept here only, or shared through a server that answers."""

    from . import connected

    if not connected.is_connected():
        return Check("shared memory", True, "kept on this computer only (not connected to a KnowItAll2 server)")
    healthy, lines = connected.describe_status()
    detail = " ".join(lines)
    if healthy:
        return Check("shared memory", True, detail)
    if "no longer accepts" in detail or "No agent here" in detail:
        fix = ("Make a new join code on the server's page (Add an agent) and run: "
               + cli_command("server", "connect", "<agent>", "--join-code", "<code>"))
    elif "rejected change" in detail:
        fix = "Review them with: " + cli_command("sync", "--rejections")
    elif "cannot be reached" in detail:
        fix = "Check that the KnowItAll2 server is running and reachable; memories saved here are sent when it is back."
    else:
        fix = "Run " + cli_command("sync") + " to see the error the last sync met."
    return Check("shared memory", False, detail, fix)


def _known_files_check() -> Check:
    """The knowledge files in projects: on or off, where written, and any project where search may not see them."""

    from . import known

    name = "knowledge files"
    if not known.enabled():
        return Check(name, True, "off; `knowitall2 known --on` writes them into each project again")
    status = known.read_status()
    if status.get("failed"):
        return Check(name, True, f"the last refresh failed ({status.get('failed_at')}): {status['failed']}; the "
                                 "files are written again after the next change to memories",
                     f"Run: {cli_command('known')} to try now and see the error")
    if not status.get("at"):
        return Check(name, True, "not written yet; they are written after the next change to memories",
                     f"Run: {cli_command('known')} to write them now")
    folders = status.get("folders") or []
    detail = f"in {len(folders)} project folders ({known.FOLDER}/), last written {status['at']}"
    problems = [*status.get("problems", []), *status.get("notes", [])]
    if problems:
        return Check(name, True, detail + "; " + "; ".join(problems[:3]) +
                     (f"; and {len(problems) - 3} more" if len(problems) > 3 else ""),
                     f"See the notes above; then run: {cli_command('known')}")
    return Check(name, True, detail)


def _refused_folder_checks() -> list[Check]:
    """A note for each project folder the knowledge files should be in but the last refresh refused, with
    why and what to do: six projects kept stale files for days, and nothing said so (release-candidate
    review, R-M3)."""

    from . import known

    if not known.enabled():
        return []
    refused = known.read_status().get("refused")
    checks = []
    for item in refused if isinstance(refused, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("folder"), str):
            continue
        root = item["folder"]
        folder = str(Path(root) / known.FOLDER)
        why, fix = {
            "link": (f"{folder} is a link, or holds one, and KnowItAll2 never writes through a link",
                     f"Delete the link (what it points to is kept), then run: {cli_command('known')}"),
            "tracked": (f"Git tracks {known.FOLDER}/ there, so it is the repository's own",
                        f"If those files are KnowItAll2's, untrack them (git -C \"{root}\" rm -r --cached "
                        f"{known.FOLDER}) and commit, then run: {cli_command('known')}"),
            "git": (f"Git could not be run there to check that it does not track {known.FOLDER}/",
                    f"Check that Git works there (run: git -C \"{root}\" status), then run: {cli_command('known')}"),
        }.get(item.get("reason"), (
            f"{folder} was not written by KnowItAll2, or its {known.INDEX} is empty or damaged",
            f"If it holds only KnowItAll2's old files, delete that folder, then run: {cli_command('known')}"))
        checks.append(Check("knowledge files", True, f"not written in {root}: {why}", fix))
    held = known.read_status().get("held")
    for item in held if isinstance(held, list) else []:
        # A synced folder has its own note (``_cloud_folder_checks``); a package or image says what to add.
        if isinstance(item, dict) and isinstance(item.get("folder"), str) and "cloud" not in str(item.get("reason")):
            why, _, fix = str(item.get("reason")).partition("; ")
            checks.append(Check("knowledge files", True, f"not written in {item['folder']}: {why}",
                                (fix[:1].upper() + fix[1:] + ".") if fix else None))
    return checks


def _cloud_folder_checks(*, home: Path | None = None) -> list[Check]:
    """A note naming the project folders agents work in that OneDrive, Dropbox, Google Drive, or iCloud keeps
    in sync, so they can be moved; nothing when there are none."""

    from . import known

    synced = [place.rstrip("\\/") for place in known.synced_places(home)]
    found = []
    for folder in sorted(known.worked_folders()):
        if any(folder == place or folder.startswith(place + os.sep) for place in synced):
            try:
                found.append(str(Path(folder).resolve(strict=True)))
            except OSError:
                continue   # a folder since deleted or moved is no longer anywhere (seen 2026-10-12)
    if not found:
        return []
    written = "KnowItAll2 writes no knowledge files there" if not known._synced_allowed() else (
        "knowledge files are written there, as config.json allows")
    return [Check("project folders", True,
                  f"{len(found)} project folder{'s are' if len(found) > 1 else ' is'} in a folder that a cloud "
                  f"service keeps in sync ({written}): {'; '.join(found)}",
                  f"Move {'these projects' if len(found) > 1 else 'this project'} to a folder on this computer that "
                  "is not synced, and open it from there.")]


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
