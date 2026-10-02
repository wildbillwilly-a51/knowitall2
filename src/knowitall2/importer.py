"""Import memories from another system, in a simple documented format.

The format is JSON Lines: one memory per line. See ``docs/import-format.md``.
Every line goes through the same checks as ``remember`` (secrets, length,
rules only from the user's words), so an import cannot bypass them. A dry run
performs the whole import inside a transaction and rolls it back, so it
reports exactly what a real import would do.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import review
from .identity import ProjectIdentity, identify, identify_remote
from .memory import Memory, MemoryInputError

PREVIEW_CHARACTERS = 110


@dataclass
class ImportReport:
    items: list[tuple[int, str, str]] = field(default_factory=list)  # line, outcome, text preview
    questions: int = 0

    @property
    def outcomes(self) -> Counter:
        return Counter(_outcome_group(outcome) for _, outcome, _ in self.items)

    def describe(self, *, dry_run: bool, verbose: bool) -> str:
        summary = ", ".join(f"{name} {count}" for name, count in sorted(self.outcomes.items())) or "nothing"
        lines = [f"{'Would import' if dry_run else 'Imported'}: {summary}; questions for you: {self.questions}."]
        for number, outcome, preview in self.items:
            if verbose or outcome.startswith("rejected"):
                lines.append(f"- line {number}: {outcome}: {preview}")
        if dry_run:
            lines.append("Dry run: nothing was saved.")
        return "\n".join(lines)


class _Rollback(Exception):
    pass


def import_file(memory: Memory, path: Path, *, dry_run: bool) -> ImportReport:
    report = ImportReport()
    try:
        with memory.store.transaction():
            _import_lines(memory, path, report)
            if dry_run:
                raise _Rollback
    except _Rollback:
        pass
    return report


def _import_lines(memory: Memory, path: Path, report: ImportReport) -> None:
    by_origin: dict[str, str] = {}
    conflicts: list[tuple[str, list[str]]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise MemoryInputError(f"Cannot read {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            report.items.append((number, "rejected (not a JSON object)", line[:PREVIEW_CHARACTERS]))
            continue
        if not isinstance(item, dict):
            report.items.append((number, "rejected (not a JSON object)", line[:PREVIEW_CHARACTERS]))
            continue
        preview = " ".join(str(item.get("text") or "").split())[:PREVIEW_CHARACTERS]
        try:
            result = _remember(memory, item)
        except MemoryInputError as exc:
            report.items.append((number, f"rejected ({_first_sentence(str(exc))})", preview))
            continue
        report.items.append((number, "imported" if result.status == "saved" else "already known", preview))
        origin = str(item.get("origin") or "").strip()
        if origin:
            by_origin[origin] = result.record.id
            others = [str(other) for other in item.get("conflicts_with") or [] if str(other).strip()]
            if others:
                conflicts.append((origin, others))
    asked: set[frozenset[str]] = set()
    for origin, others in conflicts:
        for other in others:
            pair = frozenset((by_origin.get(origin), by_origin.get(other)))
            if None in pair or len(pair) != 2 or pair in asked:
                continue
            asked.add(pair)
            records = [memory.store.get(record_id) for record_id in pair]
            if any(record is None or record.status != "active" for record in records):
                continue
            older, newer = sorted(records, key=lambda record: (record.created_at, record.id))
            if review.ask_conflict(memory, older, newer):
                report.questions += 1


def _remember(memory: Memory, item: dict[str, Any]):
    scope = item.get("scope") or "global"
    project = _project(memory, item.get("project")) if scope == "project" else None
    if scope == "project" and project is None:
        raise MemoryInputError("its project was not found; give a local path or a Git remote")
    return memory.remember(
        str(item.get("text") or ""),
        kind=item.get("kind") or "fact",
        subjects=_strings(item.get("subjects"), "subjects"),
        tags=_strings(item.get("tags"), "tags"),
        scope=scope,
        source=item.get("source") or "inferred",
        project=project,
        project_path=None if project is not None else Path.home(),
        recorded_at=_time(item.get("created_at")),
    )


def _project(memory: Memory, value: Any) -> ProjectIdentity | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    if isinstance(path, str) and path.strip() and Path(path).is_dir():
        found = identify(path)
        if found is not None:
            memory.project_for(path)
            return found
    remote = value.get("remote")
    if isinstance(remote, str) and remote.strip():
        return identify_remote(remote, value.get("name") if isinstance(value.get("name"), str) else None)
    return None


def _time(value: Any) -> str | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryInputError(f"created_at {value!r} is not an ISO 8601 time") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment > datetime.now(timezone.utc):
        raise MemoryInputError(f"created_at {value!r} is in the future")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _strings(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MemoryInputError(f"{name} must be a list of strings")
    return value


def _first_sentence(message: str) -> str:
    text = message.removeprefix("Not saved: ")
    return text.split(". ")[0].rstrip(".")[:120]


def _outcome_group(outcome: str) -> str:
    return "rejected" if outcome.startswith("rejected") else outcome
