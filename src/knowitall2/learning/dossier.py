"""Turn a session into bounded, redacted text chunks for fact extraction."""

from __future__ import annotations

import hashlib
import re
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
    body_start: int = 0
    end_offset: int = 0  # log offset just past this dossier's last event, for resuming
    known: list = field(default_factory=list)  # related existing memories shown to the model
    tool_inputs: list[str] = field(default_factory=list)  # commands, paths, and folders its tool calls used
    worked_elsewhere: bool = False  # filed under the project its work was in, not the session folder's

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

    if len(session.events) < 2 or (require_user and not any(event.kind == "user" for event in session.events)):
        return []
    project = identify(session.cwd) if session.cwd else None
    header = _header(session, project)
    dossiers: list[Dossier] = []
    current = _new_dossier(session, project, header, 0)
    for event in session.events:
        entry, user_text, output = _render(event)
        if not entry:
            continue
        if len(current.text) + len(entry) > budget and current.text != header:
            dossiers.append(current)
            current = _new_dossier(session, project, header, len(dossiers))
        current.text += entry
        current.end_offset = max(current.end_offset, event.end)
        if user_text:
            current.user_texts.append(user_text)
        if output:
            current.tool_outputs.append(output)
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


def _excerpt(text: str, limit: int) -> str:
    compact = text.strip()
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."


def _head_and_tail(text: str, limit: int) -> str:
    compact = text.strip()
    if len(compact) <= limit:
        return compact
    half = (limit - 20) // 2
    return compact[:half].rstrip() + "\n  [...]\n  " + compact[-half:].lstrip()
