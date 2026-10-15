"""What each open chat has been given, so the message hook adds only what is new to it.

Chats in the desktop apps stay open for days, and a chat opened in one folder
often works in another project. The session-start hook briefs a chat once;
after that, when the user sends a message, the message hook adds, once each:

- the headlines of memories added since the chat's briefing: the projects'
  own, from other chats, and shared ones about the project itself or a
  system the chat touched (named in its commands or the user's words);
- apart, what KnowItAll2 learned from the chat itself, not yet checked (each item once);
  and
- a project's own table of contents, when the chat starts working in a
  project other than its folder's (found from the paths its tools used).

A chat's state is one small JSON file in ``chats`` in the data home, named by
the chat's session id (or its log's path):

- ``since``: when the chat was briefed; memories added later are new to it;
- ``projects``: the projects it works in;
- ``told``: memories it has seen (in its briefing, or anywhere in its log);
- ``log`` and ``offset``: how far into its log the hook has read;
- ``session``: its session id, which names what the learner saved from it;
- ``touched``: the systems it named in its commands or the user's words;
- ``own_told``: whether it was given the block of what was learned from it (later items come in a short
  line); again after a compaction;
- ``relayed``: whether it passed on news of learning to the user, which it does once (kept across a compaction).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .files import read_text, short_path, write_text_atomic
from .paths import data_home

KEPT_DAYS = 30
# At most this many new memories at a time, with no "+N more": 312 items waited behind 197 shown in the 2026-10-13
# study, and about 7% of what was shown led to a recall.
NEW_SHOWN = 3
# What was learned from the chat itself, each item told once, newest first: the first batch, then a short line.
OWN_SHOWN = 5
OWN_LATER_SHOWN = 3
# Tool calls in one stretch of a chat that touch another project's folder before
# the chat counts as working there; one passing look is not enough.
WORK_CALLS = 3
READ_LIMIT = 4 * 1024 * 1024
# A chat that began before this version: how far back to look for what it saw and where it worked.
CATCH_UP_READ = 1024 * 1024
TOLD_KEPT = 2000
SHELLS = frozenset({"Bash", "PowerShell", "shell"})   # the tools whose calls are commands, as the logs name them
# How much of one command or message is read for the systems it names.
TOUCHED_TEXT_LIMIT = 20_000
# A word, as ``known`` splits names into words.
_WORD = re.compile(r"[^\W_]+")
RECORD_ID = re.compile(r"\bk-[0-9a-f]{10}\b")
_RECORD_ID_BYTES = re.compile(rb"\bk-[0-9a-f]{10}\b")


def folder() -> Path:
    return data_home() / "chats"


def chat_key(session_id: str | None, transcript: str | None) -> str | None:
    """The file name of a chat's state: by its session id, which survives a resume, else by its log."""

    from .learning.moments import key

    if session_id:
        return "s" + key(session_id)
    if transcript:
        return "t" + key(str(transcript))
    return None


def load(chat: str) -> dict[str, Any] | None:
    try:
        state = json.loads(read_text(folder() / f"{chat}.json"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def save(chat: str, state: dict[str, Any], *, fresh: bool = False) -> None:
    """Write a chat's state, merged with what another hook saved since ``state`` was read.

    The message hook, a KnowItAll2 tool call (the MCP server), and the note that news was passed on can save one
    chat at about the same time (candidate review 2026-10-14, E-L5). So the file is read again and merged under
    a short lock: lists keep what either added, a flag set by either stays set, the log offset never goes back.
    A save from before a new briefing (``since`` changed meanwhile) adds only its flags. ``fresh`` (a new
    briefing) replaces the state. The lock is waited for at most ``LOCK_WAIT_SECONDS``; then the merge is
    written without it, still atomically.
    """

    path = folder() / f"{chat}.json"
    held = _lock(path.with_suffix(".lock"))
    try:
        current = None if fresh else load(chat)
        merged = state if current is None else _merged(current, state)
        merged["told"] = list(dict.fromkeys(merged.get("told") or []))[-TOLD_KEPT:]
        write_text_atomic(path, json.dumps(merged) + "\n")
    finally:
        if held:
            try:
                path.with_suffix(".lock").unlink()
            except OSError:
                pass


# Chat-state lists only ever grow (what was told, touched, pointed to, the projects worked in).
_FLAGS = ("relayed", "own_told")
LOCK_WAIT_SECONDS = 0.05
LOCK_STALE_SECONDS = 10


def _merged(current: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if current.get("since") != state.get("since"):
        merged = dict(current)   # briefed again since ``state`` was read: that start wins
        if state.get("relayed"):
            merged["relayed"] = True   # once a chat, compacted or not
        return merged
    merged = {**current, **state}
    for name, value in state.items():
        before = current.get(name)
        if isinstance(value, list) and isinstance(before, list):
            merged[name] = list(dict.fromkeys([*before, *value]))
    for flag in _FLAGS:
        if current.get(flag) or state.get(flag):
            merged[flag] = True
    if current.get("log") == state.get("log"):
        merged["offset"] = max(int(current.get("offset") or 0), int(state.get("offset") or 0))
    return merged


def _lock(path: Path) -> bool:
    """Take a lock file, waiting at most ``LOCK_WAIT_SECONDS``; a lock left by a crash is taken over."""

    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                    path.unlink()
                    continue
            except OSError:
                pass
        except OSError:
            if not path.parent.is_dir():
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    continue
                except OSError:
                    return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


def start(chat: str, *, since: str, project_id: str | None, told: Iterable[str], transcript: str | None,
          session_id: str | None = None) -> None:
    """A chat was just briefed (a new chat, or one resumed, cleared, or compacted): start its state afresh.

    Whether it already passed on news of learning is kept: that is once a chat, compacted or not.
    """

    previous = load(chat) or {}
    state: dict[str, Any] = {"since": since, "projects": [project_id] if project_id else [], "told": list(told),
                             "log": transcript, "offset": log_size(transcript)}
    if session_id:
        state["session"] = session_id
    if previous.get("relayed"):
        state["relayed"] = True
    save(chat, state, fresh=True)


def mark_relayed(chat: str) -> None:
    """The chat passed on news of learning to the user (``hooks._passed_on``)."""

    state = load(chat)
    if state is not None and not state.get("relayed"):
        state["relayed"] = True
        save(chat, state)


def own_sessions(state: dict[str, Any], *, session_id: str | None = None, transcript: str | None = None) -> set[str]:
    """The ids the learner may have saved this chat's memories under: its session id, and the one its log names."""

    found = {value for value in (session_id, state.get("session")) if isinstance(value, str) and value}
    log = transcript or state.get("log")
    if isinstance(log, str) and log:
        from .learning.transcripts import _CODEX_SESSION_ID

        name = Path(log).name
        match = _CODEX_SESSION_ID.search(name)
        found.add(match.group(1) if match else Path(log).stem)
    return found


def find_open(*, session_id: str | None, transcript: str | None) -> tuple[str, dict[str, Any]] | None:
    """An open chat's key and state, by its session id or else by its log; None when it has none."""

    if session_id:
        chat = chat_key(session_id, None)
        state = load(chat) if chat else None
        if state is not None:
            return chat, state
    if not transcript:
        return None
    wanted = _same_log(transcript)
    try:
        paths = sorted(folder().glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in paths:
        state = load(path.stem)
        if state is not None and isinstance(state.get("log"), str) and _same_log(state["log"]) == wanted:
            return path.stem, state
    return None


def seen_since(state: dict[str, Any], transcript: str | None) -> tuple[list[str], set[str]]:
    """The shell commands and the memory ids the chat's log added past where the message hook last read
    (``read_new``), leaving that place as is."""

    if not transcript or state.get("log") != transcript:
        return [], set()
    commands: list[str] = []
    _, mentioned = read_new(dict(state), transcript, commands=commands)
    return commands, mentioned


def touched(texts: Iterable[str], table: dict[tuple[str, ...], str]) -> list[str]:
    """The systems of ``table`` (``known.system_table``) that these commands or words name, in order."""

    from .known import command_names

    # Only text holding every word of some name is matched, and only against those names: matching every name
    # word by word took about 1 ms per 1,000 characters (2 s for the largest chat of the 2026-10-13 study's logs).
    by_first: dict[str, dict[tuple[str, ...], str]] = {}
    for key, target in table.items():
        by_first.setdefault(key[0], {})[key] = target
    found: list[str] = []
    for text in texts:
        piece = str(text)[:TOUCHED_TEXT_LIMIT]
        words = set(_WORD.findall(piece.casefold()))
        names = {key: target for word in words & by_first.keys() for key, target in by_first[word].items()
                 if words.issuperset(key)}
        if not names:
            continue
        # A host reached by URL or network path counts too (candidate review of 0.11.1, E-M3).
        for system_id in command_names(piece, names):
            if system_id not in found:
                found.append(system_id)
    return found


def _same_log(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def read_new(state: dict[str, Any], transcript: str | None, *,
             said: list[str] | None = None, commands: list[str] | None = None) -> tuple[list[str], set[str]]:
    """What the chat's log added since the last look: its tool calls' inputs, and the memory ids it mentions.

    With ``said``, the agent's own messages to the user are added to it too, and the reports other agents
    gave it (a subagent's final result, a delegated message); with ``commands``, its shell commands. Moves
    the state's offset to the end of what was read.
    """

    if not transcript:
        return [], set()
    size = log_size(transcript)
    start = int(state.get("offset") or 0) if state.get("log") == transcript else 0
    if start > size:
        start = 0  # the log was replaced
    start = max(start, size - READ_LIMIT)
    if size <= start:
        return [], set()
    path = Path(transcript)
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            chunk = stream.read(size - start)
    except OSError:
        return [], set()
    mentioned = {match.decode("ascii") for match in _RECORD_ID_BYTES.findall(chunk)}
    inputs: list[str] = []
    try:
        from .learning.transcripts import read_session

        session, _ = read_session(path, start=start)
        inputs = [part for event in session.events if event.kind == "tool" for part in (event.text, event.cwd) if part]
        if said is not None:
            said.extend(event.text if event.kind != "tool" else event.output or "" for event in session.events
                        if event.kind in ("assistant", "agent_report") or (event.kind == "tool" and event.tool == "Agent"))
        if commands is not None:
            commands.extend(event.text for event in session.events if event.kind == "tool" and event.tool in SHELLS)
    except (OSError, ValueError):
        pass
    state["log"], state["offset"] = transcript, size
    return inputs, mentioned


def projects_worked_in(inputs: Iterable[str], known: Iterable[tuple[str, str, str]]) -> dict[str, int]:
    """How many tool calls touched each known project's folder; the deepest folder wins.

    ``known`` holds (project id, name, folder) for every folder any project was
    seen in. On Windows each part of a folder also matches by its short name
    (such as ``JOHNSM~1`` for ``John Smith``), which some tools print for part of a path.
    """

    folders = sorted(((len(path), _folder_pattern(path), project_id) for project_id, _, path in known if path),
                     key=lambda item: item[0], reverse=True)
    counts: dict[str, int] = {}
    for text in inputs:
        normal = _normal(text)
        touched: set[str] = set()
        taken: list[tuple[int, int]] = []
        for _, pattern, project_id in folders:
            for match in pattern.finditer(normal):
                if not any(low <= match.start() < high for low, high in taken):
                    taken.append(match.span())
                    touched.add(project_id)
        for project_id in touched:
            counts[project_id] = counts.get(project_id, 0) + 1
    return counts


def _folder_pattern(path: str) -> re.Pattern[str]:
    """A folder, as a pattern that ends at a path boundary; on Windows each part may be its short name."""

    separator = "\\" if os.name == "nt" else "/"
    parts = _normal(path).split(separator)
    short = short_path(path)
    short_parts = _normal(short).split(separator) if short else parts
    if len(short_parts) != len(parts):
        short_parts = parts
    pieces = [re.escape(part) if part == other else f"(?:{re.escape(part)}|{re.escape(other)})"
              for part, other in zip(parts, short_parts)]
    return re.compile(re.escape(separator).join(pieces) + r"(?![\w.-])")


def prune(*, now: float | None = None) -> None:
    """Forget the state of chats not used for a month."""

    moment = time.time() if now is None else now
    try:
        for path in folder().glob("*.json"):
            try:
                if moment - path.stat().st_mtime > KEPT_DAYS * 86400:
                    path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _normal(text: str) -> str:
    """Paths as Windows writes them, whatever the shell: C:\\x, C:/x, /c/x, and /mnt/c/x are the same folder."""

    if os.name != "nt":
        return text
    value = text.casefold().replace("\\\\", "\\").replace("/", "\\")
    value = re.sub(r"\\mnt\\([a-z])\\", r"\1:\\", value)
    return re.sub(r"(?<![\w:.\\])\\([a-z])\\", r"\1:\\", value)


def log_size(transcript: str | None) -> int:
    if not transcript:
        return 0
    try:
        return Path(transcript).stat().st_size
    except OSError:
        return 0
