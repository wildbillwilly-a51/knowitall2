"""Read Claude Code and Codex session logs into the events the learner may use.

Only the user's own messages, the assistant's visible text, and tool calls
with their results are kept. Thinking, harness attachments and meta records,
system reminders, task notifications, compaction summaries, side-chain
(helper agent) records, and KnowItAll2's own tool calls are dropped. Reports
that helper agents deliver through the user channel are labeled as agent
reports, never as user words.
Codex helper-agent and non-interactive sessions are not learned from.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_HARNESS_MARKERS = (
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "Caveat: The messages below were generated",
)
_AGENT_REPORT_MARKERS = ("[Subagent hand-back]", "<agent-message", "Another Claude session sent a message")
# A block the app wrapped in a tag of its own, such as <pasted_content id=...>...</pasted_content>.
_TAGGED = re.compile(r"<([a-z][a-z0-9_-]*)(?:\s[^<>]*)?>.*?</\1>", re.DOTALL)
_INTERRUPTED = re.compile(r"\[Request interrupted by user[^\]\n]*\]")
# A compacted chat goes on from a summary written as a user message. The summary can repeat anything the chat
# read, so it is never the user's own words; what it summarizes is earlier in the same log.
_COMPACTION_SUMMARY = "This session is being continued from a previous conversation"
_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
_PATH_ONLY_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"})
# Files whose contents are knowledge in words, not code or data.
DOCUMENT_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc")
# The Read tool numbers each line ("12<tab>text"); the number is not part of the document.
_LINE_NUMBERS = re.compile(r"(?m)^[ \t]*\d+\t")


def is_document(path: str) -> bool:
    return path.casefold().endswith(DOCUMENT_SUFFIXES)
_SKIPPED_TOOLS = frozenset({"ToolSearch", "TodoWrite", "TaskStop", "SubagentHandback", "Skill", "EnterPlanMode", "ExitPlanMode"})
_WEB_TOOLS = frozenset({"WebSearch", "WebFetch"})
_OWN_TOOL_PREFIX = "mcp__knowitall2__"


@dataclass(frozen=True)
class Event:
    kind: str  # "user", "assistant", "agent_report", or "tool"
    text: str
    tool: str | None = None
    output: str | None = None
    end: int = 0  # byte offset just past the log record that produced this event
    cwd: str | None = None  # the folder a command ran in, when the log records it apart from the command


@dataclass
class Session:
    agent: str
    session_id: str
    path: Path
    cwd: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    events: list[Event] = field(default_factory=list)


def claude_code_log_root() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return base / "projects"


def claude_code_logs(root: Path | None = None) -> list[Path]:
    """Main session logs, newest first. Helper-agent logs live in subfolders and are skipped."""

    return [path for _, path in claude_code_listing(root)]


def claude_code_listing(root: Path | None = None) -> list[tuple[float, Path]]:
    """Each main session log with the time it was last written, newest first."""

    folder = root if root is not None else claude_code_log_root()
    found: list[tuple[float, Path]] = []
    for project in _scan(folder)[1]:
        found += _scan(project, _is_claude_code_log)[0]
    return _newest_first(found)


def codex_log_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return base / "sessions"


def codex_logs(root: Path | None = None) -> list[Path]:
    """Codex session logs (``sessions/YYYY/MM/DD/rollout-*.jsonl``), newest first."""

    return [path for _, path in codex_listing(root)]


def codex_listing(root: Path | None = None) -> list[tuple[float, Path]]:
    """Each Codex session log with the time it was last written, newest first."""

    found: list[tuple[float, Path]] = []
    waiting = [root if root is not None else codex_log_root()]
    while waiting:
        files, folders = _scan(waiting.pop(), _is_rollout)
        found += files
        waiting += folders
    return _newest_first(found)


def recent_codex_listing(root: Path | None = None, *, days: int = 2) -> list[tuple[float, Path]]:
    """The Codex logs in the date folders of the last ``days`` days, newest first.

    Codex files each session under the day it started, so this finds a
    current session without reading every log there ever was; a session that
    began earlier is only in ``codex_listing``.
    """

    folder = root if root is not None else codex_log_root()
    now = datetime.now(timezone.utc)
    days_wanted = {moment.strftime("%Y/%m/%d") for back in range(days)
                   for moment in (now - timedelta(days=back), (now - timedelta(days=back)).astimezone())}
    found: list[tuple[float, Path]] = []
    for day in sorted(days_wanted):
        found += _scan(folder.joinpath(*day.split("/")), _is_rollout)[0]
    return _newest_first(found)


def folder_listing(folder: Path) -> list[tuple[float, Path]]:
    """The session logs directly in ``folder`` with the time each was last written, newest first."""

    return _newest_first(_scan(folder, lambda name: name.endswith(".jsonl"))[0])


def session_logs() -> list[Path]:
    """Every agent's session logs, newest first."""

    return [path for _, path in _newest_first([*claude_code_listing(), *codex_listing()])]


def is_codex_log(path: Path) -> bool:
    return path.name.startswith("rollout-")


def read_session(path: Path, *, start: int = 0, stop: int | None = None) -> tuple[Session, int]:
    """Read a session log from byte offset ``start``, and no further than the line that reaches ``stop``."""

    reader = read_codex_session if is_codex_log(path) else read_claude_code_session
    return reader(path, start=start, stop=stop)


def _is_claude_code_log(name: str) -> bool:
    return name.endswith(".jsonl")


def _is_rollout(name: str) -> bool:
    return name.startswith("rollout-") and name.endswith(".jsonl")


def _scan(folder: Path, wanted: Callable[[str], bool] | None = None) -> tuple[list[tuple[float, Path]], list[Path]]:
    """The wanted files in ``folder`` with the time each was last written, and its subfolders.

    One directory listing gives both; on Windows it also carries each file's
    times, so this reads no file. A file deleted meanwhile is left out, and a
    missing or unreadable folder is empty.
    """

    files: list[tuple[float, Path]] = []
    folders: list[Path] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):  # a linked folder could lead back here
                        folders.append(Path(entry.path))
                    elif wanted is not None and wanted(entry.name) and entry.is_file():
                        files.append((entry.stat().st_mtime, Path(entry.path)))
                except OSError:
                    continue  # deleted since the folder was listed
    except OSError:
        pass
    return files, folders


def _newest_first(found: list[tuple[float, Path]]) -> list[tuple[float, Path]]:
    return sorted(found, key=lambda item: item[0], reverse=True)


def read_claude_code_session(path: Path, *, start: int = 0, stop: int | None = None) -> tuple[Session, int]:
    """Parse ``path`` from byte offset ``start``; return the session and the offset read to.

    A trailing line without a newline may still be being written, so it is
    left for the next read.
    """

    session = Session(agent="claude-code", session_id=path.stem, path=path)
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    offset = start
    with path.open("rb") as stream:
        stream.seek(start)
        for raw in stream:
            if not raw.endswith(b"\n") or (stop is not None and offset >= stop):
                break
            offset += len(raw)
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                before = len(session.events)
                _consume(record, session, pending)
                for index in range(before, len(session.events)):
                    session.events[index] = replace(session.events[index], end=offset)
    return session, offset


def _consume(record: dict[str, Any], session: Session, pending: dict[str, tuple[str, dict[str, Any]]]) -> None:
    kind = record.get("type")
    if kind not in {"user", "assistant"} or record.get("isSidechain") or record.get("isMeta"):
        return
    if record.get("isApiErrorMessage") or record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
        return
    if session.cwd is None and isinstance(record.get("cwd"), str):
        session.cwd = record["cwd"]
    if isinstance(record.get("sessionId"), str):
        session.session_id = record["sessionId"]
    timestamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
    if timestamp:
        session.started_at = session.started_at or timestamp
        session.ended_at = timestamp
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if kind == "assistant":
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = _clean(block.get("text"))
                if text:
                    session.events.append(Event("assistant", text))
            elif block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                name = str(block.get("name") or "tool")
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                pending[block["id"]] = (name, arguments)
        return
    if isinstance(content, str):
        _add_user_text(session, content)
        return
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            _add_user_text(session, block.get("text"))
        elif block.get("type") == "tool_result":
            call = pending.pop(str(block.get("tool_use_id")), None)
            if call is not None:
                event = _tool_event(call[0], call[1], _result_text(block.get("content")))
                if event is not None:
                    session.events.append(event)


def _add_user_text(session: Session, value: object) -> None:
    text = _clean(value)
    if not text or any(marker in text for marker in _HARNESS_MARKERS) or text.startswith(_COMPACTION_SUMMARY):
        return
    if any(marker in text for marker in _AGENT_REPORT_MARKERS):
        session.events.append(Event("agent_report", text))
        return
    # What the app wraps in tags (pasted text, which may hold words the user did not write; a page or file the
    # user had open; a shell command's output) and its interrupt notices are not the user's words; what the
    # user typed around them is.
    text = _INTERRUPTED.sub("", _TAGGED.sub("", text)).strip()
    if text:
        session.events.append(Event("user", text))


def _tool_event(name: str, arguments: dict[str, Any], output: str) -> Event | None:
    if name.startswith(_OWN_TOOL_PREFIX) or name in _SKIPPED_TOOLS:
        return None
    if name in _SHELL_TOOLS:
        return Event("tool", str(arguments.get("command") or ""), tool=name, output=output)
    if name in _PATH_ONLY_TOOLS:
        path = arguments.get("file_path") or arguments.get("notebook_path") or ""
        if name == "Read" and is_document(str(path)):
            # A document the agent read (a handoff, state file, or notes) is knowledge; code stays path only.
            return Event("tool", str(path), tool=name, output=_LINE_NUMBERS.sub("", output))
        return Event("tool", str(path), tool=name)
    if name in _WEB_TOOLS:
        return Event("tool", str(arguments.get("query") or arguments.get("url") or ""), tool=name)
    if name in {"Grep", "Glob"}:
        target = " in ".join(str(part) for part in (arguments.get("pattern"), arguments.get("path")) if part)
        return Event("tool", target, tool=name, output=output)
    if name == "Agent":
        return Event("tool", str(arguments.get("description") or ""), tool=name, output=output)
    compact = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return Event("tool", compact, tool=name, output=output)


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return _clean(content)
    if isinstance(content, list):
        parts = [str(item.get("text")) for item in content if isinstance(item, dict) and item.get("type") == "text"]
        return _clean("\n".join(parts))
    return ""


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _SYSTEM_REMINDER.sub("", value).strip()


# Codex ------------------------------------------------------------------------

# MCP servers whose calls are memory lookups, not work: KnowItAll2 itself.
_CODEX_OWN_SERVERS = frozenset({"knowitall2"})
# A message another Codex session delegated arrives as a user message; it is not the user's words.
_CODEX_DELEGATION = "<codex_delegation>"
_CODEX_SESSION_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
_CODEX_SHELL_FLAGS = frozenset({"-Command", "-command", "-c", "-lc", "/C", "/c"})
_CODEX_META_LINES = 20


def read_codex_session(path: Path, *, start: int = 0, stop: int | None = None) -> tuple[Session, int]:
    """Parse a Codex rollout from byte offset ``start``; return the session and the offset read to.

    Codex records each finished step as an ``item_completed`` event, which is
    all the learner needs. Helper-agent (subagent) and non-interactive
    (``exec``) sessions yield no events.
    """

    match = _CODEX_SESSION_ID.search(path.name)
    session = Session(agent="codex", session_id=match.group(1) if match else path.stem, path=path)
    meta = _codex_meta(path)
    if isinstance(meta.get("id"), str):
        session.session_id = meta["id"]
    if isinstance(meta.get("cwd"), str):
        session.cwd = meta["cwd"]
    source = meta.get("source")
    learnable = not (isinstance(source, dict) or source == "exec")
    offset = start
    with path.open("rb") as stream:
        stream.seek(start)
        for raw in stream:
            if not raw.endswith(b"\n") or (stop is not None and offset >= stop):
                break
            offset += len(raw)
            if not learnable or (b'"item_completed"' not in raw and b'"turn_context"' not in raw):
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict):
                before = len(session.events)
                _consume_codex(record, session)
                for index in range(before, len(session.events)):
                    session.events[index] = replace(session.events[index], end=offset)
    return session, offset


def session_folder(path: Path) -> str | None:
    """The folder a session worked in, read cheaply from the start of its log."""

    if is_codex_log(path):
        cwd = _codex_meta(path).get("cwd")
        return cwd if isinstance(cwd, str) and cwd else None
    try:
        with path.open("rb") as stream:
            for _, raw in zip(range(50), stream):
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("cwd"), str) and record["cwd"]:
                    return record["cwd"]
    except OSError:
        pass
    return None


def _codex_meta(path: Path) -> dict[str, Any]:
    """The session's ``session_meta`` payload, which Codex writes first."""

    try:
        with path.open("rb") as stream:
            for _, raw in zip(range(_CODEX_META_LINES), stream):
                if b'"session_meta"' not in raw:
                    continue
                record = json.loads(raw)
                payload = record.get("payload") if isinstance(record, dict) else None
                return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def _consume_codex(record: dict[str, Any], session: Session) -> None:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if record.get("type") == "turn_context":
        if session.cwd is None and isinstance(payload.get("cwd"), str):
            session.cwd = payload["cwd"]
        return
    if record.get("type") != "event_msg" or payload.get("type") != "item_completed":
        return
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    timestamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
    if timestamp:
        session.started_at = session.started_at or timestamp
        session.ended_at = timestamp
    event = _codex_event(item)
    if event is not None:
        session.events.append(event)


def _codex_event(item: dict[str, Any]) -> Event | None:
    kind = item.get("type")
    if kind == "UserMessage":
        text = _codex_text(item.get("content"))
        if not text:
            return None
        if text.startswith(_CODEX_DELEGATION):
            return Event("agent_report", text)
        if text.startswith(("<", _COMPACTION_SUMMARY)) or any(marker in text for marker in _HARNESS_MARKERS):
            return None  # injected context, such as <environment_context>
        return Event("user", text)
    if kind == "AgentMessage":
        text = _codex_text(item.get("content"))
        return Event("assistant", text) if text else None
    if kind == "CommandExecution":
        output = item.get("aggregated_output")
        if not isinstance(output, str):
            output = "\n".join(part for part in (item.get("stdout"), item.get("stderr")) if isinstance(part, str))
        exit_code = item.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            output = f"(exit code {exit_code})\n{output}"
        folder = item.get("cwd") if isinstance(item.get("cwd"), str) else None
        return Event("tool", _codex_command(item.get("command")), tool="shell", output=_clean(output), cwd=folder)
    if kind == "McpToolCall":
        server = str(item.get("server") or "")
        if server in _CODEX_OWN_SERVERS:
            return None
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        return Event(
            "tool", json.dumps(arguments, ensure_ascii=False, sort_keys=True), tool=f"{server}.{item.get('tool')}",
            output=_result_text(result.get("content")),
        )
    if kind == "FileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
        described = [
            f"{change.get('type', 'change')} {name}" if isinstance(change, dict) else str(name)
            for name, change in changes.items()
        ]
        return Event("tool", "; ".join(described), tool="FileChange") if described else None
    if kind == "WebSearch":
        query = item.get("query")
        return Event("tool", query, tool="WebSearch") if isinstance(query, str) and query else None
    return None


def _codex_text(content: object) -> str:
    if isinstance(content, str):
        return _clean(content)
    if not isinstance(content, list):
        return ""
    parts = [str(part.get("text")) for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return _clean("\n".join(parts))


def _codex_command(command: object) -> str:
    """The script a shell was asked to run, or the command line itself."""

    if isinstance(command, list):
        words = [str(word) for word in command]
        if len(words) >= 3 and words[-2] in _CODEX_SHELL_FLAGS:
            return words[-1]
        return " ".join(words)
    return str(command or "")
