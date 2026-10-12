"""Learn durable memories from finished session logs, in the background.

A failure stays with its own session: it is retried on later runs, and the
excerpt that keeps failing is skipped after repeated failures in a row, so one
bad excerpt can never block the rest of its session, other logs, or learning.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import journal, review
from ..memory import (
    KINDS, PREFERENCE_TAG, VERIFICATION_FOR_SOURCE, VERIFICATION_STRENGTH, Memory, MemoryInputError, _stem, _tokens,
    is_users_choice, keywords, may_replace_unasked, significant_words,
)
from ..quotes import _ELISION, letters, nearly_quoted_in as _nearly_quoted_in, quoted_in as _quoted_in
from ..secrets import contains_secret, redact
from ..store import RecordRow, StoreError
from .dossier import Dossier, build_dossiers, file_by_work
from .extractor import ExtractionError, Extractor
from .state import MAX_FAILURES, LearnerSettings, LearnerState, day_ago
from .transcripts import line_boundary, read_session

MIN_TEXT_CHARACTERS = 20
# The model is asked for under 500 characters and to split larger knowledge; a little over is still kept.
MAX_TEXT_CHARACTERS = 1000
# A finder's answer: this share of its significant words must be in the quoted words.
USER_WORDS_SHARE = 0.5
# A memory in the user's own words: nearly all its significant words are theirs, and every address, path, or
# command in it too, so a sentence the user typed cannot carry someone else's instruction.
USER_STATEMENT_SHARE = 0.8
# The work a preference was about, as a model may still write it in front of the user's words: "For the release
# notes, ..." (its name, without "the", becomes a subject).
_CONTEXT_CLAUSE = re.compile(r"(?:for|when|while|in|during|on)\s+(?:(?:the|a|an)\s+)?([^,;:]{3,80})[,;:]\s",
                             re.IGNORECASE)
_COMMAND_WORDS = frozenset({
    "curl", "wget", "sudo", "rm", "sh", "bash", "zsh", "powershell", "pwsh", "iex", "invoke-expression",
    "invoke-webrequest", "iwr", "irm", "chmod", "chown", "ssh", "scp", "nc", "eval", "exec",
})
# A memory a candidate says it updates or contradicts must share this much with it, or a subject.
RELATED_WORDS = 2
RELATED_SHARE = 0.25
LEARNER_AGENT = "learner"
# What some work has not done yet ("not yet deployed"): a record of the moment, not knowledge. A condition
# that is not about the work ("until the hook is trusted", "not yet in the pending list") is kept.
_WORK_STATE = re.compile(
    r"\bnot\s+yet\s+(?:been\s+)?(?:fixed|deployed|released|implemented|merged|pushed|done|finished|built|tested|"
    r"tried|connected|confirmed|verified|reviewed|applied|installed|configured|migrated|published|committed|"
    r"started|completed|resolved)\b", re.IGNORECASE)
_LASTING = re.compile(r"\bfrom\s+now\s+on\b|\balways\b|\bnever\b", re.IGNORECASE)
# A log's entry while its set-aside part is caught up: read up to here, then go on from ``RESUME_AT``.
LEARN_TO = "learn_to"
RESUME_AT = "resume_at"
# Background learning sets aside a session quiet for this long, so a backlog cannot grow forever (about 100 sessions
# waited on 2026-10-11); ``learn --catch-up`` still takes it.
SET_ASIDE_DAYS = 14
# A rule only when the user plainly made one. Anything else the user says about how they want things done is a
# preference: kept with the work it was about, weighed like any memory, never a question; how often the user
# says it again is what makes it a lasting preference (the user, 2026-10-11: a single statement in a chat must
# not become a permanent rule).
_STANDING_RULE = re.compile(
    r"\b(?:make|add|save|keep)\s+(?:it|that|this|these|those|them)\s+(?:as\s+)?"
    r"(?:a|one\s+of\s+my|my)\s+(?:standing\s+|permanent\s+)?rules?\b(?![\s-]+of[\s-]+thumb\b)",
    re.IGNORECASE,
)
# A sentence that asks ("Should we make that a rule?") makes no rule; the closing marks a question may end with.
_CLOSING_MARKS = " \t\"')]}”’»"
# Said before "make that a rule" in the same clause (looked for this many characters back), it means the opposite.
_RULE_NEGATED = re.compile(r"\b(?:not|never|dont|no\s+need\s+to)\b|n't\b", re.IGNORECASE)
_NEGATION_WINDOW = 80
# A sentence that begins with these words and goes on with an instruction; not "from now on until Friday", and
# not "going forward with the plan".
_FROM_NOW_ON = re.compile(
    r"\W*(from\s+now\s+on|going\s+forward)\b(?![\s,;:]*(?:until|till|with)\b)[\s,;:-]*\w+\W+\w", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
# What the user did not type as their own words: code, quotations, quoted lines.
_NOT_TYPED = re.compile(r"```[\s\S]*?(?:```|\Z)|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|^[ \t]*>[^\n]*", re.MULTILINE)
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
# How much of the instruction after "from now on" a quote must hold, so the phrase cannot be stitched in.
_INSTRUCTION_LETTERS = 8


def _made_a_rule(evidence: str, user_texts: list[str]) -> bool:
    """Whether the user plainly made a standing rule, in words they typed themselves.

    Either "make that (this, it) a rule", not negated ("don't make that a
    rule"), in the sentence the quote is from or one beside it; or the quoted
    sentence begins with "from now on" or "going forward" and goes on with an
    instruction, which the quote reaches ("From now on, always keep backups in
    X" quoted as "always keep backups in X"; never a "From now on" stitched in
    from another sentence). Quotations, code, and what the user pasted after
    asking about it ("Can you summarize this mail?") never count, nor does a
    question ("Should we make that a rule?") or a rule of thumb.
    """

    pieces = [letters(part) for part in _ELISION.split(evidence) if letters(part)]
    for said in user_texts:
        if not pieces or not _quoted_in(evidence, [said]):
            continue
        text = said.replace("’", "'")
        flat, excluded = letters(text), _not_typed(text)
        sentences, position = [], 0
        for start, end in _sentence_spans(text):
            count = len(letters(text[start:end]))
            sentences.append((start, end, position, position + count))
            position += count
        beside: set[int] = set()
        cursor = 0
        for piece in pieces:
            found = flat.find(piece, cursor)
            cursor = found + len(piece)
            for index, (start, end, first, last) in enumerate(sentences):
                if found >= last or cursor <= first:
                    continue
                beside.update((index - 1, index, index + 1))
                begins = _FROM_NOW_ON.match(text, start, end)
                if begins is None or _inside(begins.start(1), excluded) or _asks(text, start, end):
                    continue
                instruction = first + len(letters(text[start:begins.end(1)]))
                if min(cursor, last) - max(found, instruction) >= _INSTRUCTION_LETTERS:
                    return True
        for index in sorted(beside):
            if 0 <= index < len(sentences) and _says_make_it_a_rule(text, sentences[index][:2], excluded):
                return True
    return False


def _says_make_it_a_rule(text: str, span: tuple[int, int], excluded: list[tuple[int, int]]) -> bool:
    start, end = span
    if _asks(text, start, end):
        return False
    for match in _STANDING_RULE.finditer(text, start, end):
        if _inside(match.start(), excluded):
            continue
        window = max(start, match.start() - _NEGATION_WINDOW)
        clause = max(window, *(text.rfind(mark, window, match.start()) + 1 for mark in ",;:"))
        if not _RULE_NEGATED.search(text, clause, match.start()):
            return True
    return False


def _asks(text: str, start: int, end: int) -> bool:
    """Whether the sentence from ``start`` to ``end`` is a question: it asks, it does not decide."""

    return text[start:end].rstrip(_CLOSING_MARKS).endswith("?")


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for match in _SENTENCE_END.finditer(text):
        if match.start() > start:
            spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _not_typed(text: str) -> list[tuple[int, int]]:
    """Where ``text`` holds words the user did not type as their own: code, quotations, quoted lines, and
    everything after a paragraph that asks about what follows (ending in ":" or "?"), which is pasted."""

    spans = [match.span() for match in _NOT_TYPED.finditer(text)]
    start = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        if text[start:match.start()].rstrip().endswith((":", "?")):
            spans.append((match.end(), len(text)))
            break
        start = match.end()
    return spans


def _inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


# Two preferences say the same when this share of each one's significant words is in the other, and they have the
# same numbers and the same words that turn a statement around ("use port 8080" is not "use port 9090", and
# "squash commits" is not "do not squash commits").
SAME_PREFERENCE_SHARE = 0.8
_POLARITY_WORDS = frozenset({"not", "no", "never", "without", "instead", "avoid", "stop", "none"})
_NOT_CONTRACTED = re.compile(r"n['’]t\b|\b(?:cannot|dont|doesnt|didnt|cant|wont|isnt|arent|shouldnt)\b",
                             re.IGNORECASE)
# What is left of "don't" and the like once a text is split into words.
_CONTRACTION_STEMS = frozenset({"don", "doesn", "didn", "isn", "aren", "wasn", "weren", "won", "shouldn", "couldn",
                                "wouldn", "mustn", "haven", "hasn", "hadn", "cannot", "dont", "doesnt", "didnt",
                                "cant", "wont", "isnt", "arent", "shouldnt"})
KNOWN_LIMIT = 40
# The user's preferences about the same things, shown besides, so the model can tell when the user says one again.
KNOWN_PREFERENCES = 8
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
    # Each candidate's outcome, with its text (redacted, and none for one that held a secret) and memory ids.
    results: list[dict[str, Any]] = field(default_factory=list)
    set_aside: int = 0

    def describe(self, *, dry_run: bool) -> str:
        lines = [
            f"Session logs found: {self.logs}; still active: {self.active}; ready to learn from: {len(self.ready)}.",
        ]
        for item in self.ready:
            repeat = f", {item.duplicates} identical to content already learned" if item.duplicates else ""
            lines.append(f"- {item.session_id}: {item.dossiers} dossier(s), {item.characters:,} characters{repeat}")
        if self.set_aside:
            noun = "session" if self.set_aside == 1 else "sessions"
            lines.append(f"Set aside {self.set_aside} {noun} waiting more than {SET_ASIDE_DAYS} days; "
                         "`knowitall2 learn --catch-up` lists them.")
        if dry_run:
            lines.append("Dry run: no model calls were made and nothing was saved.")
            return "\n".join(lines)
        lines.append(f"Model calls: {self.calls}; sessions deferred by the budget: {self.deferred}.")
        if self.outcomes:
            lines.append("Candidates: " + ", ".join(f"{name} {count}" for name, count in sorted(self.outcomes.items())))
        if self.failed:
            lines.append("Will retry later: " + ", ".join(self.failed))
        if self.skipped:
            lines.append(f"Skipped a part after {MAX_FAILURES} failures in a row: " + ", ".join(self.skipped))
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
    calls_today = state.limited_calls_since(day_ago(moment))
    origin = "work" if right_away else "learning"
    report = LearnReport(logs=len(logs))
    memory = None if dry_run or memory_factory is None else memory_factory()
    run_seen: set[str] = set()

    def remaining() -> int:
        return min(settings.max_calls_per_run - report.calls, settings.max_calls_per_day - calls_today - report.calls)

    try:
        found = []
        for log in logs:
            try:
                found.append((log, log.stat()))
            except OSError:
                continue
        if not right_away:
            # The newest waiting sessions first; one quiet for weeks is set aside.
            found.sort(key=lambda item: item[1].st_mtime, reverse=True)
        set_aside_before = moment - timedelta(days=SET_ASIDE_DAYS)
        for log, status in found:
            entry = state.entry(log)
            if status.st_size <= int(entry.get("offset", 0)):
                continue
            active_at = datetime.fromtimestamp(status.st_mtime, tz=timezone.utc)
            if not right_away and active_at > idle_since:
                report.active += 1
                continue
            if (not right_away and active_at < set_aside_before and not entry.get("caught_up_at")
                    and _set_aside(entry, log, moment)):
                report.set_aside += 1
                continue
            if not dry_run and remaining() <= 0:
                # Counted, not read: reading every waiting log took minutes at each run once the calls ran out.
                report.deferred += 1
                continue
            start = int(entry.get("offset", 0))
            # Catching up on a set-aside part reads only that part (``learn_to``); what came after was learned.
            session, end = read_session(log, start=start, stop=int(entry.get(LEARN_TO) or 0) or None)
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
            budget = remaining()
            progress: int | None = None
            completed = True
            current: Dossier | None = None
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
                    current = dossier
                    dossier.known = known_context(memory, dossier)
                    try:
                        candidates = extractor.extract(dossier)
                    finally:
                        report.usage.update(getattr(extractor, "last_usage", None) or {})
                    budget -= 1
                    report.calls += 1
                    state.record_call(at=moment, session=session.session_id, outcome="ok", origin=origin)
                    for candidate in candidates:
                        outcome, record_ids = publish_with_ids(memory, candidate, dossier, run=run)
                        report.outcomes[outcome] += 1
                        report.results.append({"outcome": outcome, "ids": record_ids,
                                               "text": result_text(outcome, candidate)})
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
                state.record_call(at=moment, session=session.session_id, outcome="failed", origin=origin)
                # Only failures in a row count: a run that got further first starts the count again.
                entry["failures"] = 1 if progress is not None else int(entry.get("failures", 0)) + 1
                entry["last_error"] = str(exc)[:300]
                entry["updated_at"] = moment.isoformat()
                if entry["failures"] >= MAX_FAILURES:
                    # Give up on the excerpt that keeps failing, not on the rest of the session.
                    last = current is None or current is dossiers[-1]
                    entry["offset"] = end if last else current.end_offset
                    entry["failures"] = 0
                    entry["status"] = "skipped" if last else "partial"
                    report.skipped.append(session.session_id)
                else:
                    entry["status"] = "failed"
                    report.failed.append(session.session_id)
                continue
            except (sqlite3.Error, StoreError, OSError) as exc:
                # The memory store failed (a write held too long by another process, say): keep what this
                # session already gave, blame no session, and stop; the next run carries on from here.
                if progress is not None:
                    entry.update({"offset": progress, "session_id": session.session_id, "status": "partial",
                                  "updated_at": moment.isoformat()})
                report.blocked = f"the memory store failed: {exc}"
                journal.problem("learning", f"learning stopped because the memory store failed: {exc}")
                break
            if completed:
                _finish(entry, session.session_id, end, moment)
                continue
            # The budget ran out part-way: keep what was learned and continue next run.
            if progress is not None:
                entry["offset"] = progress
            entry.update({"session_id": session.session_id, "status": "partial", "updated_at": moment.isoformat()})
            report.deferred += 1
    finally:
        if not dry_run:
            state.save()
    return report


def result_text(outcome: str, candidate: dict[str, Any]) -> str | None:
    """A candidate's text for the run's results, which the news shows: none for a secret, and redacted.

    A turned-down candidate was never screened like a memory, so it is
    redacted too, in case the checks turned it down for another reason.
    """

    return None if outcome == "rejected (secret)" else redact(str(candidate.get("text") or ""))


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
    preferences = 0
    for row, _rank in memory.store.search(match, project_id=project_id, scope="all", limit=KNOWN_LIMIT * 5):
        if preferences >= KNOWN_PREFERENCES:
            break
        if row.id not in seen and PREFERENCE_TAG in row.tags:
            known.append(row)
            seen.add(row.id)
            preferences += 1
    return known


def publish(memory: Memory, candidate: dict[str, Any], dossier: Dossier, *, run: str | None = None) -> str:
    """Validate one candidate and save it; returns the outcome name for the report.

    A candidate that updates or contradicts a known memory replaces it only when
    its evidence is at least as strong (``may_replace_unasked``); otherwise both
    are kept and KnowItAll2 finds out which is right. A preference the user
    states again is counted on the memory that holds it, not saved twice.
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
        # When the words were said, for "newer wins" later (``_last_said``).
        said_at = _when_said(" ".join(str(candidate.get("evidence") or "").split()), dossier)
        if said_at:
            details["said_at"] = said_at
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
    older = None
    if PREFERENCE_TAG in checked["tags"] and checked["relation"] not in ("updates", "contradicts"):
        # What the model says the user changed is never counted as said again.
        known, same = _preference_repeated(memory, checked, dossier)
        if known is not None and same and known.kind == "rule":
            return "already known", [known.id]   # one of the user's rules says it already
        if known is not None and same:
            return _said_again(memory, checked, dossier, known)
        older = known   # nearly the same words, with another number or a "not": the user changed it
    if older is None and checked["relation"] == "repeats" and not _named_a_preference(memory, checked, dossier):
        return "already known", []   # it repeats something that is not one of the user's preferences
    if older is None:
        older = _changed_memory(memory, checked, dossier)
    verification = VERIFICATION_FOR_SOURCE[checked["source"]]
    earlier = older is not None and _said_before(memory, checked, older)
    if earlier and VERIFICATION_STRENGTH[older.verification] >= VERIFICATION_STRENGTH[verification]:
        return "already known", [older.id]   # older words: what is known now was said since, and newer words win
    replaces = None
    if older is not None and not earlier and may_replace_unasked(older, kind=checked["kind"], text=checked["text"],
                                                                 verification=verification):
        replaces, older = older.id, None
    elif older is not None and _word_for_word(older.text, checked["text"]):
        return "already known", [older.id]   # nothing to ask: a preference saying what a rule says, word for word
    try:
        result = memory.remember(
            checked["text"],
            kind=checked["kind"],
            subjects=checked["subjects"],
            tags=checked["tags"],
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
        return ("updated" if result.replaced else "already known"), ids
    if older is not None:
        review.ask_conflict(memory, older, result.record)
        return "saved with a question", ids + [older.id]
    return ("updated" if replaces else "saved"), ids


def _preference_repeated(memory: Memory, checked: dict[str, Any], dossier: Dossier) -> tuple[RecordRow | None, bool]:
    """The user's preference or rule a candidate says again (with True), or changes in nearly the same words, with
    another number or a "not" (with False): the one the model named, or one the model was not shown."""

    if checked["known_id"] in dossier.known_ids:
        record = memory.store.get(checked["known_id"])
        if record is not None and record.status == "active" and _a_preference(record):
            compared = _compare(record.text, checked["text"])
            if compared is not None:
                return record, compared == "same"
    terms = keywords(checked["text"], limit=12)
    if not terms or not _content_words(checked["text"]):
        return None, False
    project_id = dossier.project.id if dossier.project is not None else None
    match = " OR ".join(f'"{term}"' for term in terms)
    changed = None
    for row, _rank in memory.store.search(match, project_id=project_id, scope="all", limit=20):
        if not _a_preference(row):
            continue
        compared = _compare(row.text, checked["text"])
        if compared == "same":
            return row, True
        if compared == "changed" and changed is None:
            changed = row
    return changed, False


def _compare(known: str, text: str) -> str | None:
    """"same" when ``text`` says what ``known`` says in nearly the same words, "changed" when it is in nearly the
    same words but with another number or a word that turns it around ("not", "never", "instead"), and None
    when it says something else."""

    mine, theirs = _content_words(text), _content_words(known)
    if not mine or not theirs or min(_share(mine, theirs), _share(theirs, mine)) < SAME_PREFERENCE_SHARE:
        return None
    return "same" if _turns(text) == _turns(known) else "changed"


def _content_words(text: str) -> set[str]:
    """``text``'s significant words, endings aside, without the words that turn it around (compared apart)."""

    return {_stem(word) for word in significant_words(text) if word not in _POLARITY_WORDS | _CONTRACTION_STEMS}


def _turns(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """The words of ``text`` that turn a statement around ("don't" counted as "not"), and its numbers."""

    tokens = _tokens(text)
    polarity = {token for token in tokens if token in _POLARITY_WORDS}
    if _NOT_CONTRACTED.search(text):
        polarity.add("not")
    return frozenset(polarity), frozenset(token for token in tokens if any(character.isdigit() for character in token))


def _word_for_word(known: str, text: str) -> bool:
    """The same letters and digits, word for word."""

    return "".join(_tokens(known)) == "".join(_tokens(text))


def _named_a_preference(memory: Memory, checked: dict[str, Any], dossier: Dossier) -> bool:
    """Whether the model said this preference repeats one of the user's preferences it was shown: when the words
    say something else, they are the user's words all the same, and are kept."""

    if PREFERENCE_TAG not in checked["tags"] or checked["known_id"] not in dossier.known_ids:
        return False
    record = memory.store.get(checked["known_id"])
    return record is not None and _a_preference(record)


def _a_preference(record: RecordRow) -> bool:
    """How the user wants things done: a preference, or a rule or decision in the user's own words."""

    return PREFERENCE_TAG in record.tags or is_users_choice(record)


def _share(words: set[str], others: set[str]) -> float:
    return sum(1 for word in words if any(_same_word(word, other) for other in others)) / len(words)


def _said_again(memory: Memory, checked: dict[str, Any], dossier: Dossier, known: RecordRow) -> tuple[str, list[str]]:
    """Count a preference the user said again, in another chat, on the memory that holds it.

    The new words are kept as their own memory, replaced at once by the one
    that holds the preference: it syncs like any memory, keeps the user's words
    and date, and ``Store.said_in_sessions`` counts the chats it came from.
    The user's own words take the place of a weaker memory holding it.
    Returns the memory that now holds the preference first.
    """

    if not dossier.session_id or dossier.session_id in memory.store.said_in_sessions(known.id):
        return "already known", [known.id]
    words = memory.said_again(known, checked["text"], subjects=checked["subjects"], source=checked["source"],
                              session=dossier.session_id)
    return "said again", ([words.id, known.id] if words.status == "active" else [known.id, words.id])


def _changed_memory(memory: Memory, checked: dict[str, Any], dossier: Dossier) -> RecordRow | None:
    """The known memory a candidate updates or contradicts, if the claim holds up."""

    if checked["relation"] not in ("updates", "contradicts") or checked["known_id"] not in dossier.known_ids:
        return None
    record = memory.store.get(checked["known_id"])
    if record is None or record.status != "active" or not _about_the_same(record, checked):
        return None  # an unrelated memory is never replaced or questioned: the candidate is new
    return record


def _when_said(evidence: str, dossier: Dossier) -> str | None:
    """When the session said what ``evidence`` quotes: the user's last message holding it, or else the time the
    excerpt ends. None when the log kept no times."""

    for said, at in reversed(list(zip(dossier.user_texts, dossier.user_times))):
        if at and (_quoted_in(evidence, [said]) or _nearly_quoted_in(evidence, [said])):
            return at
    return dossier.ended_at


def _said_before(memory: Memory, checked: dict[str, Any], older: RecordRow) -> bool:
    """Whether the candidate's words were said before what ``older`` holds was last said.

    "Newer wins" goes by when the user said each thing, not by when a chat was
    learned: a catch-up of an old chat, or a chat learned after a later one,
    must not replace what was said since. When either time is not known, the
    candidate is not taken for the older: it replaces or asks, as before 0.10.6.
    """

    said, last = _moment(checked.get("said_at")), _last_said(memory, older)
    return said is not None and last is not None and said < last


def _last_said(memory: Memory, record: RecordRow) -> datetime | None:
    """When what ``record`` holds was last said, when that is truly known: the newest time the journal keeps of
    the words it was learned from or counted with, or else when it was saved, if it was saved while it was said
    (an agent's or the user's save in the chat). None when not known: learning and imports save words said
    earlier, and before 0.10.6 the journal kept no time for them, so an old memory learned late from a backlog
    must not count as said since (R-M5)."""

    times = [_moment(event["details"].get("said_at"))
             for event in memory.store.events(kinds=["candidate"], record_id=record.id, limit=20)
             if event["record_ids"][:1] == [record.id]]
    known = [moment for moment in times if moment is not None]
    if known:
        return max(known)
    return _moment(record.created_at) if _saved_when_said(record) else None


def _saved_when_said(record: RecordRow) -> bool:
    """Whether ``record`` was saved while its words were said: by an agent (named or not), the user, or the app at
    the time, not by learning or an import, which save what was said before."""

    agent = record.source_agent or ""
    return agent != LEARNER_AGENT and not agent.startswith("import:")


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)



def validate(candidate: dict[str, Any], dossier: Dossier) -> tuple[dict[str, Any] | None, str | None]:
    """Deterministic checks the model cannot talk its way past."""

    text = " ".join(str(candidate.get("text") or "").split())
    kind = candidate.get("kind")
    scope = candidate.get("scope")
    evidence = " ".join(str(candidate.get("evidence") or "").split())
    subjects = candidate.get("subjects") if isinstance(candidate.get("subjects"), list) else []
    # First, so that a candidate holding a secret is never kept or shown, whatever else is wrong with it.
    if contains_secret(" ".join([text, evidence, *(str(item) for item in subjects)])):
        return None, "secret"
    subjects = [" ".join(str(item).split())[:80] for item in subjects if str(item).strip()][:8]
    if not MIN_TEXT_CHARACTERS <= len(text) <= MAX_TEXT_CHARACTERS:
        return None, "length"
    if kind not in KINDS and kind != "preference":   # a preference is saved as a note (below)
        return None, "kind"
    if scope not in ("global", "project"):
        return None, "scope"
    # Where some work stood goes out of date, often before anyone reads it: a catch-up of a day-old chat saved
    # "not yet fixed" after the fix was released (2026-10-10). The prompt says so; models do it anyway. What lasts
    # ("from now on", "always", "never") is not a state, nor is the user's own rule or preference (checked below).
    work_state = bool(_WORK_STATE.search(text)) and not _LASTING.search(text)
    if work_state and kind not in ("rule", "preference"):
        return None, "work state"
    if scope == "project" and dossier.project is None:
        scope = "global"
    if dossier.from_documents and dossier.project is not None:
        # A project's documents may be wrong or planted: what they teach reaches only that project's agents.
        scope = "project"
    body = dossier.text[dossier.body_start:]  # not the header KnowItAll2 wrote (the session's id and folder)
    if not _quoted_in(evidence, [body]) and not _nearly_quoted_in(evidence, [body]):
        # Every memory must rest on the session itself: not on the known-memory
        # list, and not on context an engine adds, such as the user's AGENTS.md.
        # A near quote (a few words re-typed) counts, but only an exact one below
        # makes a memory observed or the user's own words, or makes a rule; the
        # user's words must also say what the memory says.
        return None, "evidence not in the session"
    source = "inferred"
    tags: list[str] = []
    if kind in ("rule", "preference"):
        quoted = _quoted_in(evidence, dossier.user_texts)
        if quoted and not _in_the_users_words(text, evidence):
            text, subjects = _the_users_sentence(text, evidence, subjects)
        in_their_words = quoted and _in_the_users_words(text, evidence)
        if kind == "rule" and in_their_words and _made_a_rule(evidence, dossier.user_texts):
            source = "user"   # the user plainly made it a rule
        elif quoted or _nearly_quoted_in(evidence, dossier.user_texts):
            # How the user wants things done, said once: a preference, never a rule and never a question.
            kind, tags = "note", [PREFERENCE_TAG]
            source = "user" if in_their_words else "inferred"
        else:
            kind = "note"   # an instruction an agent wrote, not the user's
        if work_state and source != "user":
            return None, "work state"
    elif _quoted_in(evidence, dossier.trusted_outputs):
        # What a command printed is often raw data that the memory explains in its own words, so no shared words
        # are needed. Documents, searches, MCP tools, and helper agents only say what someone wrote: unverified.
        source = "observed"
    elif _quoted_in(evidence, dossier.user_texts) and _in_the_users_words(text, evidence):
        source = "user"
    relation = candidate.get("relation")
    relation = relation if relation in ("updates", "contradicts", "repeats") else "new"
    known_id = str(candidate.get("known_id") or "").strip().strip("[]")
    return {
        "text": text, "kind": kind, "scope": scope, "subjects": subjects, "source": source, "tags": tags,
        "relation": relation, "known_id": known_id, "said_at": _when_said(evidence, dossier),
    }, None


def _says_what_was_quoted(text: str, quote: str, *, share: float = USER_WORDS_SHARE) -> bool:
    """Whether the quoted words support ``text``: most of its significant words, endings aside, are in them."""

    words = {_stem(word) for word in significant_words(text)}
    if not words:
        return False
    quoted = {_stem(word) for word in significant_words(quote)}
    found = sum(1 for word in words if any(_same_word(word, other) for other in quoted))
    return found / len(words) >= share


def _in_the_users_words(text: str, quote: str) -> bool:
    """Whether ``text`` says what the user said in ``quote``, and nothing more that matters.

    A memory said to be in the user's own words must say what the user said,
    not rest a document's instruction on a harmless sentence of theirs: nearly
    all its significant words are in the quote, and so is every address,
    path, option, or command it names.
    """

    if not _says_what_was_quoted(text, quote, share=USER_STATEMENT_SHARE):
        return False
    said = {_bare(token) for token in quote.split()}
    return all(_bare(token) in said for token in text.split() if _matters(token))


def _the_users_sentence(text: str, quote: str, subjects: list[str]) -> tuple[str, list[str]]:
    """A preference or rule as the user's own words, without the work the model wrote around them.

    The model is asked for the user's sentence as the text, with the work it
    was about in the subjects. When the text still wraps an exact quote in
    words of the model's ("For the release notes, ..."), the memory keeps the
    quote alone, and a leading "For ..." clause becomes a subject: what is
    saved as the user's words is then only what they said. Words that are not
    context (an address, a path, an option, or a command) are never left out
    this way, and a quote shortened with "..." is never a sentence.
    """

    sentence = quote.strip()
    if _ELISION.search(sentence) or not MIN_TEXT_CHARACTERS <= len(sentence) <= MAX_TEXT_CHARACTERS:
        return text, subjects
    if not _says_what_was_quoted(sentence, text, share=USER_STATEMENT_SHARE):
        return text, subjects   # the text says something else
    said = {_bare(token) for token in sentence.split()}
    if any(_matters(token) and _bare(token) not in said for token in text.split()):
        return text, subjects   # what the text adds is not context
    context = _CONTEXT_CLAUSE.match(text)
    work = context.group(1).strip() if context else ""
    if work and len(subjects) < 8 and work.casefold() not in {item.casefold() for item in subjects}:
        subjects = [*subjects, work]
    sentence = sentence[0].upper() + sentence[1:]
    return (sentence if sentence[-1] in ".!?" else sentence + "."), subjects


def _bare(token: str) -> str:
    return token.strip(".,;:!?()[]{}\"'`").lower()


def _matters(token: str) -> bool:
    """An address, path, option, or command: what would make a sentence an instruction to run something."""

    bare = _bare(token)
    return bool(bare) and (bare in _COMMAND_WORDS or bare.startswith("-") or any(mark in bare for mark in "/\\.:|$@=&;>"))


def _about_the_same(older: RecordRow, checked: dict[str, Any]) -> bool:
    """Whether a memory a candidate says it updates or contradicts is about what the candidate is about.

    A shared subject, or a few significant words in common: otherwise the
    claim is the model's mistake (or an instruction it read), and the known
    memory is left alone.
    """

    mine = {" ".join(str(subject).lower().split()) for subject in checked.get("subjects") or []}
    if mine & {" ".join(subject.lower().split()) for subject in older.subjects}:
        return True
    first = {_stem(word) for word in significant_words(older.text)}
    second = {_stem(word) for word in significant_words(checked["text"])}
    if not first or not second:
        return False
    shared = sum(1 for word in first if any(_same_word(word, other) for other in second))
    return shared >= RELATED_WORDS and shared / min(len(first), len(second)) >= RELATED_SHARE


_WORD_PREFIX_MINIMUM = 4


def _same_word(first: str, second: str) -> bool:
    """The same word, or one a longer form of the other ("deploy" and "deployment")."""

    if first == second:
        return True
    shorter, longer = sorted((first, second), key=len)
    return len(shorter) >= _WORD_PREFIX_MINIMUM and longer.startswith(shorter)


def _set_aside(entry: dict[str, Any], log: Path, moment: datetime) -> bool:
    """Set aside what is waiting in ``log``, the way ``learn --start-from-now`` does, for catching up later; False
    when no whole line waits."""

    from .catchup import SET_ASIDE_TO, SKIPPED

    try:
        end = line_boundary(log)
    except OSError:
        return False
    if end <= int(entry.get("offset", 0)):
        return False
    entry.update({"offset": end, "status": SKIPPED, SET_ASIDE_TO: end, "failures": 0, "updated_at": moment.isoformat()})
    return True


def _finish(entry: dict[str, Any], session_id: str, end: int, moment: datetime) -> None:
    bound = int(entry.get(LEARN_TO) or 0)
    if bound and end >= bound:
        # The set-aside part is caught up: carry on from where learning had already got to.
        end = max(end, int(entry.pop(RESUME_AT, 0) or 0))
        entry.pop(LEARN_TO, None)
    entry.update(
        {"session_id": session_id, "offset": end, "status": "done", "failures": 0, "last_error": None,
         "updated_at": moment.isoformat()}
    )
