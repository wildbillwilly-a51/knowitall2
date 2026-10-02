"""What each open chat has been given, so the message hook adds only what is new to it.

Chats in the desktop apps stay open for days, and a chat opened in one folder
often works in another project. The session-start hook briefs a chat once;
after that, when the user sends a message, the message hook adds, once each:

- the headlines of memories added since the chat's briefing, for the projects
  it works in and the systems they use; and
- a project's own table of contents, when the chat starts working in a
  project other than its folder's (found from the paths its tools used).

A chat's state is one small JSON file in ``chats`` in the data home, named by
the chat's session id (or its log's path):

- ``since``: when the chat was briefed; memories added later are new to it;
- ``projects``: the projects it works in;
- ``told``: memories it has seen (in its briefing, or anywhere in its log);
- ``log`` and ``offset``: how far into its log the hook has read.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .files import short_path, write_text_atomic
from .paths import data_home

KEPT_DAYS = 30
NEW_SHOWN = 3
# Tool calls in one stretch of a chat that touch another project's folder before
# the chat counts as working there; one passing look is not enough.
WORK_CALLS = 3
READ_LIMIT = 4 * 1024 * 1024
# A chat that began before this version: how far back to look for what it saw and where it worked.
CATCH_UP_READ = 1024 * 1024
TOLD_KEPT = 2000
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
        state = json.loads((folder() / f"{chat}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def save(chat: str, state: dict[str, Any]) -> None:
    state["told"] = list(dict.fromkeys(state.get("told") or []))[-TOLD_KEPT:]
    write_text_atomic(folder() / f"{chat}.json", json.dumps(state) + "\n")


def start(chat: str, *, since: str, project_id: str | None, told: Iterable[str], transcript: str | None) -> None:
    """A chat was just briefed (a new chat, or one resumed, cleared, or compacted): start its state afresh."""

    save(chat, {"since": since, "projects": [project_id] if project_id else [], "told": list(told),
                "log": transcript, "offset": log_size(transcript)})


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


def mentioned_since(state: dict[str, Any], transcript: str | None) -> set[str]:
    """The memory ids the chat's log mentions past where the message hook last read, leaving that place as is."""

    if not transcript or state.get("log") != transcript:
        return set()
    size = log_size(transcript)
    start = max(int(state.get("offset") or 0), size - READ_LIMIT)
    if size <= start:
        return set()
    try:
        with Path(transcript).open("rb") as stream:
            stream.seek(start)
            chunk = stream.read(size - start)
    except OSError:
        return set()
    return {match.decode("ascii") for match in _RECORD_ID_BYTES.findall(chunk)}


def _same_log(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def read_new(state: dict[str, Any], transcript: str | None) -> tuple[list[str], set[str]]:
    """What the chat's log added since the last look: its tool calls' inputs, and the memory ids it mentions.

    Moves the state's offset to the end of what was read.
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
