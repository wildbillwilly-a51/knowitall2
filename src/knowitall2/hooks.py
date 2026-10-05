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
  learned since, and a project's table of contents when the chat starts
  working in another project. Where the app does not show hook messages, it
  also passes news of learning to the agent.

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
# While ``main`` runs a hook: news to mark as shown once its output is really written.
_deferred: list[tuple[list[dict[str, Any]], str | None, str | None]] | None = None
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
CATCH_UP_HEADER = ("KnowItAll2's table of contents for project {name}, which this chat's briefing did not have "
                   "(recall with a few of these words for the details):")
SPAWN_INTERVAL_SECONDS = 30 * 60


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
        try:
            for items, transcript, session_id in _deferred:
                moments.mark_shown(items, transcript=transcript, session_id=session_id)
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

    from .memory import Memory
    from .store import Store

    payload = _payload(stdin_text)
    cwd = _text(payload, "cwd")
    context = ""
    try:
        store = Store.open(database_path())
        try:
            memory = Memory(store, agent=agent)
            since = memory.now()
            context = memory.briefing(project_path=cwd or Path.cwd())
            project = memory.project_for(cwd or Path.cwd())
            journal.tidy(store)
        finally:
            store.close()
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
            message = _news_message(payload, agent)
            if message and not shows_hook_messages(payload, agent):
                context = "\n\n".join(part for part in (context, f"{RELAY}\n\n{message}") if part)
                message = ""
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
    blocks = []
    try:
        background = _chat_background(payload, agent, transcript)
        if background:
            blocks.append(f"{BACKGROUND}\n\n{background}")
    except Exception as exc:
        journal.problem(f"message hook ({agent})", f"what is new could not be added: {exc}")
    if load_settings().enabled and not shows_hook_messages(payload, agent, transcript=transcript):
        parts = []
        now = moments.read_now()
        cwd = _text(payload, "cwd")
        if now and now.get("folder") and cwd and _same_place(str(now["folder"]), cwd) and moments.first_mention(
                f"now-{now.get('since')}", transcript=transcript, session_id=_text(payload, "session_id")):
            parts.append(moments.started_message(str(now.get("reason") or "asked"), str(now.get("detail") or "")))
        news = _news_message(payload, agent, transcript=transcript)
        if news:
            parts.append(news)
        if parts:
            blocks.append(RELAY + "\n\n" + "\n\n".join(parts))
    if not blocks:
        return ""
    context = "\n\n".join(blocks)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})


def _start_chat(payload: dict[str, Any], agent: str, *, since: str, project_id: str | None, briefing: str) -> None:
    from . import chats

    chat = chats.chat_key(_text(payload, "session_id"), _transcript(payload, agent))
    if chat is not None:
        chats.start(chat, since=since, project_id=project_id, told=chats.RECORD_ID.findall(briefing),
                    transcript=_transcript(payload, agent))
        chats.prune()


def _pointers(payload: dict[str, Any], store: Any, inputs: Sequence[str], state: dict[str, Any]) -> list[tuple[str, str]]:
    """Which knowledge files the user's message or the agent's tool calls since the last message name.

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
        texts = [_text(payload, "prompt") or "", *inputs]
        return known.find_pointers(store._connection, texts, root / known.FOLDER, state.get("pointed", []))
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
            inputs, mentioned = chats.read_new(state, transcript)
            state["told"] = sorted(mentioned)
            if project is not None:
                state["projects"].append(project.id)
                contents = memory.project_contents(project.id, known=state["told"], header=CATCH_UP_HEADER)
                if contents:
                    parts.append(contents)
                    state["told"] += chats.RECORD_ID.findall(contents)
        else:
            inputs, mentioned = chats.read_new(state, transcript)
            state["told"] = [*state.get("told", []), *sorted(mentioned)]
        pointers = _pointers(payload, store, inputs, state)
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
        new = memory.added_since(state["projects"], state["since"], known=state["told"])
        if new:
            lines = [NEW_HEADER, *(f"- {line}" for _, line in new[:chats.NEW_SHOWN])]
            if len(new) > chats.NEW_SHOWN:
                lines.append(f"(+{len(new) - chats.NEW_SHOWN} more; use recall to search.)")
            parts.append("\n".join(lines))
            state["told"] += [record_id for record_id, _ in new]
    finally:
        store.close()
    chats.save(chat, state)
    return "\n\n".join(parts)


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
    new = memory.added_since(projects, since, known={*told, *chats.mentioned_since(state, transcript)})
    lines = []
    if new:
        lines = [NEW_HEADER, *(f"- {line}" for _, line in new[:chats.NEW_SHOWN])]
        if len(new) > chats.NEW_SHOWN:
            lines.append(f"(+{len(new) - chats.NEW_SHOWN} more; use recall to search.)")
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
    transcript = transcript or _transcript(payload, agent)
    session_id, cwd = _text(payload, "session_id"), _text(payload, "cwd")
    items = moments.news_to_show(transcript=transcript, session_id=session_id, cwd=cwd)
    if not items:
        return ""
    mine = set(moments.session_keys(transcript, session_id))
    text = "\n\n".join(moments.describe(item, here=bool(mine & set(item.get("sessions") or []))) for item in items)
    if _deferred is not None:
        _deferred.append((items, transcript, session_id))
    else:
        moments.mark_shown(items, transcript=transcript, session_id=session_id)
    return text


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
