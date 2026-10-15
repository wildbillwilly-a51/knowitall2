"""Agent hook entry points: fast, fail-open, and never delaying a session.

- ``session-start`` (Claude Code and Codex) adds the capped KnowItAll2
  briefing to the new session, shows news of learning the session has not
  shown yet, and starts the background learner, detached, when learning is on.
- ``stop`` (both agents; it runs when a turn ends) starts learning from the
  session right away when the turn made a commit, and shows the user what
  learning started, learned, or could not do.
- ``session-end`` (Claude Code only; Codex has no such hook) starts learning
  from the rest of the session.
- ``prompt-submit`` (both agents) runs when the user sends a message. It gives
  the agent what is new to the chat since its briefing (``chats``): memories
  learned since in other chats, about its projects or the systems it
  touched; once, what was learned from the chat itself, not yet checked; and
  a project's table of contents when the chat starts working in another
  project. Where the app does not show hook messages, it also passes news of
  learning to the agent, to tell the user once a chat.

Learning itself always runs in a separate, detached process, so an agent
never waits for it. News goes to the user as ``systemMessage``, which the
agent does not read and which costs it no tokens. The Claude desktop app
shows those messages only in a collapsed notice that is easy to miss
(2026-09-29), so there the news goes to the agent instead, when the user
next sends a message, to pass on in a line or two (the user's choice). Any failure produces no output and a
normal exit, so KnowItAll2 can never stop a session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import journal
from .learning import moments
from .learning.state import LearnerState, RunLock, load_settings
from .paths import HOME_ENVIRONMENT_VARIABLE, data_home, database_path

HOOK_AGENTS = ("claude-code", "codex")
# While ``main`` runs a hook: what to note (news shown, news passed on) once its output is really written.
_deferred: list[Callable[[], None]] | None = None
HOOK_EVENTS = ("session-start", "stop", "session-end", "prompt-submit")
_ENTRYPOINT = re.compile(rb'"entrypoint"\s*:\s*"([^"]+)"')
# Hosts that show hook messages only where the user easily misses them (collapsed).
SILENT_HOSTS = ("claude-desktop",)
RELAY = (
    "KnowItAll2 news for the user. This app shows KnowItAll2's own messages only in a collapsed notice, so pass "
    "it on: at the start of your reply, tell the user in one or two short lines what KnowItAll2 learned or could "
    "not do, then continue with their request. It needs no action from you."
)
BACKGROUND = "KnowItAll2 background for you (no need to mention it to the user):"
NEW_HEADER = "New in KnowItAll2 since this chat started (recall with a few of these words for the details):"
OWN_HEADER = ("What KnowItAll2 learned from this chat so far, from your own work here (no need to recall it). It is "
              "checked once the chat has been quiet for a while; if one is wrong, correct it with remember or "
              "forget it:")
OWN_LATER = ("KnowItAll2 also learned from this chat since, not yet checked (correct a wrong one with remember or "
             "forget):")
CATCH_UP_HEADER = ("KnowItAll2's table of contents for project {name}, which this chat's briefing did not have "
                   "(recall with a few of these words for the details):")
SPAWN_INTERVAL_SECONDS = 30 * 60
# How long the session start's writes wait for another process's write lock before they are dropped.
WRITE_TIMEOUT_SECONDS = 0.5


def main(argv: Sequence[str] | None = None, *, stdin: str | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2 or arguments[0] not in HOOK_AGENTS or arguments[1] not in HOOK_EVENTS:
        return 0
    global _deferred
    source = f"{arguments[1]} hook ({arguments[0]})"
    handler = {"session-start": session_start, "stop": stop, "session-end": session_end,
               "prompt-submit": prompt_submit}[arguments[1]]
    _deferred = []
    try:
        try:
            # Agents send UTF-8, but on Windows text stdin is in the ANSI code page, which
            # would misread a folder name such as "alphá" and so lose the project.
            text = sys.stdin.buffer.read().decode("utf-8", "replace") if stdin is None else stdin
            output = handler(text, agent=arguments[0])
        except Exception as exc:
            journal.problem(source, f"{type(exc).__name__}: {exc}")
            output = ""
        if output:
            # The output is ASCII-only JSON, so no console encoding can break it.
            try:
                sys.stdout.write(output)
                sys.stdout.flush()
            except (OSError, ValueError, UnicodeError) as exc:
                journal.problem(source, f"its output could not be written: {exc}")
                return 0  # the news was not delivered, so it stays unshown
        for note in _deferred:
            try:
                note()
            except Exception as exc:
                journal.problem(source, f"the news it showed could not be marked as shown: {exc}")
    finally:
        _deferred = None
        try:
            # When connected to a server: fetch what other computers learned, and send what this one saved.
            # Looked up first (as ``connected.is_connected`` does), so a computer that is not connected never
            # loads the network code, which costs more than everything else a hook does.
            if (data_home() / "server" / "connection.json").is_file():
                from .connected import nudge

                nudge(arguments[0])
        except Exception as exc:
            journal.problem(source, f"a sync could not start: {type(exc).__name__}: {exc}")
        # Rewrite the knowledge files in each project when memories changed (including by that sync's
        # previous run); one read-only query when nothing changed. Never raises.
        from .known import nudge as refresh_known_files

        refresh_known_files()
    return 0


def session_start(
    stdin_text: str, *, agent: str = "claude-code", start_learner: Callable[[], None] | None = None,
) -> str:
    """Return what the SessionStart hook prints: briefing JSON, or nothing."""

    payload = _payload(stdin_text)
    cwd = _text(payload, "cwd")
    from .known import worked_in

    worked_in(cwd)   # knowledge files go only to folders agent sessions run in
    context = ""
    try:
        context, project, since = _briefing(agent, cwd or Path.cwd())
    except Exception as exc:
        context = ""
        journal.problem(f"session start ({agent})", f"no briefing was added: {exc}")
    else:
        try:
            _start_chat(payload, agent, since=since, project_id=project.id if project else None, briefing=context)
        except Exception as exc:
            journal.problem(f"session start ({agent})", f"the chat's state could not be kept: {exc}")
    message = ""
    try:
        if load_settings().enabled:
            news = _news_items(payload, agent)
            if news and not shows_hook_messages(payload, agent):
                relay, notice = _passed_on(payload, agent, news)
                if relay:
                    context = "\n\n".join(part for part in (context, RELAY + "\n\n" + "\n\n".join(relay)) if part)
                message = "\n\n".join(notice)
            else:
                message = "\n\n".join(text for _, text in news)
    except Exception as exc:
        journal.problem(f"session start ({agent})", f"learning news could not be shown: {exc}")
    try:
        (start_learner or maybe_start_learner)()
    except Exception as exc:
        journal.problem(f"session start ({agent})", f"background learning could not start: {exc}")
    output: dict[str, Any] = {}
    if message:
        output["systemMessage"] = message
    if context:
        output["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": context}
    return json.dumps(output) if output else ""


def _briefing(agent: str, folder: str | Path) -> tuple[str, Any, str]:
    """The briefing, its project, and when it was made.

    It is read on the store opened only to read, so another process holding
    the write lock (a long import, a sync, a learning run) cannot hold it up.
    What it records (its use, the project's last use, what it offered) is
    written after, waiting at most ``WRITE_TIMEOUT_SECONDS``: when the store is
    still busy, those writes are dropped, never the briefing.
    """

    from .memory import Memory
    from .store import Store, StoreError

    try:
        reader = Store.open_read_only(database_path())
    except StoreError:
        reader = None  # not created yet, or to be upgraded: the usual opening below does that
    if reader is not None:
        memory = None
        try:
            memory = Memory(reader, agent=agent)
            memory.put_off_writes()
            since = memory.now()
            context = memory.briefing(project_path=folder)
            project = memory.project_for(folder)
        except Exception as exc:
            memory = None
            journal.problem(f"session start ({agent})", "the briefing could not be made on the store opened to "
                                                        f"read, so it was made as before: {type(exc).__name__}: {exc}")
        finally:
            reader.close()
        if memory is not None:
            try:
                store = Store.open(database_path(), timeout=WRITE_TIMEOUT_SECONDS)
                try:
                    memory.make_writes(store)
                    journal.tidy(store)
                finally:
                    store.close()
            except Exception as exc:
                journal.problem(f"session start ({agent})", "what the briefing records (its use, the project's last "
                                                            f"use) was not kept: {type(exc).__name__}: {exc}")
            return context, project, since
    store = Store.open(database_path())
    try:
        memory = Memory(store, agent=agent)
        since = memory.now()
        context = memory.briefing(project_path=folder)
        project = memory.project_for(folder)
        journal.tidy(store)
    finally:
        store.close()
    return context, project, since


def stop(stdin_text: str, *, agent: str = "claude-code", start_learner: Callable[..., object] | None = None) -> str:
    """When a turn ends: learn right away if it made a commit, and show news of learning."""

    if not load_settings().enabled:
        return ""
    payload = _payload(stdin_text)
    transcript = _transcript(payload, agent)
    messages = []
    commits = moments.commits_since_last_look(transcript, first_look_from=lambda: _learned_to(transcript)) if transcript else []
    if commits:
        detail = commits[-1][:7]
        if moments.request(transcript=transcript, session_id=_text(payload, "session_id"), agent=agent,
                           cwd=_text(payload, "cwd"), reason="commit", detail=detail):
            (start_learner or maybe_start_learner)(requests=True)
            messages.append(moments.started_message("commit", detail))
    if not shows_hook_messages(payload, agent, transcript=transcript):
        return ""  # the news waits for the user's next message (prompt_submit)
    news = _news_message(payload, agent, transcript=transcript)
    if news:
        messages.append(news)
    return json.dumps({"systemMessage": "\n\n".join(messages)}) if messages else ""


def prompt_submit(stdin_text: str, *, agent: str = "claude-code") -> str:
    """When the user sends a message: what KnowItAll2 learned since the chat began, and news to relay.

    For the agent: headlines of memories added since the chat's briefing, and a
    project's table of contents when the chat starts working in that project.
    In an app that does not show hook messages, also news of learning for the
    agent to pass on.
    """

    payload = _payload(stdin_text)
    transcript = _transcript(payload, agent)
    from .known import worked_in

    worked_in(_text(payload, "cwd"))
    blocks = []
    try:
        background = _chat_background(payload, agent, transcript)
        if background:
            blocks.append(f"{BACKGROUND}\n\n{background}")
    except Exception as exc:
        journal.problem(f"message hook ({agent})", f"what is new could not be added: {exc}")
    notice: list[str] = []
    if load_settings().enabled and not shows_hook_messages(payload, agent, transcript=transcript):
        news: list[tuple[str, str]] = []
        now = moments.read_now()
        cwd = _text(payload, "cwd")
        if now and now.get("folder") and cwd and _same_place(str(now["folder"]), cwd) and moments.first_mention(
                f"now-{now.get('since')}", transcript=transcript, session_id=_text(payload, "session_id")):
            news.append((STARTED, moments.started_message(str(now.get("reason") or "asked"),
                                                          str(now.get("detail") or ""))))
        news += _news_items(payload, agent, transcript=transcript)
        relay, notice = _passed_on(payload, agent, news, transcript=transcript)
        if relay:
            blocks.append(RELAY + "\n\n" + "\n\n".join(relay))
    output: dict[str, Any] = {}
    if notice:
        output["systemMessage"] = "\n\n".join(notice)
    if blocks:
        output["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": "\n\n".join(blocks)}
    return json.dumps(output) if output else ""


def _start_chat(payload: dict[str, Any], agent: str, *, since: str, project_id: str | None, briefing: str) -> None:
    from . import chats

    chat = chats.chat_key(_text(payload, "session_id"), _transcript(payload, agent))
    if chat is not None:
        chats.start(chat, since=since, project_id=project_id, told=chats.RECORD_ID.findall(briefing),
                    transcript=_transcript(payload, agent), session_id=_text(payload, "session_id"))
        chats.prune()


def _pointers(payload: dict[str, Any], store: Any, said: Sequence[str], state: dict[str, Any],
              commands: Sequence[str] = ()) -> list[tuple[str, str]]:
    """Which knowledge files the user's message or the agent's own words since the last message name.

    Not the agent's tool calls: their logins, paths, and long commands named many systems the work did
    not need, and agents learned to skip the pointers (2026-10-05: 8 of 45 opened). The systems its shell
    ``commands`` reach are noted for the folder's next refresh instead. What arrives as a message is read
    by who wrote it (``known.message_parts``): a subagent's result counts as the agent's words, a monitor's
    event as nobody's.

    Never raises: a pointer is a help, and a failure must not cost the chat what else this hook adds.
    """

    try:
        from . import known
        from .identity import find_git_root

        cwd = _text(payload, "cwd")
        if not cwd or not known.enabled():
            return []
        root = find_git_root(Path(cwd))
        if root is None:
            return []
        users, agents = known.message_parts(_text(payload, "prompt"))
        return known.find_pointers(store._connection, users, root / known.FOLDER, state.get("pointed", []),
                                   own_words=[*said, *agents], commands=commands)
    except Exception as exc:
        journal.problem("message hook", f"knowledge-file pointers could not be found: {type(exc).__name__}: {exc}")
        return []


def _chat_background(payload: dict[str, Any], agent: str, transcript: str | None) -> str:
    """What is new to this chat since its briefing: memories added since, and projects it moved to."""

    from . import chats
    from .identity import identify
    from .memory import Memory
    from .store import Store

    chat = chats.chat_key(_text(payload, "session_id"), transcript)
    if chat is None:
        return ""
    state = chats.load(chat)
    parts: list[str] = []
    store = Store.open(database_path())
    try:
        memory = Memory(store, agent=agent)
        if state is None:
            # A chat that began before KnowItAll2 kept chat state: its briefing had no table of
            # contents, so give it its project's once, then carry on from now.
            cwd = _text(payload, "cwd")
            project = identify(Path(cwd)) if cwd else None
            state = {"since": memory.now(), "projects": [], "told": [], "log": transcript,
                     "offset": max(0, chats.log_size(transcript) - chats.CATCH_UP_READ)}
            if _text(payload, "session_id"):
                state["session"] = _text(payload, "session_id")
            said: list[str] = []
            commands: list[str] = []
            inputs, mentioned = chats.read_new(state, transcript, said=said, commands=commands)
            state["told"] = sorted(mentioned)
            if project is not None:
                state["projects"].append(project.id)
                contents = memory.project_contents(project.id, known=state["told"], header=CATCH_UP_HEADER)
                if contents:
                    parts.append(contents)
                    state["told"] += chats.RECORD_ID.findall(contents)
        else:
            said, commands = [], []
            inputs, mentioned = chats.read_new(state, transcript, said=said, commands=commands)
            state["told"] = [*state.get("told", []), *sorted(mentioned)]
        pointers = _pointers(payload, store, said, state, commands)
        if pointers:
            parts.append("\n".join(line for _, line in pointers))
            state["pointed"] = [*state.get("pointed", []), *(target for target, _ in pointers)]
            from .known import log_pointers

            log_pointers(agent, chat, [target for target, _ in pointers], at=memory.now())
        worked = chats.projects_worked_in(inputs, store.all_project_paths())
        moved = [project_id for project_id, calls in sorted(worked.items(), key=lambda item: -item[1])
                 if calls >= chats.WORK_CALLS and project_id not in state["projects"]]
        if moved:
            # One project per message, the one worked in most; another follows if work goes on there.
            state["projects"].append(moved[0])
            contents = memory.project_contents(moved[0], known=state["told"])
            if contents:
                parts.append(contents)
                state["told"] += chats.RECORD_ID.findall(contents)
        from .known import message_parts

        users, _ = message_parts(_text(payload, "prompt"))
        state["touched"] = _touched(store, state, [*commands, *users])
        sessions = chats.own_sessions(state, session_id=_text(payload, "session_id"), transcript=transcript)
        # What was learned from the chat, each item once: the first batch in a block (again after a compaction,
        # ``chats.start``, which may have taken the chat's early findings out of its context), later ones in a
        # short line (candidate review 2026-10-14, E-L6: lessons from later commits were never told).
        own = memory.learned_in(sessions, known=state["told"],
                                limit=chats.OWN_LATER_SHOWN if state.get("own_told") else chats.OWN_SHOWN)
        if own:
            if state.get("own_told"):
                parts.append(f"{OWN_LATER} " + "; ".join(line for _, line in own) + ".")
            else:
                parts.append("\n".join([OWN_HEADER, *(f"- {line}" for _, line in own)]))
            state["told"] += [record_id for record_id, _ in own]
            state["own_told"] = True
        new = memory.added_since(state["projects"], state["since"], known=state["told"], sessions=sessions,
                                 touched=state["touched"])
        if new:
            parts.append("\n".join([NEW_HEADER, *(f"- {line}" for _, line in new[:chats.NEW_SHOWN])]))
            state["told"] += [record_id for record_id, _ in new]  # all of them: the rest is not advertised
    finally:
        store.close()
    chats.save(chat, state)
    return "\n\n".join(parts)


def _touched(store: Any, state: dict[str, Any], texts: Sequence[str]) -> list[str]:
    """The systems the chat has touched: those it had, and those these commands or the user's words name.

    Commands and the user's words, as in the 2026-10-13 replay; not the agent's own prose, which names systems
    in passing (35% of the pointers it set off named a system the work never touched).
    """

    from . import chats
    from .known import system_table

    earlier = [item for item in state.get("touched") or [] if isinstance(item, str)]
    if not any(texts):
        return earlier
    return list(dict.fromkeys([*earlier, *chats.touched(texts, system_table(store._connection))]))


def tool_news(memory: Any, *, cwd: str | os.PathLike[str], agent: str | None, shown: Sequence[str] = (),
              environ: dict[str, str] | None = None) -> str:
    """For an agent's KnowItAll2 tool call: memories added since the chat's briefing that it has not seen.

    The message hook tells a chat only when the user next writes, and a chat
    can work for many minutes in between, so another chat's finding (such as
    the user's decision made there) could reach it too late (2026-09-30). Any
    tool call answers with what is new, once. The chat is found by the session
    id Claude Code gives its tools, else as the newest log in its folder;
    ``shown`` are ids the call's own answer already has. Empty when there is
    nothing new or the chat has no state.
    """

    from . import chats

    variables = os.environ if environ is None else environ
    session_id = variables.get("CLAUDE_CODE_SESSION_ID") if agent == "claude-code" else None
    log = None
    if not session_id:
        found_log = moments.newest_session_log(cwd, agent, within_minutes=10)
        log = str(found_log) if found_log else None
    found = chats.find_open(session_id=session_id, transcript=log)
    if found is None:
        return ""
    chat, state = found
    since, projects = state.get("since"), state.get("projects") or []
    if not isinstance(since, str) or not projects:
        return ""
    transcript = state.get("log") if isinstance(state.get("log"), str) else None
    told = [*(state.get("told") or []), *shown]  # what this answer shows, such as the chat's own save, is seen
    commands, mentioned = chats.seen_since(state, transcript)
    # What the agent's commands touched since the user's message counts now; the message hook keeps it later.
    new = memory.added_since(projects, since, known={*told, *mentioned}, touched=_touched(memory.store, state, commands),
                             sessions=chats.own_sessions(state, session_id=session_id, transcript=transcript))
    lines = []
    if new:
        lines = [NEW_HEADER, *(f"- {line}" for _, line in new[:chats.NEW_SHOWN])]
        told += [record_id for record_id, _ in new]
    if len(told) != len(state.get("told") or []):
        state["told"] = told
        chats.save(chat, state)
    return "\n".join(lines)


def shows_hook_messages(payload: dict[str, Any], agent: str, *, transcript: str | None = None) -> bool:
    """Whether the app running this session shows hook messages to the user.

    Claude Code names the app that runs it in ``CLAUDE_CODE_ENTRYPOINT``, and
    in each record of the session's log.
    """

    if agent != "claude-code":
        return True
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT") or _logged_entrypoint(transcript or _transcript(payload, agent))
    return entry not in SILENT_HOSTS


def _logged_entrypoint(transcript: str | None) -> str | None:
    if not transcript:
        return None
    try:
        with open(transcript, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            tail = stream.read()
    except OSError:
        return None
    found = _ENTRYPOINT.findall(tail)
    return found[-1].decode("utf-8", errors="replace") if found else None


def _same_place(first: str, second: str) -> bool:
    def normal(value: str) -> str:
        return os.path.normcase(os.path.normpath(value)).rstrip("\\/")

    return normal(first) == normal(second)


def session_end(stdin_text: str, *, agent: str = "claude-code",
                start_learner: Callable[..., object] | None = None) -> str:
    """When a session ends: learn what it did since it was last learned from."""

    if not load_settings().enabled:
        return ""
    payload = _payload(stdin_text)
    transcript = _transcript(payload, agent)
    if not transcript or not _unlearned(transcript):
        return ""
    if moments.request(transcript=transcript, session_id=_text(payload, "session_id"), agent=agent,
                       cwd=_text(payload, "cwd"), reason="session end"):
        (start_learner or maybe_start_learner)(requests=True)
    return ""


def request_now(*, cwd: str | os.PathLike[str], agent: str | None, reason: str,
                start_learner: Callable[..., object] | None = None) -> str:
    """For an agent's ``learn`` call: learn the agent's current session now; returns what to tell the agent."""

    if not load_settings().enabled:
        return "Learning is turned off in KnowItAll2, so nothing was learned. Save single facts with remember."
    log = moments.newest_session_log(cwd, agent)
    if log is None:
        return ("KnowItAll2 could not find this session's log, so it will learn from it when the session has "
                "been idle for a while. Save anything important now with remember.")
    moments.request(transcript=str(log), session_id=None, agent=agent or "", cwd=str(cwd), reason=reason)
    (start_learner or maybe_start_learner)(requests=True)
    return ("KnowItAll2 is learning from this session now, in the background. Tell the user in one short line; "
            "what it learns will be shown to them when this turn ends and in the KnowItAll2 app.")


def maybe_start_learner(
    *, now: float | None = None, launcher: Callable[..., object] = subprocess.Popen, force: bool = False,
    requests: bool = False,
) -> bool:
    """Start one detached learner run when learning is on and none ran recently.

    ``force`` skips the wait between runs, for the user's "Run now".
    ``requests`` learns only the sessions just asked for, right away; a run
    already in progress learns them before it stops.
    """

    if not load_settings().enabled:
        return False
    moment = time.time() if now is None else now
    marker = data_home() / "learner" / "last-start"
    if not requests:
        try:
            if not force and moment - marker.stat().st_mtime < SPAWN_INTERVAL_SECONDS:
                return False
        except OSError:
            pass
    if RunLock().busy():
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    if not requests:
        marker.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    log = open(marker.parent / "last-run.log", "w", encoding="utf-8")
    try:
        launcher(
            [sys.executable, "-B", "-P", "-m", "knowitall2", "learn", *(["--requests"] if requests else [])],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(data_home()), env=_learner_environment(), close_fds=True, **_detached_options(),
        )
    finally:
        log.close()
    return True


def _news_message(payload: dict[str, Any], agent: str, *, transcript: str | None = None) -> str:
    return "\n\n".join(text for _, text in _news_items(payload, agent, transcript=transcript))


def _news_items(payload: dict[str, Any], agent: str, *, transcript: str | None = None) -> list[tuple[str, str]]:
    """News of learning this session has not shown, as (status, what the user reads); marked as shown once the
    hook's output is written."""

    transcript = transcript or _transcript(payload, agent)
    session_id, cwd = _text(payload, "session_id"), _text(payload, "cwd")
    items = moments.news_to_show(transcript=transcript, session_id=session_id, cwd=cwd)
    if not items:
        return []
    mine = set(moments.session_keys(transcript, session_id))
    texts = [(str(item.get("status") or "done"),
              moments.describe(item, here=bool(mine & set(item.get("sessions") or [])))) for item in items]
    _after_output(lambda: moments.mark_shown(items, transcript=transcript, session_id=session_id))
    return texts


# News that waits for the user is passed on every time: the daily limit used up, or the model not available.
_NEEDS_THE_USER = ("limit", "stopped")
STARTED = "started"


def _passed_on(payload: dict[str, Any], agent: str, news: list[tuple[str, str]], *,
               transcript: str | None = None) -> tuple[list[str], list[str]]:
    """For an app that does not show hook messages: which news the agent passes on to the user, and which is
    shown only in the app's own notice.

    The agent passed on "KnowItAll2 learned N things" after every commit, opening most of its replies with it
    (2026-10-12 review: 42 of 58 times), and each relay could set off a pointer. So a chat passes on news of
    learning once (and that learning has started, until then); later news goes to the app's notice and to the
    KnowItAll2 app. A chat with no state of its own passes on everything, as before.
    """

    from . import chats

    chat = chats.chat_key(_text(payload, "session_id"), transcript or _transcript(payload, agent))
    state = chats.load(chat) if chat else None
    relayed = bool(state and state.get("relayed"))
    relay = [text for status, text in news if not relayed or status in _NEEDS_THE_USER]
    notice = [text for status, text in news if relayed and status not in _NEEDS_THE_USER]
    if chat and state is not None and not relayed and any(
            status not in (*_NEEDS_THE_USER, STARTED) for status, _ in news):
        _after_output(lambda: chats.mark_relayed(chat))
    return relay, notice


def _after_output(note: Callable[[], None]) -> None:
    """Note something once the hook's output is really written (in ``main``); right away when called directly."""

    if _deferred is not None:
        _deferred.append(note)
    else:
        note()


def _unlearned(transcript: str) -> bool:
    """Whether a session's log has grown past what the learner already read."""

    try:
        size = Path(transcript).stat().st_size
    except OSError:
        return False
    return size > _learned_to(transcript)


def _learned_to(transcript: str) -> int:
    """How far into a session's log the learner has read."""

    entry = LearnerState().logs.get(os.path.normcase(str(Path(transcript))), {})
    try:
        return int(entry.get("offset", 0))
    except (TypeError, ValueError):
        return 0


def _payload(stdin_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


def _transcript(payload: dict[str, Any], agent: str) -> str | None:
    """The session's log: as the agent gave it, or (Codex may give none) the newest log in its folder."""

    given = _text(payload, "transcript_path")
    if given:
        return given
    cwd = _text(payload, "cwd")
    found = moments.newest_session_log(cwd, agent) if cwd else None
    return str(found) if found else None


def _learner_environment() -> dict[str, str]:
    environment = dict(os.environ)
    package_parent = Path(__file__).resolve().parents[1]
    if package_parent.name == "src" and (package_parent.parent / "pyproject.toml").is_file():
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(package_parent) + (os.pathsep + existing if existing else "")
    environment.setdefault(HOME_ENVIRONMENT_VARIABLE, str(data_home()))
    # What the detached learner and finder print goes to a log file: write it as UTF-8, not the console code page.
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _detached_options() -> dict[str, object]:
    if sys.platform != "win32":
        return {"start_new_session": True}
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return {"creationflags": flags}
