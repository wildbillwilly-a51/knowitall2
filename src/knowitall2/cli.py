"""The ``knowitall2`` command."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from . import __version__, journal, review
from .agents import AGENT_NAMES, AgentError, adapter_for, describe_checks, server_launch
from .journal import EVENT_KINDS
from .memory import KINDS, RECALL_SCOPES, SCOPES, SOURCES, Memory, MemoryInputError
from .paths import data_home, database_path
from .store import Store, StoreError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowitall2", description="KnowItAll2: an always-on memory for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"knowitall2 {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("serve", help="run the MCP server on stdio (started by coding agents)")
    commands.add_parser("serve-once", help=argparse.SUPPRESS)

    serve_http = commands.add_parser(
        "serve-http", help="run a KnowItAll2 server that agents on several computers share (usually in Docker)",
    )
    serve_http.add_argument("--host", default=os.environ.get("KNOWITALL2_SERVER_HOST", "127.0.0.1"),
                            help="the address to listen on (default 127.0.0.1; 0.0.0.0 in the container)")
    serve_http.add_argument("--port", type=int, default=int(os.environ.get("KNOWITALL2_SERVER_PORT", "4191")))
    join_code = commands.add_parser("server-join-code", help=argparse.SUPPRESS)
    join_code.add_argument("name")
    syncing = commands.add_parser("sync", help="send and fetch shared memories now (when connected to a server)")
    syncing.add_argument("--agent", help="the agent whose key to use (default: any connected agent here)")
    syncing.add_argument("--quiet", action="store_true", help="say nothing unless something went wrong")

    remember = commands.add_parser("remember", help="save a memory")
    remember.add_argument("text")
    remember.add_argument("--kind", choices=KINDS, default="fact")
    remember.add_argument("--subject", action="append", default=[], dest="subjects", help="repeatable")
    remember.add_argument("--tag", action="append", default=[], dest="tags", help="repeatable")
    remember.add_argument("--scope", choices=SCOPES)
    remember.add_argument("--source", choices=SOURCES, default="inferred",
                          help="user only for the user's own words (default inferred; agents run this command too)")
    remember.add_argument("--replaces", metavar="ID")
    remember.add_argument("--project-path")

    recall = commands.add_parser("recall", help="search memories by keywords")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int)
    recall.add_argument("--scope", choices=RECALL_SCOPES, default="all")
    recall.add_argument("--project-path")

    forget = commands.add_parser("forget", help="retire a memory by id")
    forget.add_argument("id")
    forget.add_argument("--reason")

    move = commands.add_parser("move", help="file project memories under another project (one KnowItAll2 knows)")
    move.add_argument("ids", nargs="+", metavar="id")
    move.add_argument("--project", required=True, help="the project's name, as the app shows it")
    move.add_argument("--system", help="also file them under this system in the catalog (its name)")

    restore = commands.add_parser("restore", help="bring back a retired or replaced memory")
    restore.add_argument("id")
    history = commands.add_parser("history", help="show recently retired or replaced memories")
    history.add_argument("--limit", type=int, default=20)

    importing = commands.add_parser("import", help="import memories from a JSON Lines file (docs/import-format.md)")
    importing.add_argument("file", type=Path)
    importing.add_argument("--dry-run", action="store_true", help="report what would happen; save nothing")
    importing.add_argument("--verbose", action="store_true", help="list every line's outcome")
    importing.add_argument("--label", default="import", help="where the memories came from, shown in provenance")

    brief = commands.add_parser("brief", help="show the session-start briefing")
    brief.add_argument("--project-path")

    commands.add_parser("stats", help="show memory counts and where data is stored")

    activity = commands.add_parser("activity", help="show what KnowItAll2 did recently, newest first")
    activity.add_argument("--limit", type=int, default=30)
    activity.add_argument("--kind", action="append", default=[], dest="kinds", choices=EVENT_KINDS,
                          help="repeatable; for example learning, candidate, recall")
    activity.add_argument("--run", help="only the entries of one learning run, such as r-1a2b3c4d")
    activity.add_argument("--problems", action="store_true", help="show recorded problems instead")

    commands.add_parser("questions", help="show KnowItAll2's open questions for you")
    answer = commands.add_parser("answer", help="answer one of KnowItAll2's questions")
    answer.add_argument("id", help="the question id, for example q-1a2b3c4d")
    answer.add_argument("choice", help="the key of the option you choose")

    user_home_help = "use this folder instead of the user's home (for testing)"
    setup = commands.add_parser("setup", help="register KnowItAll2 with a coding agent, and install the app's shortcut")
    setup.add_argument("agent", choices=AGENT_NAMES)
    setup.add_argument("--user-home", type=Path, help=user_home_help)
    setup.add_argument("--no-app-shortcut", action="store_true", help="do not add the KnowItAll2 app shortcut")
    _add_server_options(setup, required_code=False)
    uninstall = commands.add_parser("uninstall", help="remove KnowItAll2 from a coding agent (memories are kept)")
    uninstall.add_argument("agent", choices=AGENT_NAMES)
    uninstall.add_argument("--user-home", type=Path, help=user_home_help)
    server = commands.add_parser(
        "server", help="this computer's connection to a KnowItAll2 server: status, connect, disconnect",
    )
    server_actions = server.add_subparsers(dest="server_action", required=True, metavar="action")
    server_actions.add_parser("status", help="show the connection and whether the server answers")
    connecting = server_actions.add_parser("connect", help="connect an agent already set up here to a server")
    connecting.add_argument("agent", choices=AGENT_NAMES)
    _add_server_options(connecting, required_code=True)
    server_actions.add_parser("disconnect", help="keep memory on this computer only again (its copy stays)")
    doctor = commands.add_parser("doctor", help="check the installation and each agent's registration")
    doctor.add_argument("--agent", choices=AGENT_NAMES, action="append", dest="agents")
    doctor.add_argument("--user-home", type=Path, help=user_home_help)

    learn = commands.add_parser("learn", help="learn memories from finished Claude Code and Codex sessions")
    mode = learn.add_mutually_exclusive_group()
    mode.add_argument(
        "--start-from-now", choices=AGENT_NAMES, metavar="AGENT",
        help="treat an agent's existing sessions as already read, so only new activity is learned",
    )
    mode.add_argument("--dry-run", action="store_true", help="show what would be learned; no model calls, nothing saved")
    mode.add_argument("--show", metavar="SESSION", help="print the redacted dossier(s) for a session id or prefix")
    mode.add_argument("--enable", action="store_true", help="turn learning on (the user's consent)")
    mode.add_argument("--disable", action="store_true", help="turn learning off")
    mode.add_argument("--status", action="store_true", help="show learning settings and progress")
    mode.add_argument("--requests", action="store_true",
                      help="learn only the sessions agents asked to learn now (the agent hooks use this)")
    mode.add_argument(
        "--catch-up", nargs="?", const="", metavar="FOLDER",
        help="learn from past sessions in FOLDER that were skipped as already covered; without FOLDER, list them",
    )
    mode.add_argument(
        "--documents", nargs="?", const="", metavar="PROJECT",
        help="learn from what projects keep in writing (state files, summaries, handoffs, agents' notes); "
             "PROJECT is a name or folder, or every project without one",
    )
    learn.add_argument("--since", metavar="YYYY-MM-DD", help="with --catch-up: only sessions active since this date")
    learn.add_argument("--ignore-daily-limit", action="store_true",
                       help="for this one run, learn waiting sessions without the daily limit (the user's choice)")
    learn.add_argument("--estimate", action="store_true",
                       help="with --catch-up FOLDER or --documents: only show how many model calls it would take")
    learn.add_argument("--max-calls", type=int, metavar="N",
                       help="with --catch-up FOLDER: the newest sessions only, up to about N model calls; "
                            "with --documents: at most N calls per project")
    learn.add_argument(
        "--backend", choices=("claude-cli", "codex-cli"),
        help="with --enable: the engine that runs learning calls (default: the current one if installed, "
             "else whichever is installed)",
    )
    learn.add_argument("--model", help="model used for extraction (default: sonnet for Claude Code; Codex's everyday model)")

    maintain = commands.add_parser(
        "maintain", help="review existing memories for duplicates and outdated facts (also runs after learning)",
    )
    maintain.add_argument("--dry-run", action="store_true", help="show the groups and which would be reviewed")
    maintain.add_argument("--model", help="model used for the review (default: the learning model)")

    cataloguing = commands.add_parser(
        "catalog", help="file memories under systems and review questions (also runs after learning)",
    )
    cataloguing.add_argument("--dry-run", action="store_true", help="show what would be done; no model calls")
    cataloguing.add_argument(
        "--commands", type=int, nargs="?", const=40, metavar="CALLS",
        help="note which command each lesson is about, so agents see it just before running that command; "
             "at most CALLS model calls (default 40), each for 50 memories",
    )

    finding = commands.add_parser(
        "find-out", help="have an agent look up a system's missing parts in its project folder now (read-only)",
    )
    finding.add_argument("system", help="the system's id, such as sys-1a2b3c4d5e6f")

    app = commands.add_parser("app", help="open the KnowItAll2 app window (it stops when the window closes)")
    app.add_argument("--no-window", action="store_true", help="only print the address to open in a browser")
    app.add_argument("--port", type=int, default=0, help="a fixed local port (default: any free port)")
    shortcut = app.add_mutually_exclusive_group()
    shortcut.add_argument("--shortcut", action="store_true",
                          help="add a KnowItAll2 shortcut to the Start menu (Windows) or applications menu (Linux)")
    shortcut.add_argument("--remove-shortcut", action="store_true", help="remove the shortcut again")
    app.add_argument("--desktop", action="store_true", help="with --shortcut on Windows: also put one on the desktop")

    update = commands.add_parser("update", help="download the latest KnowItAll2 and refresh every agent's setup")
    update.add_argument("--finish", action="store_true", help=argparse.SUPPRESS)
    update.add_argument("--previous", default=__version__, help=argparse.SUPPRESS)
    update.add_argument("--unchanged", action="store_true", help=argparse.SUPPRESS)

    known = commands.add_parser(
        "known", help="write what KnowItAll2 knows as files in each project (knowitall2-known/) now",
    )
    switch = known.add_mutually_exclusive_group()
    switch.add_argument("--off", action="store_true", help="take the files out of every project and keep them out")
    switch.add_argument("--on", action="store_true", help="write the files again after --off")
    known.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)

    commands.add_parser("version", help="print the version")
    return parser


def _add_server_options(parser: argparse.ArgumentParser, *, required_code: bool) -> None:
    parser.add_argument("--server", metavar="ADDRESS",
                        help="the KnowItAll2 server's address, such as https://kia.example.lan (needed for the "
                             "first agent on this computer)")
    parser.add_argument("--join-code", metavar="CODE", required=required_code,
                        help="this agent's one-time code from the server's page (Add an agent)")
    existing = parser.add_mutually_exclusive_group()
    existing.add_argument("--send-memories", action="store_true",
                          help="add this computer's memories to the server's shared memory")
    existing.add_argument("--replace-memories", action="store_true",
                          help="set this computer's memories aside (a backup is kept) and use only the server's")


def main(argv: Sequence[str] | None = None) -> int:
    # Printed text can hold any character; where the console code page lacks one, escape it instead of failing.
    # A pipe or file (an agent reading the output) gets UTF-8: on Windows it would get the ANSI code page, which
    # has no "山" and writes "é" as a byte UTF-8 readers cannot read. PYTHONIOENCODING, when set, still decides.
    utf8 = not os.environ.get("PYTHONIOENCODING")
    for stream in (sys.stdout, sys.stderr):
        try:
            if utf8 and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            else:
                stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass
    arguments = build_parser().parse_args(argv)
    if arguments.command == "version":
        print(f"knowitall2 {__version__}")
        return 0
    if arguments.command == "serve":
        from .front import main as serve

        return serve()
    if arguments.command == "serve-once":
        from .mcp_server import run_once

        return run_once()
    if arguments.command == "serve-http":
        from .server.web import run as serve_http

        return serve_http(arguments.host, arguments.port)
    if arguments.command == "server-join-code":
        return _server_join_code(arguments.name)
    if arguments.command in _MEMORY_WORK:
        try:
            return _run_memory_work(arguments)
        finally:
            # This work runs on its own, mostly in a detached process after the hook that started it has
            # checked the knowledge files: so it rewrites them itself when it changed memories.
            from .known import nudge as refresh_known_files

            refresh_known_files()
    if arguments.command == "known":
        from .known import run_command as run_known

        return run_known(arguments)
    if arguments.command in {"setup", "uninstall", "doctor"}:
        return _run_agent_command(arguments)
    if arguments.command == "server":
        return _run_server_command(arguments)
    if arguments.command == "app":
        return _run_app(arguments)
    if arguments.command == "update":
        from . import update

        if arguments.finish:
            return update.finish(previous=arguments.previous, unchanged=arguments.unchanged)
        return update.run()
    try:
        store = Store.open(database_path())
    except StoreError as exc:
        print(f"knowitall2: {exc}", file=sys.stderr)
        return 1
    try:
        agent = f"import:{arguments.label}"[:40] if arguments.command == "import" else "cli"
        print(_run(Memory(store, agent=agent), store, arguments))
        from .connected import nudge
        from .known import nudge as refresh_known_files

        nudge("cli", store=store, pull=False)
        refresh_known_files()
        return 0
    except MemoryInputError as exc:
        print(f"knowitall2: {exc}", file=sys.stderr)
        return 1
    except (StoreError, sqlite3.Error) as exc:
        print(f"knowitall2: the memory store failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


# Commands that change memories without a tool call or a hook around them: syncing, learning, upkeep.
_MEMORY_WORK = frozenset({"sync", "learn", "maintain", "catalog", "find-out"})


def _run_memory_work(arguments: argparse.Namespace) -> int:
    if arguments.command == "sync":
        from .connected import run_command as run_sync

        return run_sync(arguments)
    if arguments.command == "learn":
        from .learning.command import run_learn

        return run_learn(arguments)
    if arguments.command == "maintain":
        from .learning.command import run_maintain

        return run_maintain(arguments)
    if arguments.command == "catalog":
        from .learning.command import run_catalog_command

        return run_catalog_command(arguments)
    from .learning import finder

    result = finder.run(arguments.system)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "done" else 1


def _server_join_code(name: str) -> int:
    """Make a join code on this server's database (until the admin page can make them)."""

    from .server import accounts, open_store

    try:
        store = open_store(database_path())
    except StoreError as exc:
        print(f"knowitall2: {exc}", file=sys.stderr)
        return 1
    try:
        found = accounts.new_join_code(store, name, now=journal.utc_now())
    except accounts.AccountError as exc:
        print(f"knowitall2: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"Join code for {found['name']}: {found['code']} (works once, until {found['expires_at']})")
    return 0


def _run(memory: Memory, store: Store, arguments: argparse.Namespace) -> str:
    if arguments.command == "remember":
        return memory.remember(
            arguments.text,
            kind=arguments.kind,
            subjects=arguments.subjects,
            tags=arguments.tags,
            scope=arguments.scope,
            source=arguments.source,
            replaces=arguments.replaces,
            project_path=arguments.project_path,
        ).describe()
    if arguments.command == "recall":
        return memory.recall(
            arguments.query, limit=arguments.limit, scope=arguments.scope, project_path=arguments.project_path,
        )
    if arguments.command == "forget":
        return memory.forget(arguments.id, reason=arguments.reason)
    if arguments.command == "restore":
        return memory.restore(arguments.id)
    if arguments.command == "move":
        return _move(memory, store, arguments)
    if arguments.command == "import":
        from .importer import import_file

        report = import_file(memory, arguments.file, dry_run=arguments.dry_run)
        return report.describe(dry_run=arguments.dry_run, verbose=arguments.verbose or arguments.dry_run)
    if arguments.command == "history":
        if not 1 <= arguments.limit <= 200:
            raise MemoryInputError("--limit must be between 1 and 200.")
        return memory.history(limit=arguments.limit)
    if arguments.command == "brief":
        return memory.briefing(project_path=arguments.project_path)
    if arguments.command == "questions":
        return review.list_questions(memory, how_to_answer="Answer with: knowitall2 answer <id> <choice>")
    if arguments.command == "answer":
        return review.answer(memory, arguments.id, arguments.choice)
    if arguments.command == "activity":
        return _format_activity(store, arguments)
    return _format_stats(store)


def _move(memory: Memory, store: Store, arguments: argparse.Namespace) -> str:
    from .catalog import find_system

    found = store.project_named(arguments.project)
    project = memory.stored_project(found["id"]) if found else None
    if project is None:
        raise MemoryInputError(f"There is no single project named {arguments.project!r}; the app lists them by name.")
    system_id = None
    if arguments.system:
        system = find_system(store.systems_list(), arguments.system)
        if system is None:
            raise MemoryInputError(f"There is no system named {arguments.system!r}.")
        system_id = system["id"]
    lines = []
    for record_id in arguments.ids:
        try:
            lines.append(memory.move(record_id, project, system_id=system_id))
        except MemoryInputError as exc:
            lines.append(f"Not moved: {exc}")
    return "\n".join(lines)


def _format_activity(store: Store, arguments: argparse.Namespace) -> str:
    if not 1 <= arguments.limit <= 500:
        raise MemoryInputError("--limit must be between 1 and 500.")
    if arguments.problems:
        problems = journal.read_problems(limit=arguments.limit)
        if not problems:
            return "No problems have been recorded."
        lines = ["Recorded problems, newest first:"]
        lines.extend(f"{_local_time(item['at'])}  {item.get('source', '?')}: {item.get('message', '')}" for item in problems)
        return "\n".join(lines)
    events = store.events(kinds=arguments.kinds, run_id=arguments.run, limit=arguments.limit)
    if not events:
        return "Nothing has been recorded yet." if not (arguments.kinds or arguments.run) else "No matching activity."
    lines = ["Activity, newest first:"]
    for event in events:
        who = f"  {event['agent']}" if event["agent"] else ""
        run = f"  [{event['run_id']}]" if event["run_id"] and event["kind"] == "learning" else ""
        outcome = f" ({event['outcome']})" if event["outcome"] else ""
        lines.append(f"{_local_time(event['at'])}  {event['kind']}{outcome}{who}{run}: {event['summary']}")
        reason = event["details"].get("reason") if event["kind"] == "candidate" else None
        if reason:
            lines.append(f"    why: {reason}")
    return "\n".join(lines)


def _local_time(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _run_app(arguments: argparse.Namespace) -> int:
    if arguments.shortcut or arguments.remove_shortcut:
        from .app import shortcut

        try:
            changes = shortcut.create(desktop=arguments.desktop) if arguments.shortcut else shortcut.remove()
        except (shortcut.ShortcutError, OSError, subprocess.SubprocessError) as exc:
            print(f"knowitall2: {exc}", file=sys.stderr)
            return 1
        print("\n".join(f"- {change}" for change in changes) or "- there was no shortcut to remove")
        return 0
    from .app import run

    return run(show_window=not arguments.no_window, port=arguments.port)


def _run_agent_command(arguments: argparse.Namespace) -> int:
    if arguments.command == "doctor":
        from .doctor import run_checks

        checks = run_checks(agents=arguments.agents, user_home=arguments.user_home)
        print(describe_checks(checks))
        healthy = all(check.ok for check in checks)
        print("KnowItAll2 is healthy." if healthy else "KnowItAll2 needs attention; see the fixes above.")
        return 0 if healthy else 1
    adapter = adapter_for(arguments.agent, user_home=arguments.user_home)
    shared: list[str] = []
    if arguments.command == "setup" and (arguments.join_code or arguments.server):
        if not arguments.join_code:
            print("knowitall2: --server needs this agent's --join-code from the server's page (Add an agent)",
                  file=sys.stderr)
            return 1
        # A join code works once: spend it only when setup can finish afterwards.
        try:
            adapter.preflight()
        except (AgentError, OSError) as exc:
            print(f"knowitall2: {exc}", file=sys.stderr)
            return 1
        # Connect first: if the server or the code does not work, nothing is changed.
        ok, shared = _connect_agent(arguments)
        if not ok:
            print("knowitall2: " + "\n".join(shared), file=sys.stderr)
            return 1
    try:
        if arguments.command == "setup":
            changes = adapter.setup(server_launch())
        else:
            changes = adapter.uninstall()
    except (AgentError, OSError) as exc:
        print(f"knowitall2: {exc}", file=sys.stderr)
        return 1
    if arguments.command == "uninstall":
        shared = _forget_agent_key(arguments.agent)
        if arguments.user_home is None:
            shared = [*shared, *_remove_knowledge_files_if_last()]
    verb = "setup" if arguments.command == "setup" else "uninstall"
    # The app is installed with KnowItAll2; testing against another home leaves the real shortcut alone.
    app_changes: list[str] = []
    if verb == "setup" and arguments.user_home is None:
        _write_update_script()
        if not arguments.no_app_shortcut:
            app_changes = _install_app_shortcut()
    lines = [f"KnowItAll2 {verb} for {adapter.display_name}:"]
    if changes or app_changes:
        lines.extend(f"- {change}" for change in [*changes, *app_changes])
    else:
        lines.append("- nothing changed; it is already " + ("set up." if verb == "setup" else "removed."))
    lines.extend(f"- {line}" for line in shared)
    if changes and verb == "setup":
        lines.append(adapter.restart_hint())
    elif changes:
        lines.append(f"Your memories were kept in {data_home()}; delete that folder to remove them.")
        if arguments.user_home is None:
            lines.append("The KnowItAll2 app stays for your other agents; remove it with: knowitall2 app --remove-shortcut")
    print("\n".join(lines))
    return 0


def _connect_agent(arguments: argparse.Namespace) -> tuple[bool, list[str]]:
    """Connect one agent to a server with its join code: (worked, what to tell the user)."""

    from . import connected

    try:
        store = Store.open(database_path())
    except StoreError as exc:
        return False, [str(exc)]
    try:
        existing = connected.load()
        if existing is None and not arguments.server:
            return False, ["give the server's address with --server, such as --server https://kia.example.lan"]
        address = arguments.server or existing.address
        first = existing is None
        if first:
            count = connected.local_memories(store)
            if count and not (arguments.send_memories or arguments.replace_memories):
                return False, [
                    f"this computer already has {count} memories. Ask the user which they want, then run the same "
                    "command again with one of:",
                    "  --send-memories     add them to the server's shared memory (the server keeps one copy of "
                    "anything it already has)",
                    "  --replace-memories  set them aside, with a backup, and use only the server's memory",
                ]
        try:
            report = connected.connect(store, address, arguments.agent, arguments.join_code,
                                       upload=arguments.send_memories, replace=arguments.replace_memories)
        except connected.ConnectError as exc:
            return False, [str(exc)]
    finally:
        store.close()
    address = connected.load().address
    name = (report.get("connection") or {}).get("name") or arguments.agent
    lines = [f"connected {arguments.agent} to the KnowItAll2 server at {address} as \"{name}\""]
    if first:
        if report.get("uploaded"):
            lines.append(f"sent this computer's memory to the server ({report['uploaded']} items)")
        if report.get("refused"):
            lines.append(f"the server refused {report['refused']} items of this computer's memory; the reasons are in: "
                         "knowitall2 activity --problems")
        if report.get("backup"):
            lines.append(f"set this computer's memories aside; the backup is {report['backup']}")
        lines.append(f"received {report.get('received', 0)} items of the shared memory; this computer now keeps "
                     "a copy and syncs it in the background")
        if report.get("problem"):
            lines.append(f"the first sync did not finish ({report['problem']}); it carries on in the background")
    warning = connected.plain_http_warning(address)
    if warning:
        lines.append(f"note: {warning}")
    return True, lines


def _forget_agent_key(agent: str) -> list[str]:
    from . import connected

    if not connected.is_connected():
        return []
    store = Store.open(database_path())
    try:
        if connected.remove_agent(store, agent):
            return ["it was the last agent here with a key for the KnowItAll2 server, so this computer keeps its "
                    "memory here only again (its copy stays); remove its entries on the server's page"]
    finally:
        store.close()
    return [f"removed {agent}'s key for the KnowItAll2 server; remove its entry on the server's page"]


def _remove_knowledge_files_if_last() -> list[str]:
    """After uninstalling an agent: when no agent here still has KnowItAll2's instructions, take out the
    knowledge files KnowItAll2 wrote into projects (memories are kept)."""

    from . import known
    from .agents import instructions

    for name in AGENT_NAMES:
        path = getattr(adapter_for(name), "instructions_path", None)
        if path is not None and instructions.state(path) != "missing":
            return []
    try:
        store = Store.open(database_path()) if database_path().is_file() else None
    except StoreError:
        store = None
    try:
        changes = known.remove_all(store._connection if store is not None else None)
    finally:
        if store is not None:
            store.close()
    return [f"no agent here uses KnowItAll2 any more, so its knowledge files were taken out of projects "
            f"({len(changes)} changes)"] if changes else []


def _run_server_command(arguments: argparse.Namespace) -> int:
    from . import connected

    if arguments.server_action == "status":
        healthy, lines = connected.describe_status()
        print("\n".join(lines))
        return 0 if healthy else 1
    if arguments.server_action == "connect":
        ok, lines = _connect_agent(arguments)
        if not ok:
            print("knowitall2: " + "\n".join(lines), file=sys.stderr)
            return 1
        print("\n".join(lines))
        return 0
    if not connected.is_connected():
        print("This computer already keeps its memory here only.")
        return 0
    store = Store.open(database_path())
    try:
        connected.disconnect(store)
    finally:
        store.close()
    print("This computer keeps its memory here only again; its copy of the shared memory stays. "
          "Remove its agents on the server's page.")
    return 0


def _write_update_script() -> None:
    """The script agents run when the user says "update KnowItAll2"."""

    from . import update

    try:
        update.write_launcher()
        update.remember_version()
    except OSError as exc:
        journal.problem("setup", f"could not write the update script: {exc}")


def _install_app_shortcut() -> list[str]:
    from .app import shortcut

    try:
        return shortcut.install()
    except Exception as exc:
        # The agent is set up either way; the shortcut can be added later.
        return [f"could not add the KnowItAll2 app shortcut ({exc}); add it with: knowitall2 app --shortcut"]


def _format_stats(store: Store) -> str:
    from .learning.maintenance import REASON_PREFIX
    from .learning.state import LearnerState

    stats = store.stats()
    now = datetime.now(timezone.utc)
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    usage = store.usage_summary(since=month_ago)
    kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(stats["by_kind"].items())) or "none"
    sources = ", ".join(f"{name} {count}" for name, count in sorted(usage["sources"].items())) or "none"
    recalls = usage["operations"].get("recall", {"count": 0, "with_results": 0})
    briefings = usage["operations"].get("briefing", {"count": 0, "with_results": 0})
    rate = f" ({recalls['with_results'] * 100 // recalls['count']}%)" if recalls["count"] else ""
    learner = LearnerState()
    lines = [
        f"knowitall2 {__version__}",
        f"Data: {database_path()}",
        f"Memories: {stats['active']} active ({stats['superseded']} superseded, {stats['retired']} retired)",
        f"By kind: {kinds}",
        f"By source: {sources}",
        f"Projects seen: {stats['projects']}",
        f"Open questions for you: {store.count_open_questions()}",
        "Usefulness, last 30 days:",
        f"  recalls: {recalls['count']}, found something: {recalls['with_results']}{rate}",
        f"  briefings: {briefings['count']}",
        f"  never returned yet: {usage['never_used']} of {stats['active']} memories",
    ]
    for item in usage["top"]:
        lines.append(f"  used {item['count']}x: [{item['id']}] {' '.join(item['text'].split())[:90]}")
    lines.append(
        f"Learner: {learner.calls_since(now - timedelta(days=1))} calls in the last 24 hours, "
        f"{learner.calls_since(now - timedelta(days=30))} in the last 30 days"
    )
    last_review = store.last_review()
    lines.append(
        f"Maintenance: {store.count_changed_by(REASON_PREFIX, since=month_ago)} memories merged or retired "
        f"in the last 30 days; last review {last_review[:16].replace('T', ' ') + ' UTC' if last_review else 'never'}"
    )
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    problems = len(journal.read_problems(since=week_ago))
    lines.append(
        f"Problems in the last 7 days: {problems}" + (" (see: knowitall2 activity --problems)" if problems else "")
    )
    return "\n".join(lines)
