"""Turn a session into bounded, redacted text chunks for fact extraction."""

from __future__ import annotations

import hashlib
import ntpath
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..identity import ProjectIdentity, identify
from ..secrets import redact
from .transcripts import Event, Session

MAX_DOSSIER_CHARACTERS = 48_000
USER_CHARACTERS = 2_000
ASSISTANT_CHARACTERS = 1_500
AGENT_REPORT_CHARACTERS = 3_000
COMMAND_CHARACTERS = 800
OUTPUT_CHARACTERS = 1_500
# A document the agent read keeps more: host lists and procedures sit in the middle of state files.
DOCUMENT_CHARACTERS = 6_000
_DOCUMENT_COMMAND = re.compile(
    r"(?:\bGet-Content\b|\bcat\b|\btype\b|\bhead\b|\btail\b|\bsed\s+-n\b|\bless\b|\bmore\b)[^|;&\n]{0,200}?"
    r"\.(?:md|markdown|txt|rst|adoc)\b", re.IGNORECASE)
_DOCUMENT_SEARCH = re.compile(
    r"(?:\bgrep\b|\begrep\b|\brg\b|\bSelect-String\b|\bsls\b|\bfindstr\b)[^|;&\n]{0,200}?"
    r"\.(?:md|markdown|txt|rst|adoc)\b", re.IGNORECASE)
# Tools whose output is what a command printed: Claude Code's shells, and Codex's (see transcripts).
_COMMAND_TOOLS = frozenset({"Bash", "PowerShell", "shell"})
# A command that fetches from the network, or shows what a repository's authors wrote (commits, diffs).
_FETCHES = re.compile(
    r"https?://|\b(?:curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm|Start-BitsTransfer)\b"
    r"|\bgh\s+api\b|\bgit\s+(?:show|log|diff|blame|cat-file)\b", re.IGNORECASE)
# A positive list of command shapes whose output describes system state. Unknown
# commands, searches, pipelines and scripts remain unverified: absence from a
# blacklist is never proof of provenance.
_PRINTERS = frozenset({"cat", "type", "get-content", "gc", "head", "tail", "less", "more", "bat", "tac", "nl"})
# Complete argv shapes: unknown arguments can echo literals, select authored
# content or invoke a formatter. A command name alone is not a trust boundary.
_STATUS_SHAPES = {
    "uname": {(), ("-a",), ("-r",)}, "uptime": {()}, "hostname": {()},
    "whoami": {()}, "id": {()}, "ls": {()}, "df": {(), ("-h",)},
    "free": {(), ("-h",)}, "ss": {(), ("-lnt",), ("-lntp",)},
    "get-service": {()}, "get-process": {()}, "get-computerinfo": {()},
    "get-netipaddress": {()}, "get-date": {()},
    "git": {("status",), ("status", "--short"), ("status", "--porcelain"),
            ("rev-parse", "HEAD"), ("rev-parse", "--show-toplevel"),
            ("rev-parse", "--is-inside-work-tree"), ("ls-files",)},
    "docker": {("ps",), ("ps", "-a"), ("version",), ("info",)},
    "podman": {("ps",), ("ps", "-a"), ("version",), ("info",)},
    "ip": {("addr",), ("address",), ("route",), ("link",)},
    "ubus": {("call", "system", "board")},
}
_VERSION_COMMANDS = frozenset({"python", "python3", "git", "docker", "podman", "node", "npm"})


@dataclass
class Dossier:
    session_id: str
    agent: str
    cwd: str | None
    project: ProjectIdentity | None
    chunk: int
    text: str = ""
    user_texts: list[str] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)
    # The tool outputs that show what a command printed (see ``_trusted_output``); only these make a memory observed.
    trusted_outputs: list[str] = field(default_factory=list)
    # The same calls as (command, output): a memory whose details a command printed is observed (``learner.validate``).
    trusted_calls: list[tuple[str, str]] = field(default_factory=list)
    body_start: int = 0
    end_offset: int = 0  # log offset just past this dossier's last event, for resuming
    end_event_index: int = 0  # when inside a record, number of its events already consumed
    known: list = field(default_factory=list)  # related existing memories shown to the model
    tool_inputs: list[str] = field(default_factory=list)  # commands, paths, and folders its tool calls used
    worked_elsewhere: bool = False  # filed under the project its work was in, not the session folder's
    from_documents: bool = False  # a project's own documents, not a session: what it teaches stays with that project
    user_times: list = field(default_factory=list)  # when each of ``user_texts`` was said, as the log recorded it
    ended_at: str | None = None  # when its last event was recorded

    @property
    def known_ids(self) -> set[str]:
        return {record.id for record in self.known}

    @property
    def fingerprint(self) -> str:
        """Identifies the content regardless of which session file it came from."""

        return hashlib.sha256(self.text[self.body_start:].encode("utf-8")).hexdigest()


def build_dossiers(
    session: Session, *, budget: int = MAX_DOSSIER_CHARACTERS, require_user: bool = True,
) -> list[Dossier]:
    """Chunk a session's events into redacted dossiers; empty when nothing is worth learning.

    ``require_user`` is relaxed when continuing a session from a saved offset,
    because a later stretch of a long session may hold only tool work.
    """

    if not session.events or (require_user and
            (len(session.events) < 2 or not any(event.kind == "user" for event in session.events))):
        return []
    project = identify(session.cwd) if session.cwd else None
    header = _header(session, project)
    dossiers: list[Dossier] = []
    current = _new_dossier(session, project, header, 0)
    for position, event in enumerate(session.events):
        entry, user_text, output = _render(event)
        if not entry:
            continue
        if len(current.text) + len(entry) > budget and current.text != header:
            dossiers.append(current)
            current = _new_dossier(session, project, header, len(dossiers))
        current.text += entry
        following = session.events[position + 1] if position + 1 < len(session.events) else None
        if event.end and following is not None and following.end == event.end:
            current.end_offset, current.end_event_index = event.start, event.index + 1
        else:
            current.end_offset, current.end_event_index = event.end, 0
        current.ended_at = event.at or current.ended_at
        if user_text:
            current.user_texts.append(user_text)
            current.user_times.append(event.at)
        if output:
            current.tool_outputs.append(output)
            if _trusted_output(event, session.cwd):
                current.trusted_outputs.append(output)
                current.trusted_calls.append((redact(event.text or ""), output))
        if event.kind == "tool":
            current.tool_inputs += [part for part in (event.text, event.cwd) if part]
    if current.text != header:
        dossiers.append(current)
    return dossiers


def file_by_work(
    dossiers: Iterable[Dossier], *, known: Iterable[tuple[str, str, str]],
    resolve: Callable[[str], ProjectIdentity | None], started: str | None = None, ended: str | None = None,
) -> None:
    """File each dossier under the project its tool calls worked in, when that is not the session folder's.

    A chat opened in one folder often works in another project, so what it
    learns belongs there. A dossier moves when at least ``WORK_CALLS`` of its
    tool calls touch another known project's folder, more than touch its own.
    ``known`` holds (project id, name, folder) for every folder a project was
    seen in; ``resolve`` gives a project by its id.
    """

    from ..chats import WORK_CALLS, projects_worked_in

    known = list(known)
    for dossier in dossiers:
        counts = projects_worked_in(dossier.tool_inputs, known)
        if not counts:
            continue
        own = dossier.project.id if dossier.project is not None else None
        best, calls = max(counts.items(), key=lambda item: item[1])
        if best == own or calls < WORK_CALLS or calls <= counts.get(own, 0):
            continue
        project = resolve(best)
        if project is None:
            continue
        where = f"folder {dossier.cwd or 'unknown'}"
        span = f"{started or '?'} to {ended or '?'}"
        header = (f"Session {dossier.session_id} ({dossier.agent}) in {where}; this part of it worked in project "
                  f"{project.name} ({project.root}), so 'project' scope means project {project.name}, {span}\n\n")
        dossier.text = header + dossier.text[dossier.body_start:]
        dossier.body_start = len(header)
        dossier.project, dossier.worked_elsewhere = project, True


def _new_dossier(session: Session, project: ProjectIdentity | None, header: str, chunk: int) -> Dossier:
    return Dossier(session.session_id, session.agent, session.cwd, project, chunk, text=header, body_start=len(header))


def _header(session: Session, project: ProjectIdentity | None) -> str:
    where = f"project {project.name} ({session.cwd})" if project else f"folder {session.cwd or 'unknown'}"
    span = f"{session.started_at or '?'} to {session.ended_at or '?'}"
    return f"Session {session.session_id} ({session.agent}) in {where}, {span}\n\n"


def _render(event: Event) -> tuple[str, str | None, str | None]:
    """Return the dossier entry, the user text it holds, and the tool output it holds."""

    if event.kind == "user":
        text = _excerpt(redact(event.text), USER_CHARACTERS)
        return f"[user] {text}\n\n", text, None
    if event.kind == "assistant":
        return f"[assistant] {_excerpt(redact(event.text), ASSISTANT_CHARACTERS)}\n\n", None, None
    if event.kind == "agent_report":
        return f"[helper agent report] {_excerpt(redact(event.text), AGENT_REPORT_CHARACTERS)}\n\n", None, None
    detail = _excerpt(redact(event.text), COMMAND_CHARACTERS)
    entry = f"[tool {event.tool}] {detail}\n"
    output = None
    if event.output:
        limit = DOCUMENT_CHARACTERS if _reads_document(event) else OUTPUT_CHARACTERS
        output = _head_and_tail(redact(event.output), limit)
        entry += f"  output: {output}\n"
    return entry + "\n", None, output


def _reads_document(event: Event) -> bool:
    """Whether a tool call read a document (a handoff, state file, or notes), whose details matter."""

    if event.tool == "Read":
        return True  # only documents keep their contents (see transcripts)
    return bool(_DOCUMENT_COMMAND.search(event.text or ""))


def _trusted_output(event: Event, folder: str | None = None) -> bool:
    """Whether a tool call's output shows what a command printed, not what someone wrote.

    A document the agent read (with Read or a shell command), a search of
    files or documents, an MCP tool's answer, and a helper agent's report hold
    what a repository's authors or another model wrote, which may be wrong or
    planted, so a memory resting on them stays unverified. So does anything
    fetched from the network, and any file printed from the session's folder
    (a cloned repository's settings files, say). A file printed by its
    absolute path outside that folder, such as a system's own settings
    (``cat /etc/openwrt_release``), is what that system holds, and counts.
    """

    if event.tool not in _COMMAND_TOOLS:
        return False
    command = event.text or ""
    if _reads_document(event) or _DOCUMENT_SEARCH.search(command) or _FETCHES.search(command):
        return False
    return _trusted_command(command, event.cwd or folder)


def _trusted_command(command: str, folder: str | None, *, remote: bool = False) -> bool:
    if not command.strip() or re.search(r"[\n\r;&|()`$<>]", command):
        return False  # compound commands and expansion need a shell parser, not a provenance guess
    try:
        parser = shlex.shlex(command, posix=True)
        parser.whitespace_split, parser.escape, parser.commenters = True, "", ""
        tokens = list(parser)
    except ValueError:
        return False
    if not tokens:
        return False
    name = tokens[0].casefold()
    arguments = tokens[1:]
    if name == "ssh" and not remote:
        # Skip only options with known arity, then the destination. Quoted remote
        # commands are parsed again; arbitrary wrappers and nested ssh are not trusted.
        while arguments and arguments[0].startswith("-"):
            option = arguments.pop(0)
            if option == "-p" and arguments and arguments[0].isdigit():
                arguments.pop(0)
            elif option == "-l" and arguments and re.fullmatch(r"[A-Za-z0-9_.-]+", arguments[0]):
                arguments.pop(0)
            elif option not in {"-T", "-t", "-n", "-q"}:
                return False
        if len(arguments) < 2:
            return False
        rest = arguments[1:]
        return _trusted_command(rest[0] if len(rest) == 1 else shlex.join(rest), folder, remote=True)
    if name in _PRINTERS:
        # Bare explicit paths only; flags have different arities in each printer.
        return bool(arguments) and folder is not None and all(_external_file(value, folder) for value in arguments)
    if name in _VERSION_COMMANDS and arguments == ["--version"]:
        return True
    if name in {"ping", "getent"}:
        host = arguments[-1] if arguments else ""
        prefix = tuple(arguments[:-1])
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", host)) and (
            (name == "ping" and prefix in {("-c1",), ("-c", "1"), ("-n", "1")})
            or (name == "getent" and prefix == ("hosts",)))
    return tuple(arguments) in _STATUS_SHAPES.get(name, set())


def _external_file(path: str, folder: str) -> bool:
    """An explicit absolute file outside the project, after resolving dot segments.

    Variables, globbing and relative paths are ambiguous, including extensionless
    names. Documents remain unverified even when read from outside the project.
    """

    if re.search(r"[*?\[\]~]", path) or path.casefold().endswith((".md", ".markdown", ".txt", ".rst", ".adoc")):
        return False
    windows = bool(re.match(r"^[A-Za-z]:[\\/]|^\\\\", path))
    if not windows and not path.startswith("/"):
        return False
    normalize = ntpath.normpath if windows else posixpath.normpath
    file, inside = _comparable(normalize(path)), _comparable(normalize(folder))
    return file != inside and not (file + "/").startswith(inside + "/")


def _comparable(path: str) -> str:
    """A path in one spelling for prefix checks: forward slashes, lower case, ``/mnt/c/`` and ``/c/`` as ``c:/``."""

    text = path.replace("\\", "/").rstrip("/").lower()
    found = re.match(r"^/(?:mnt/)?([a-z])(?=/|$)", text)
    if found:
        text = f"{found.group(1)}:" + text[found.end():]
    return text


def _excerpt(text: str, limit: int) -> str:
    compact = text.strip()
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."


def _head_and_tail(text: str, limit: int) -> str:
    compact = text.strip()
    if len(compact) <= limit:
        return compact
    half = (limit - 20) // 2
    return compact[:half].rstrip() + "\n  [...]\n  " + compact[-half:].lstrip()
