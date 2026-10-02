"""Learn durable memories from finished session logs, in the background.

A failure stays with its own session: it is retried on later runs and skipped
after repeated failures, so one bad log can never block the rest or pause
learning.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .. import journal, review
from ..memory import KINDS, VERIFICATION_FOR_SOURCE, Memory, MemoryInputError, keywords, may_supersede
from ..review import UNCONFIRMED_RULE_PREFIX
from ..secrets import contains_secret
from ..store import RecordRow
from .dossier import Dossier, build_dossiers, file_by_work
from .extractor import ExtractionError, Extractor
from .state import MAX_FAILURES, LearnerSettings, LearnerState, day_ago
from .transcripts import read_session

MIN_TEXT_CHARACTERS = 20
# The model is asked for under 500 characters and to split larger knowledge; a little over is still kept.
MAX_TEXT_CHARACTERS = 1000
# Letters and digits a quote needs, so a word or two cannot count as evidence.
MIN_QUOTE_LETTERS = 12
LEARNER_AGENT = "learner"
KNOWN_LIMIT = 40
# The project's newest memories, plus those of it and the global ones that match the excerpt's words.
KNOWN_PROJECT_RECENT = 12
KNOWN_KEYWORDS = 24


@dataclass
class ReadySession:
    session_id: str
    log: Path
    dossiers: int
    characters: int
    duplicates: int = 0


@dataclass
class LearnReport:
    logs: int = 0
    active: int = 0
    ready: list[ReadySession] = field(default_factory=list)
    calls: int = 0
    deferred: int = 0
    outcomes: Counter = field(default_factory=Counter)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    blocked: str | None = None
    usage: Counter = field(default_factory=Counter)
    # Each candidate's outcome, with its text (none for one that held a secret) and memory ids.
    results: list[dict[str, Any]] = field(default_factory=list)

    def describe(self, *, dry_run: bool) -> str:
        lines = [
            f"Session logs found: {self.logs}; still active: {self.active}; ready to learn from: {len(self.ready)}.",
        ]
        for item in self.ready:
            repeat = f", {item.duplicates} identical to content already learned" if item.duplicates else ""
            lines.append(f"- {item.session_id}: {item.dossiers} dossier(s), {item.characters:,} characters{repeat}")
        if dry_run:
            lines.append("Dry run: no model calls were made and nothing was saved.")
            return "\n".join(lines)
        lines.append(f"Model calls: {self.calls}; sessions deferred by the budget: {self.deferred}.")
        if self.outcomes:
            lines.append("Candidates: " + ", ".join(f"{name} {count}" for name, count in sorted(self.outcomes.items())))
        if self.failed:
            lines.append("Will retry later: " + ", ".join(self.failed))
        if self.skipped:
            lines.append(f"Skipped after {MAX_FAILURES} failures: " + ", ".join(self.skipped))
        if self.blocked:
            lines.append(f"Learning stopped, and no session was marked as failed: {self.blocked}")
        return "\n".join(lines)


def learn(
    *,
    logs: list[Path],
    state: LearnerState,
    settings: LearnerSettings,
    memory_factory: Callable[[], Memory] | None,
    extractor: Extractor | None,
    dry_run: bool,
    now: datetime | None = None,
    run: str | None = None,
    right_away: bool = False,
) -> LearnReport:
    """Learn from each finished log; ``run`` labels the journal entries of this run.

    ``right_away`` learns the given logs even while their sessions are still
    active, for learning at the moment something happens (a commit, the end
    of a session, the user asking). What it reads is not read again later.
    """

    moment = now or datetime.now(timezone.utc)
    idle_since = moment - timedelta(minutes=settings.idle_minutes)
    calls_today = state.calls_since(day_ago(moment))
    report = LearnReport(logs=len(logs))
    memory = None if dry_run or memory_factory is None else memory_factory()
    run_seen: set[str] = set()
    for log in logs:
        try:
            status = log.stat()
        except OSError:
            continue
        entry = state.entry(log)
        if status.st_size <= int(entry.get("offset", 0)):
            continue
        if not right_away and datetime.fromtimestamp(status.st_mtime, tz=timezone.utc) > idle_since:
            report.active += 1
            continue
        start = int(entry.get("offset", 0))
        session, end = read_session(log, start=start)
        dossiers = build_dossiers(session, require_user=start == 0)
        if memory is not None:
            # What was learned belongs to the project the work was in, not always the chat's folder.
            file_by_work(dossiers, known=memory.store.all_project_paths(), resolve=memory.stored_project,
                         started=session.started_at, ended=session.ended_at)
        duplicates = [item for item in dossiers if state.seen(item.fingerprint) or item.fingerprint in run_seen]
        run_seen.update(item.fingerprint for item in dossiers)
        report.ready.append(ReadySession(
            session.session_id, log, len(dossiers), sum(len(item.text) for item in dossiers), len(duplicates),
        ))
        if dry_run:
            continue
        if not dossiers:
            _finish(entry, session.session_id, end, moment)
            continue
        if extractor is None:
            raise ExtractionError("no extractor is configured")
        budget = min(settings.max_calls_per_run - report.calls, settings.max_calls_per_day - calls_today - report.calls)
        progress: int | None = None
        completed = True
        try:
            for dossier in dossiers:
                if any(dossier is item for item in duplicates):
                    report.outcomes["duplicate dossier"] += 1
                    progress = dossier.end_offset
                    continue
                if budget <= 0:
                    completed = False
                    break
                assert memory is not None
                dossier.known = known_context(memory, dossier)
                try:
                    candidates = extractor.extract(dossier)
                finally:
                    report.usage.update(getattr(extractor, "last_usage", None) or {})
                budget -= 1
                report.calls += 1
                state.record_call(at=moment, session=session.session_id, outcome="ok")
                for candidate in candidates:
                    outcome, record_ids = publish_with_ids(memory, candidate, dossier, run=run)
                    report.outcomes[outcome] += 1
                    report.results.append({
                        "outcome": outcome, "ids": record_ids,
                        "text": None if outcome == "rejected (secret)" else str(candidate.get("text") or ""),
                    })
                state.mark_seen(dossier.fingerprint)
                progress = dossier.end_offset
        except ExtractionError as exc:
            if progress is not None:
                entry["offset"] = progress
            if exc.blocking:
                # The backend itself is unusable: keep progress, blame no session, stop the run.
                report.blocked = str(exc)
                break
            journal.problem("learning", f"a learning call failed for session {session.session_id}: {exc}")
            report.calls += 1
            state.record_call(at=moment, session=session.session_id, outcome="failed")
            entry["failures"] = int(entry.get("failures", 0)) + 1
            entry["last_error"] = str(exc)[:300]
            entry["updated_at"] = moment.isoformat()
            if entry["failures"] >= MAX_FAILURES:
                entry["status"] = "skipped"
                entry["offset"] = end
                report.skipped.append(session.session_id)
            else:
                entry["status"] = "failed"
                report.failed.append(session.session_id)
            continue
        if completed:
            _finish(entry, session.session_id, end, moment)
            continue
        # The budget ran out part-way: keep what was learned and continue next run.
        if progress is not None:
            entry["offset"] = progress
        entry.update({"session_id": session.session_id, "status": "partial", "updated_at": moment.isoformat()})
        report.deferred += 1
    if not dry_run:
        state.save()
    return report


def known_context(memory: Memory, dossier: Dossier) -> list[RecordRow]:
    """The existing memories the model sees: this project's, plus global ones about the same things."""

    known: list[RecordRow] = []
    if dossier.project is not None:
        known.extend(memory.store.list_active(
            project_id=dossier.project.id, scope="project", limit=KNOWN_PROJECT_RECENT,
        ))
    terms = keywords(dossier.text[dossier.body_start:], limit=KNOWN_KEYWORDS)
    if not terms:
        return known
    seen = {record.id for record in known}
    match = " OR ".join(f'"{term}"' for term in terms)
    # The project's older memories too, when they are about the same things, so they are not learned again.
    project_id = dossier.project.id if dossier.project is not None else None
    for row, _rank in memory.store.search(match, project_id=project_id, scope="all", limit=KNOWN_LIMIT):
        if len(known) >= KNOWN_LIMIT:
            break
        if row.id not in seen:
            known.append(row)
            seen.add(row.id)
    return known


def publish(memory: Memory, candidate: dict[str, Any], dossier: Dossier, *, run: str | None = None) -> str:
    """Validate one candidate and save it; returns the outcome name for the report.

    A candidate that updates or contradicts a known memory replaces it only when
    its evidence is at least as strong; the user's own statements give way only
    to the user's own words. Otherwise both are kept and the user is asked.
    Every candidate, kept or not, is recorded in the journal with the reason.
    """

    return publish_with_ids(memory, candidate, dossier, run=run)[0]


def publish_with_ids(memory: Memory, candidate: dict[str, Any], dossier: Dossier, *,
                     run: str | None = None) -> tuple[str, list[str]]:
    """``publish``, also returning the ids of the memories it saved or touched."""

    outcome, record_ids = _publish(memory, candidate, dossier)
    _record_candidate(memory, candidate, dossier, outcome, record_ids, run)
    return outcome, record_ids


def _record_candidate(
    memory: Memory, candidate: dict[str, Any], dossier: Dossier, outcome: str, record_ids: list[str], run: str | None,
) -> None:
    reason = outcome[len("rejected ("):-1] if outcome.startswith("rejected (") else None
    details: dict[str, Any] = {"result": outcome}
    if reason == "secret":
        # Keep nothing of a candidate that held a secret.
        summary = "A candidate that contained a secret; nothing of it was kept."
    else:
        summary = str(candidate.get("text") or "(no text)")
        details.update({
            "kind": candidate.get("kind"), "scope": candidate.get("scope"), "subjects": candidate.get("subjects"),
            "evidence": candidate.get("evidence"), "relation": candidate.get("relation"),
            "known_id": candidate.get("known_id"),
        })
    if reason:
        details["reason"] = reason
    journal.record(
        memory.store, "candidate", summary, outcome="rejected" if reason else outcome, agent=dossier.agent,
        project_id=dossier.project.id if dossier.project else None, session=dossier.session_id, run=run,
        record_ids=record_ids, details=details, at=memory.now(),
    )


def _publish(memory: Memory, candidate: dict[str, Any], dossier: Dossier) -> tuple[str, list[str]]:
    checked, reason = validate(candidate, dossier)
    if checked is None:
        return f"rejected ({reason})", []
    older = _changed_memory(memory, checked, dossier)
    replaces = None
    if older is not None and may_supersede(older.verification, VERIFICATION_FOR_SOURCE[checked["source"]]):
        replaces, older = older.id, None
    try:
        result = memory.remember(
            checked["text"],
            kind=checked["kind"],
            subjects=checked["subjects"],
            scope=checked["scope"],
            source=checked["source"],
            replaces=replaces,
            project_path=dossier.cwd,
            project=dossier.project if dossier.worked_elsewhere else None,
            session=dossier.session_id,
        )
    except MemoryInputError:
        return "rejected (not accepted by memory)", []
    ids = [result.record.id] + ([replaces] if replaces else [])
    if result.status == "already_known":
        return "already known", ids
    if older is not None:
        review.ask_conflict(memory, older, result.record)
        return "saved with a question", ids + [older.id]
    if checked["proposed_rule"]:
        review.ask_rule(memory, result.record)
        return "saved with a question", ids
    return ("updated" if replaces else "saved"), ids


def _changed_memory(memory: Memory, checked: dict[str, Any], dossier: Dossier) -> RecordRow | None:
    """The known memory a candidate updates or contradicts, if the claim holds up."""

    if checked["relation"] not in ("updates", "contradicts") or checked["known_id"] not in dossier.known_ids:
        return None
    record = memory.store.get(checked["known_id"])
    return record if record is not None and record.status == "active" else None



def validate(candidate: dict[str, Any], dossier: Dossier) -> tuple[dict[str, Any] | None, str | None]:
    """Deterministic checks the model cannot talk its way past."""

    text = " ".join(str(candidate.get("text") or "").split())
    kind = candidate.get("kind")
    scope = candidate.get("scope")
    evidence = " ".join(str(candidate.get("evidence") or "").split())
    subjects = candidate.get("subjects") if isinstance(candidate.get("subjects"), list) else []
    subjects = [" ".join(str(item).split())[:80] for item in subjects if str(item).strip()][:8]
    if not MIN_TEXT_CHARACTERS <= len(text) <= MAX_TEXT_CHARACTERS:
        return None, "length"
    if kind not in KINDS:
        return None, "kind"
    if scope not in ("global", "project"):
        return None, "scope"
    if scope == "project" and dossier.project is None:
        scope = "global"
    if contains_secret(" ".join([text, evidence, *subjects])):
        return None, "secret"
    if not _quoted_in(evidence, [dossier.text]) and not _nearly_quoted_in(evidence, [dossier.text]):
        # Every memory must rest on the session itself: not on the known-memory
        # list, and not on context an engine adds, such as the user's AGENTS.md.
        # A near quote (a few words re-typed) counts, but only an exact one below
        # makes a memory observed or the user's own words, or makes a rule.
        return None, "evidence not in the session"
    source = "inferred"
    proposed_rule = False
    if kind == "rule":
        if _quoted_in(evidence, dossier.user_texts):
            source = "user"
        elif _nearly_quoted_in(evidence, dossier.user_texts):
            # The user's words, re-typed: ask the user whether it is their rule.
            kind, text, proposed_rule = "note", UNCONFIRMED_RULE_PREFIX + text, True
        else:
            # An instruction an agent wrote, not the user: a note, never a question the user cannot answer.
            kind = "note"
    elif _quoted_in(evidence, dossier.tool_outputs):
        source = "observed"
    elif _quoted_in(evidence, dossier.user_texts):
        source = "user"
    relation = candidate.get("relation") if candidate.get("relation") in ("updates", "contradicts") else "new"
    known_id = str(candidate.get("known_id") or "").strip().strip("[]")
    return {
        "text": text, "kind": kind, "scope": scope, "subjects": subjects, "source": source,
        "proposed_rule": proposed_rule, "relation": relation, "known_id": known_id,
    }, None


def _quoted_in(quote: str, sources: list[str]) -> bool:
    """Whether ``quote`` appears in one of ``sources``.

    Quotes are compared by their letters and digits alone, because models
    re-type punctuation: typographic apostrophes become straight ones, and
    diff markers, comment signs, and escaping disappear. A quote shortened
    with "..." counts when each piece appears, in order. Paraphrases and
    quotes stitched together from separate places still do not match.
    """

    pieces = [piece for piece in (_letters(part) for part in _ELISION.split(quote)) if piece]
    if sum(len(piece) for piece in pieces) < MIN_QUOTE_LETTERS:
        return False
    for source in sources:
        text = _letters(source)
        position = 0
        for piece in pieces:
            found = text.find(piece, position)
            if found < 0:
                break
            position = found + len(piece)
        else:
            return True
    return False


_ELISION = re.compile(r"\.{3,}|…|\[\.\.\.\]")
# A near quote: this share of its words, in order, within one stretch of the source.
NEAR_QUOTE_SHARE = 0.85
_NEAR_ANCHORS = 3
_NEAR_POSITIONS = 60
_NEAR_SLACK = 3


def _nearly_quoted_in(quote: str, sources: list[str], *, share: float = NEAR_QUOTE_SHARE) -> bool:
    """Whether ``quote`` appears in one of ``sources`` with a few words re-typed, dropped, or added.

    Models copy long passages imperfectly: a changed word, a dropped article,
    a line break read as a space. Each piece of the quote (split at "...")
    must still match, word by word and in order, within one stretch of the
    source about as long as itself, so paraphrases and quotes stitched from
    separate places do not match.
    """

    pieces = [piece for piece in (_words(part) for part in _ELISION.split(quote)) if piece]
    if sum(len("".join(piece)) for piece in pieces) < MIN_QUOTE_LETTERS:
        return False
    for source in sources:
        words, positions = _word_index(source)
        after = 0
        for piece in pieces:
            end = _near_piece(piece, words, positions, share, after=after)
            if end is None:
                break
            after = end
        else:
            return True
    return False


def _near_piece(piece: tuple[str, ...], words: tuple[str, ...], positions: dict[str, list[int]], share: float, *,
                after: int = 0) -> int | None:
    """Where a near match of ``piece`` ends, at or past word ``after``; None when there is none."""

    anchors = sorted({word for word in piece if word in positions}, key=lambda word: len(positions[word]))
    best = None
    for anchor in anchors[:_NEAR_ANCHORS]:
        offset = piece.index(anchor)
        for position in [item for item in positions[anchor] if item - offset >= after - _NEAR_SLACK][:_NEAR_POSITIONS]:
            start = max(after, position - offset - _NEAR_SLACK)
            end = position - offset + len(piece) + _NEAR_SLACK
            blocks = SequenceMatcher(None, piece, words[start:end], autojunk=False).get_matching_blocks()
            if sum(block.size for block in blocks) >= share * len(piece):
                best = end if best is None else min(best, end)
                break
    return best


@lru_cache(maxsize=256)
def _words(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


@lru_cache(maxsize=16)
def _word_index(text: str) -> tuple[tuple[str, ...], dict[str, list[int]]]:
    words = _words(text)
    positions: dict[str, list[int]] = {}
    for index, word in enumerate(words):
        positions.setdefault(word, []).append(index)
    return words, positions


@lru_cache(maxsize=256)
def _letters(text: str) -> str:
    """Only the letters and digits of ``text``, compatibility-normalized and casefolded."""

    return "".join(character for character in unicodedata.normalize("NFKC", text).casefold() if character.isalnum())


def _finish(entry: dict[str, Any], session_id: str, end: int, moment: datetime) -> None:
    entry.update(
        {"session_id": session_id, "offset": end, "status": "done", "failures": 0, "last_error": None,
         "updated_at": moment.isoformat()}
    )
