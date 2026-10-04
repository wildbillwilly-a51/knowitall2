"""Remember, recall, forget, and brief: the operations every interface shares."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import journal
from .identity import ProjectIdentity, identify
from .quotes import quoted_in
from .secrets import find_secrets, redact
from .store import RecordRow, Store

KINDS = ("fact", "procedure", "decision", "lesson", "rule", "note")
SOURCES = ("user", "observed", "inferred")
SCOPES = ("global", "project")
RECALL_SCOPES = ("all", "project", "global", "everywhere")

MAX_TEXT_CHARACTERS = 2000
MAX_LABELS = 8
MAX_LABEL_CHARACTERS = 80
DEFAULT_RECALL_LIMIT = 8
MAX_RECALL_LIMIT = 25
DEFAULT_BRIEFING_CHARACTERS = 3600
BRIEFING_ITEM_CHARACTERS = 240
BRIEFING_RULE_CHARACTERS = 200
# For a chat that moves to another project: that project's part of a briefing.
CONTENTS_CHARACTERS = 1800
# The briefing shows the project's most useful memories in full, then a table
# of contents: the headlines of the rest, by system, most useful first.
KEY_POINTS = 3
# The briefing also gives in full the project's newest memories from this many hours, up to this many.
LATEST_HOURS = 72
LATEST_SHOWN = 2
# A shared rule with this tag is in every project's briefing, whatever system it is filed under.
EVERYWHERE_TAG = "everywhere"
HEADLINE_CHARACTERS = 90
CONTENTS_HEADER = "Also known here (recall with a few of these words for the details):"
MOVED_HEADER = ("KnowItAll2 knows this about project {name}, where this chat is now working "
                "(recall with a few of these words for the details):")
# Most useful first: how to do things and reach systems, then decisions and
# lessons, then descriptions. Current-state notes ("status") go out of date,
# so briefings leave them out; recall still finds them.
_FACET_ORDER = ("howto", "access", "signin", "where", "decision", "lesson", "can_do", "rule", "about", "other")
_KIND_FACETS = {"procedure": "howto", "decision": "decision", "lesson": "lesson", "rule": "rule", "fact": "about",
                "note": "other"}
# A user rule counts as already in a project's instructions when one section of
# the project's AGENTS.md or CLAUDE.md contains its own words, in order (letters
# and digits alone, so wrapping and formatting do not matter): a section that only
# shares most of its words may say the opposite. Short rules always show.
_INSTRUCTED_MINIMUM_WORDS = 8
_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
_INSTRUCTION_FILE_LIMIT = 256 * 1024
FRESH_DAYS = 90
FRESHNESS_FLOOR = 0.6
QUESTION_NOTICE_DAYS = 3
QUESTIONS_SHOWN_KEY = "questions_shown_at"
# A question the background review has not settled within this time goes to the user.
QUESTION_REVIEW_DAYS = 3
TASK_OFFER_HEADER = (
    "KnowItAll2 asks (optional; only if it is quick and look-only, and never in the way of the user's task):"
)

VERIFICATION_FOR_SOURCE = {"user": "user_stated", "observed": "observed", "inferred": "unverified"}
VERIFICATION_STRENGTH = {"unverified": 0, "observed": 1, "user_stated": 2}
# Two statements are the same one said again when this share of their distinct words is shared.
SAME_STATEMENT_SHARE = 0.9
DEFAULT_HISTORY_LIMIT = 20
_VERIFICATION_BOOST = (1.0, 1.05, 1.15)
_STOPWORDS = frozenset(
    "a about an and any anything are as at be been but by can could did do does for from had has have "
    "how i if in is it its know me my of on or our should so tell that the their them then there these "
    "this those to us was we were what when where which who why will with would you your".split()
)
# Split like SQLite's unicode61 tokenizer: anything but letters and digits,
# including underscores, separates tokens.
_TERM_SPLIT = re.compile(r"[\W_]+")
_HASH_LIKE = re.compile(r"[0-9a-f]{12,}")
# Tool-call serialization leaking into a text argument (seen in live use).
_TOOL_CALL_MARKUP = re.compile(r"</text>|</?parameter\b|</?invoke\b|</?function_calls\b")
# Recall ranks memories by the share of the query they cover, each keyword
# weighted by how rare it is. A memory covering at least half is a match; one
# covering less, down to the partial share, is shown as a partial match when
# there are few matches, so a long, specific query still finds what is there,
# while a memory that shares one common word with the query is not shown.
STRONG_MATCH_SHARE = 0.5
PARTIAL_MATCH_SHARE = 0.3
PARTIAL_MATCHES_SHOWN = 3
# Word endings recall ignores, so "releases" finds "release" and "deployed" finds "deploy".
_ENDINGS = ("ing", "ed", "es", "s")
_STEM_MINIMUM = 4


class MemoryInputError(ValueError):
    """A request the caller can fix; the message explains how."""


@dataclass(frozen=True)
class RememberResult:
    status: str
    record: RecordRow
    replaced: RecordRow | None = None
    # The memory it was meant to replace, kept because its evidence is stronger; a question asks which is right.
    kept: RecordRow | None = None

    def describe(self) -> str:
        if self.status == "already_known":
            return f"Already known as [{self.record.id}]; marked as confirmed."
        text = f"Saved [{self.record.id}] ({self.record.kind}, {_scope_label(self.record, None)})."
        if self.replaced is not None:
            text += f" It replaces [{self.replaced.id}]."
        if self.kept is not None and self.kept.verification == "user_stated":
            text += (f" [{self.kept.id}] is the user's own statement, so it was not replaced: both are kept, "
                     "and the user will be asked which is right.")
        elif self.kept is not None:
            text += (f" [{self.kept.id}] is better verified ({self.kept.verification}), so it was not replaced: "
                     "both are kept until KnowItAll2 finds out which is right.")
        return text


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def may_supersede(older: str, newer: str) -> bool:
    """Whether evidence of verification ``newer`` may replace a memory of verification ``older``.

    Evidence must be at least as strong, and the user's own statement gives way
    only to the user's own words.
    """

    if older == "user_stated":
        return newer == "user_stated"
    return VERIFICATION_STRENGTH[newer] >= VERIFICATION_STRENGTH[older]


def may_replace_unasked(older: RecordRow, *, kind: str, text: str, verification: str) -> bool:
    """Whether learning or maintenance may replace ``older`` with a memory of this kind, text, and verification.

    Evidence must be at least as strong, and one of the user's own statements
    gives way only to the same statement said again: a model may misread
    which of two user statements is current, so otherwise the user is asked.
    """

    if not may_supersede(older.verification, verification):
        return False
    return older.verification != "user_stated" or same_statement(older, kind=kind, text=text)


def same_statement(record: RecordRow, *, kind: str, text: str) -> bool:
    """Whether ``kind`` and ``text`` say what ``record`` says, in nearly the same words.

    The same kind, and the same letters and digits or nearly all the same words.
    """

    if record.kind != kind:
        return False
    first, second = _tokens(record.text), _tokens(text)
    if "".join(first) == "".join(second):
        return True
    words, others = set(first), set(second)
    return bool(words | others) and len(words & others) / len(words | others) >= SAME_STATEMENT_SHARE


class Memory:
    """The memory operations, bound to one store and one calling agent.

    Each operation is recorded in the activity journal. The learner turns that
    off for its saves, because it records every candidate with more detail.
    """

    def __init__(
        self, store: Store, *, agent: str | None = None, clock: Callable[[], str] = utc_now,
        record_events: bool = True,
    ) -> None:
        self.store = store
        self.agent = agent
        self._clock = clock
        self._record_events = record_events
        self._touched: set[tuple[str, str]] = set()

    def now(self) -> str:
        return self._clock()

    def project_for(self, project_path: str | Path | None) -> ProjectIdentity | None:
        identity = identify(Path(project_path) if project_path else Path.cwd())
        if identity is not None and (identity.id, identity.path_key) not in self._touched:
            self.store.touch_project(identity, self._clock())
            self._touched.add((identity.id, identity.path_key))
        return identity

    def remember(
        self,
        text: str,
        *,
        kind: str = "fact",
        subjects: Iterable[str] = (),
        tags: Iterable[str] = (),
        scope: str | None = None,
        source: str = "inferred",
        replaces: str | None = None,
        project_path: str | Path | None = None,
        session: str | None = None,
        project: ProjectIdentity | None = None,
        recorded_at: str | None = None,
        detect_project: bool = True,
    ) -> RememberResult:
        """Save one memory. ``project`` and ``recorded_at`` let an import keep a
        memory's original project and date; ``detect_project`` off leaves the
        current folder's project out of it (the app has no workspace).

        ``replaces`` supersedes that memory only when the new evidence is at
        least as strong, and the user's own statement only for the user's own
        words; otherwise both are kept and a question asks which is right.
        """

        _require_choice(kind, KINDS, "kind")
        _require_choice(source, SOURCES, "source")
        if scope is not None:
            _require_choice(scope, SCOPES, "scope")
        body = _clean_text(text)
        if not body:
            raise MemoryInputError("Nothing to remember: the text is empty.")
        if len(body) > MAX_TEXT_CHARACTERS:
            raise MemoryInputError(
                f"The text is {len(body)} characters; the limit is {MAX_TEXT_CHARACTERS}. "
                "Split it into separate memories."
            )
        if _TOOL_CALL_MARKUP.search(body):
            raise MemoryInputError(
                "Not saved: the text contains tool-call markup such as </text> or <parameter>, which usually "
                "means the call was malformed. Send only the statement itself, with kind and other options "
                "as separate arguments."
            )
        subject_list = _clean_labels(subjects, "subjects")
        tag_list = _clean_labels(tags, "tags")
        findings = find_secrets("\n".join([body, *subject_list, *tag_list]))
        if findings:
            kinds = ", ".join(sorted({finding.kind for finding in findings}))
            raise MemoryInputError(
                f"Not saved: this looks like it contains a secret ({kinds}). KnowItAll2 never stores secrets. "
                "Save where the credential is kept instead, for example: "
                "'The vCenter admin password is in Vaultwarden item \"vcenter-admin\".'"
            )
        if kind == "rule" and source != "user":
            raise MemoryInputError(
                "Not saved: rules must come from the user's own words. If these are the user's own words, "
                "pass --source user (source 'user' in the remember tool); otherwise save it as a fact or note, "
                "or ask the user to confirm it as a rule."
            )
        if project is None:
            project = self.project_for(project_path) if detect_project else None
        else:
            self.store.ensure_project(project, self._clock())
        effective_scope = scope or ("project" if kind == "decision" and project is not None else "global")
        if effective_scope == "project" and project is None:
            raise MemoryInputError(
                "Not saved: no project was found for this folder. Use scope 'global', "
                "or pass project_path for a folder inside a Git repository."
            )
        project_id = project.id if effective_scope == "project" and project is not None else None
        verification = VERIFICATION_FOR_SOURCE[source]
        content_hash = _content_hash(body, effective_scope, project_id)
        now = self._clock()
        with self.store.transaction():
            existing = self.store.find_active_duplicate(content_hash)
            if existing is not None:
                stronger = max(existing.verification, verification, key=VERIFICATION_STRENGTH.__getitem__)
                self.store.confirm(existing.id, verification=stronger, now=now)
                have = {tag.casefold() for tag in existing.tags}
                added = [tag for tag in tag_list if tag.casefold() not in have]
                # Saying it again with new tags adds them; only the user's own words change a rule.
                if added and (existing.kind != "rule" or source == "user"):
                    self.store.set_tags(existing.id, [*existing.tags, *added], now=now)
                known = RememberResult("already_known", self.store.get(existing.id) or existing)
                # The journal notes the project the agent worked in, even for a global memory.
                self.record_event(
                    "remember", body, outcome="already known", project_id=project.id if project else None,
                    record_ids=[existing.id], details={"kind": kind, "source": source},
                )
                return known
            if recorded_at is not None:
                now = recorded_at
            replaced = kept = None
            if replaces:
                replaced = self.store.get(replaces.strip().strip("[]"))
                if replaced is None:
                    raise MemoryInputError(f"Not saved: there is no memory [{replaces}] to replace.")
                if replaced.status != "active":
                    raise MemoryInputError(f"Not saved: [{replaced.id}] is already {replaced.status}.")
                if not may_supersede(replaced.verification, verification):
                    kept, replaced = replaced, None
            record_id = self._new_id()
            self.store.insert_record(
                record_id=record_id,
                kind=kind,
                text=body,
                subjects=subject_list,
                tags=tag_list,
                scope=effective_scope,
                project_id=project_id,
                verification=verification,
                source_kind="user" if source == "user" else "agent",
                source_agent=self.agent,
                source_session=session,
                content_hash=content_hash,
                now=now,
            )
            if replaced is not None:
                self.store.supersede(replaced.id, record_id, now=now)
            self.record_event(
                "remember", body, outcome="updated" if replaced else "saved", project_id=project.id if project else None,
                session=session, record_ids=[record_id, replaced.id if replaced else ""],
                details={"kind": kind, "scope": effective_scope, "source": source},
            )
            record = self.store.get(record_id)
            assert record is not None
            if kept is not None:
                from . import review

                review.ask_conflict(self, kept, record)
        return RememberResult("saved", record, replaced, kept)

    def recall(
        self,
        query: str,
        *,
        limit: int | None = None,
        scope: str = "all",
        project_path: str | Path | None = None,
    ) -> str:
        _require_choice(scope, RECALL_SCOPES, "scope")
        count = DEFAULT_RECALL_LIMIT if limit is None else limit
        if not 1 <= count <= MAX_RECALL_LIMIT:
            raise MemoryInputError(f"limit must be between 1 and {MAX_RECALL_LIMIT}.")
        cleaned = " ".join(str(query or "").split())
        terms = _query_terms(cleaned)
        match = _fts_match(terms)
        if match is None:
            raise MemoryInputError(
                "Recall needs at least one meaningful keyword, such as a system, project, or topic name."
            )
        project = self.project_for(project_path)
        if scope == "project" and project is None:
            raise MemoryInputError("No project was found for this folder, so there is nothing project-scoped to search.")
        project_id = project.id if project is not None else None
        now = _parse_time(self._clock())
        weights, unknown = self._term_weights(terms, project_id=project_id, scope=scope)
        total = sum(weights.values())
        candidates = self.store.search(match, project_id=project_id, scope=scope, limit=max(40, count * 6))
        ranked = []
        for row, rank in candidates:
            tokens = _row_tokens(row)
            covered = [term for term in weights if _covers(tokens, term)]
            share = sum(weights[term] for term in covered) / total
            # A partial match of a longer query shares more than one of its keywords.
            if share >= STRONG_MATCH_SHARE or (share >= PARTIAL_MATCH_SHARE and (len(covered) > 1 or len(terms) < 3)):
                ranked.append((share, _score(row, rank, project_id, now), row))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        records = [row for share, _, row in ranked if share >= STRONG_MATCH_SHARE][:count]
        partial: list[RecordRow] = []
        if len(records) < PARTIAL_MATCHES_SHOWN:
            partial = [row for share, _, row in ranked if share < STRONG_MATCH_SHARE]
            partial = partial[: min(count - len(records), PARTIAL_MATCHES_SHOWN - len(records))]
        if not records and not partial and len(cleaned) >= 3:
            records = self.store.substring_search(cleaned, project_id=project_id, scope=scope, limit=count)
        found = [row.id for row in records + partial]
        self._log_usage("recall", project, cleaned, found)
        text = _format_recall(cleaned, records, project, partial=partial, unknown=unknown)
        offers = self._task_lines(project_id=None, record_ids=found)
        return text + ("\n" + "\n".join(offers) if offers else "")

    def _term_weights(
        self, terms: list[_Term], *, project_id: str | None, scope: str,
    ) -> tuple[dict[_Term, float], list[str]]:
        """How much each keyword tells memories apart (rarer counts more), and the keywords no memory contains.

        A keyword no memory contains still counts, fully: a memory that lacks
        most of what was asked for is not a match, however common its one
        shared word.
        """

        total = max(1, self.store.count_active(project_id=project_id, scope=scope))
        weights: dict[_Term, float] = {}
        unknown: list[str] = []
        for term in terms:
            documents = self.store.count_matching(_fts_match([term]) or "", project_id=project_id, scope=scope)
            if not documents:
                unknown.append(" ".join(term.tokens))
            weights[term] = math.log(1 + total / (documents or 0.5))
        return weights, unknown

    def forget(self, record_id: str, *, reason: str | None = None, by_user: bool = False) -> str:
        """Retire a memory. Only the user's own request (``by_user``, such as the app's) retires the user's
        own statement; anyone else's opens a question for the user, with the reason given."""

        identifier = (record_id or "").strip().strip("[]")
        if not identifier:
            raise MemoryInputError("forget needs the id of a memory, such as k-1a2b3c4d5e.")
        note = _clean_text(reason or "")[:200] or None
        stated = self.store.get(identifier)
        if not by_user and stated is not None and stated.status == "active" and stated.verification == "user_stated":
            return self._ask_before_forgetting(stated, note)
        if self.store.retire(identifier, reason=note, now=self._clock()):
            record = self.store.get(identifier)
            assert record is not None
            self.record_event(
                "forget", record.text, outcome="retired", project_id=record.project_id, record_ids=[identifier],
                details={"reason": note} if note else None,
            )
            return f"Forgot [{identifier}]: {_shorten(record.text, 120)}"
        record = self.store.get(identifier)
        if record is None:
            raise MemoryInputError(f"There is no memory [{identifier}].")
        raise MemoryInputError(f"[{identifier}] is already {record.status}.")

    def _ask_before_forgetting(self, stated: RecordRow, reason: str | None) -> str:
        from . import review

        question_id = review.ask_to_forget(self, stated, reason=reason)
        self.record_event(
            "forget", stated.text, outcome="asked the user", project_id=stated.project_id, record_ids=[stated.id],
            details={"reason": reason, "question": question_id} if reason else {"question": question_id},
        )
        return (
            f"[{stated.id}] is the user's own statement, so it was not forgotten: KnowItAll2 asks the user whether "
            f"it is still true (question [{question_id}]). If the user just asked for this, record their choice: "
            f"answer {question_id} forget"
        )

    def restore(self, record_id: str) -> str:
        """Bring back a retired or superseded memory, on the user's word.

        A restored memory counts as the user's own statement, so maintenance
        never removes it again without asking.
        """

        identifier = (record_id or "").strip().strip("[]")
        record = self.store.get(identifier)
        if record is None:
            raise MemoryInputError(f"There is no memory [{identifier}].")
        if record.status == "active":
            raise MemoryInputError(f"[{identifier}] is already active.")
        self.store.restore(identifier, verification="user_stated", now=self._clock())
        self.record_event("restore", record.text, outcome="restored", project_id=record.project_id, record_ids=[identifier])
        text = f"Restored [{identifier}]: {_shorten(record.text, 120)}"
        if record.superseded_by:
            replacement = self.store.get(record.superseded_by)
            if replacement is not None and replacement.status == "active":
                text += f"\n[{replacement.id}], which had replaced it, is also still active; forget it if it is wrong."
        return text

    def confirm(self, record_id: str) -> str:
        """The user vouches for a memory, which makes it their own statement."""

        record = self._active(record_id)
        if record.verification == "user_stated":
            return f"[{record.id}] is already your own statement."
        self.store.confirm(record.id, verification="user_stated", now=self._clock())
        self.record_event("confirm", record.text, outcome="confirmed", project_id=record.project_id,
                          record_ids=[record.id])
        return f"Confirmed [{record.id}] as your own statement."

    def correct(self, record_id: str, text: str) -> RememberResult:
        """Replace a memory with the user's own wording, keeping its kind, labels, and scope."""

        record = self._active(record_id)
        project = None
        if record.scope == "project" and record.project_id:
            project = ProjectIdentity(record.project_id, record.project_name or record.project_id, Path(""), None)
        result = self.remember(
            text, kind=record.kind, subjects=record.subjects, tags=record.tags, scope=record.scope, source="user",
            replaces=record.id, project=project, detect_project=False,
        )
        if result.status == "already_known":
            raise MemoryInputError("That is exactly what the memory already says.")
        return result

    def _active(self, record_id: str) -> RecordRow:
        identifier = (record_id or "").strip().strip("[]")
        record = self.store.get(identifier)
        if record is None:
            raise MemoryInputError(f"There is no memory [{identifier}].")
        if record.status != "active":
            raise MemoryInputError(f"[{identifier}] is {record.status}; restore it first.")
        return record

    def move(self, record_id: str, project: ProjectIdentity, *, system_id: str | None = None) -> str:
        """File a project memory under another project, such as one learned in a chat opened in another folder.

        ``system_id`` also files it under that system in the catalog, keeping its headline and part.
        """

        record = self._active(record_id)
        if record.scope != "project":
            raise MemoryInputError(f"[{record.id}] is shared by every project; only a project memory can be moved.")
        if record.project_id == project.id:
            return f"[{record.id}] is already filed under project {project.name}."
        content_hash = _content_hash(record.text, "project", project.id)
        now = self._clock()
        before = record.project_name or record.project_id
        # The check and the move are one transaction, so two moves at once leave one copy.
        with self.store.transaction():
            duplicate = self.store.find_active_duplicate(content_hash)
            if duplicate is not None:
                raise MemoryInputError(f"Project {project.name} already has this memory as [{duplicate.id}]; "
                                       f"forget [{record.id}] instead.")
            self.store.ensure_project(project, now)
            if not self.store.move_record(record.id, project_id=project.id, content_hash=content_hash, now=now):
                raise MemoryInputError(f"[{record.id}] is no longer active; it was not moved.")
            note = self.store.notes_for([record.id]).get(record.id)
            if system_id is not None and note is not None and note["system_id"] != system_id:
                self.store.set_note(record.id, headline=note["headline"], system_id=system_id, facet=note["facet"],
                                    written_by=note["written_by"], now=now)
        self.record_event("move", record.text, outcome="moved", project_id=project.id, record_ids=[record.id],
                          details={"from": before, "to": project.name})
        return f"Moved [{record.id}] from project {before} to project {project.name}."

    def history(self, *, limit: int = DEFAULT_HISTORY_LIMIT) -> str:
        """Recently retired and replaced memories, so any removal can be undone."""

        records = self.store.list_inactive(limit=limit)
        if not records:
            return "No memory has been retired or replaced yet."
        lines = ["Recently retired or replaced memories, newest first. Bring one back with: knowitall2 restore <id>"]
        for row in records:
            change = f"replaced by [{row.superseded_by}]" if row.status == "superseded" else "retired"
            why = f" ({_shorten(row.retired_reason, 160)})" if row.retired_reason else ""
            lines.append(f"- [{row.id}] {row.updated_at[:10]} {change}{why}: {_shorten(row.text, 140)}")
        return "\n".join(lines)

    def briefing(
        self, *, project_path: str | Path | None = None, max_characters: int = DEFAULT_BRIEFING_CHARACTERS,
    ) -> str:
        """The session-start briefing: the user's rules, the project's key points, and a table of contents
        of what else is known for the project (its own memories and those of the systems it uses)."""

        project = self.project_for(project_path)
        project_id = project.id if project is not None else None
        title = f"KnowItAll2 briefing for project {project.name}" if project else "KnowItAll2 briefing"
        rules = self.store.list_active(project_id=project_id, scope="all", kinds=("rule",), limit=40)
        rules, instructed = _without_instructed(rules, project)
        rules, elsewhere = self._rules_for_its_systems(rules, project)
        entries, snapshots = self._contents(project) if project is not None else ([], 0)
        notice = self._question_notice()
        if not rules and not entries and not instructed and not elsewhere:
            self._log_usage("briefing", project, None, [])
            where = "this project" if project else "this folder"
            lines_out = [
                f"{title}\nNothing is stored for {where} yet. Use recall to search all memories, "
                "and remember to save durable facts."
            ]
            if notice:
                lines_out.append(notice)
            if project_id is not None:
                lines_out.extend(self._task_lines(project_id=project_id, record_ids=[]))
            return "\n".join(lines_out)
        footer = "Use recall with a few keywords before rediscovering something; save durable facts with remember."
        lines = _BoundedLines(max_characters - len(footer) - 48)
        lines.add(title)
        shown: list[str] = []
        if rules:
            lines.add("Your rules:")
            for row in rules:
                if lines.add_item(f"- [{row.id}] {_shorten(row.text, BRIEFING_RULE_CHARACTERS)}"):
                    shown.append(row.id)
        if instructed:
            names = " and ".join(sorted({name for _, name in instructed}))
            verb = "is" if len(instructed) == 1 else "are"
            lines.add(f"({len(instructed)} more of your rules {verb} left out: this project's {names} already says so.)")
        if elsewhere:
            names = ", ".join(sorted(set(elsewhere), key=str.casefold))
            verb = "is" if len(elsewhere) == 1 else "are"
            lines.add(f"({len(elsewhere)} more of your rules {verb} about other systems ({names}); "
                      "recall finds them when you work there.)")
        shown += _add_contents(lines, entries, now=self._clock())
        self._log_usage("briefing", project, None, shown)
        omitted = lines.omitted + snapshots
        if omitted:
            lines.force(f"(+{omitted} more; use recall to search.)")
        if notice:
            lines.force(notice)
        if project_id is not None:
            for line in self._task_lines(project_id=project_id, record_ids=[]):
                lines.force(line)
        lines.force(footer)
        return lines.text()

    def project_contents(
        self, project_id: str, *, known: Iterable[str] = (), max_characters: int = CONTENTS_CHARACTERS,
        header: str = MOVED_HEADER,
    ) -> str:
        """For a chat that started in another folder: this project's own rules, key points, and table of
        contents, without the user's global rules (the chat has them). Empty when nothing is stored."""

        project = self.stored_project(project_id)
        if project is None:
            return ""
        known = set(known)
        rules = [row for row in self.store.list_active(project_id=project_id, scope="project", kinds=("rule",),
                                                        limit=20) if row.id not in known]
        rules, _ = _without_instructed(rules, project)
        entries, snapshots = self._contents(project)
        entries = [entry for entry in entries if entry.record.id not in known]
        if not rules and not entries:
            return ""
        lines = _BoundedLines(max_characters)
        lines.add(header.format(name=project.name))
        shown: list[str] = []
        if rules:
            lines.add("The user's rules for this project:")
            for row in rules:
                if lines.add_item(f"- [{row.id}] {_shorten(row.text, BRIEFING_RULE_CHARACTERS)}"):
                    shown.append(row.id)
        shown += _add_contents(lines, entries, header="Also known:", now=self._clock())
        self._log_usage("briefing", project, None, shown)
        if lines.omitted + snapshots:
            lines.force(f"(+{lines.omitted + snapshots} more; use recall to search.)")
        return lines.text()

    def added_since(
        self, projects: Iterable[str], since: str, *, known: Iterable[str] = (),
    ) -> list[tuple[str, str]]:
        """Memories added from ``since`` on for these projects (their own, and their systems' shared ones),
        newest first, as (id, "System: headline"), leaving out ``known`` ones and current-state notes."""

        known = set(known)
        found: dict[str, tuple[str, str]] = {}
        for project_id in projects:
            project = self.stored_project(project_id)
            if project is None:
                continue
            entries, _ = self._contents(project, since=since, include_rules=True)
            for entry in sorted(entries, key=lambda item: item.record.created_at, reverse=True):
                if entry.record.id in known or entry.record.id in found:
                    continue
                label = "rule" if entry.record.kind == "rule" else entry.group
                found[entry.record.id] = (entry.record.created_at, f"{label}: {entry.headline}")
        ordered = sorted(found.items(), key=lambda item: item[1][0], reverse=True)
        return [(record_id, line) for record_id, (_, line) in ordered]

    def stored_project(self, project_id: str) -> ProjectIdentity | None:
        row = next((item for item in self.store.project_list() if item.get("id") == project_id), None)
        if row is None:
            return None
        paths = self.store.project_paths(project_id)
        return ProjectIdentity(project_id, row.get("name") or project_id, Path(paths[0]) if paths else Path(""),
                               row.get("remote"))

    def _contents(
        self, project: ProjectIdentity, *, since: str | None = None, include_rules: bool = False,
    ) -> tuple[list[_Entry], int]:
        """What is known for a project, most useful first, and how many current-state notes were left out."""

        from .catalog import find_system

        own = find_system(self.store.systems_list(), project.name)
        entries, snapshots = [], 0
        for item in self.store.contents(project.id, extra_systems=[own["id"]] if own else [], since=since,
                                        include_rules=include_rules):
            record = item["record"]
            facet = item["facet"] or _KIND_FACETS.get(record.kind, "other")
            if facet == "status":
                snapshots += 1
                continue
            group = item["system_name"] or "Other"
            entries.append(_Entry(record, _headline(item["headline"] or record.text, group), facet, group,
                                  item["weight"] + (1000 if own and item["system_id"] == own["id"] else 0)))
        entries.sort(key=_entry_order)
        return entries, snapshots

    def _rules_for_its_systems(
        self, rules: list[RecordRow], project: ProjectIdentity | None,
    ) -> tuple[list[RecordRow], list[str]]:
        """Split off the user's shared rules filed under a system this project's memories are not about.

        A rule about administering one system (say, gating GitLab behind a sign-in) would otherwise lead
        every project's briefing. Rules not filed under a system, the project's own, and rules tagged
        ``everywhere`` (a policy such as where every project keeps its credentials) always stay.
        Returns the rules kept and the system name of each one left out.
        """

        if project is None:
            return rules, []
        from .catalog import find_system

        shared = [row.id for row in rules if row.scope == "global"]
        notes = self.store.notes_for(shared) if shared else {}
        if not any(note.get("system_id") for note in notes.values()):
            return rules, []
        used = self.store.project_system_ids(project.id)
        own = find_system(self.store.systems_list(), project.name)
        if own:
            used.add(own["id"])
        kept, elsewhere = [], []
        for row in rules:
            note = notes.get(row.id) or {}
            system = note.get("system_id")
            everywhere = EVERYWHERE_TAG in (tag.casefold() for tag in row.tags)
            if row.scope == "global" and system and system not in used and not everywhere:
                elsewhere.append(note.get("system_name") or "another system")
            else:
                kept.append(row)
        return kept, elsewhere

    def _task_lines(self, *, project_id: str | None, record_ids: list[str]) -> list[str]:
        """At most one optional request for the agent, when one fits this project or these memories."""

        from . import review

        try:
            tasks = review.offer_tasks(self.store, project_id=project_id, record_ids=record_ids, now=self._clock())
        except Exception:
            return []
        if not tasks:
            return []
        return [TASK_OFFER_HEADER, *(f"- [{task['id']}] {task['prompt']}" for task in tasks)]

    def promote_rule(self, note: RecordRow, text: str) -> RecordRow:
        """Make a proposed rule a real one on the user's word, superseding the note."""

        body = _clean_text(text)
        now = self._clock()
        with self.store.transaction():
            record_id = self._new_id()
            self.store.insert_record(
                record_id=record_id, kind="rule", text=body, subjects=note.subjects, tags=note.tags,
                scope=note.scope, project_id=note.project_id, verification="user_stated", source_kind="user",
                source_agent=self.agent, content_hash=_content_hash(body, note.scope, note.project_id), now=now,
            )
            self.store.supersede(note.id, record_id, now=now)
        record = self.store.get(record_id)
        assert record is not None
        return record

    def _question_notice(self) -> str | None:
        """One briefing line about open questions, at most once every few days."""

        try:
            overdue = (_parse_time(self._clock()) - timedelta(days=QUESTION_REVIEW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            count = self.store.count_user_questions(overdue_before=overdue)
            if not count:
                return None
            shown = self.store.get_meta(QUESTIONS_SHOWN_KEY)
            if shown and (_parse_time(self._clock()) - _parse_time(shown)).days < QUESTION_NOTICE_DAYS:
                return None
        except Exception:
            return None
        noun = "question" if count == 1 else "questions"
        return f"KnowItAll2 has {count} {noun} for the user. When the user has a moment, show them with the questions tool."

    def _new_id(self) -> str:
        while True:
            candidate = "k-" + uuid.uuid4().hex[:10]
            if self.store.get(candidate) is None:
                return candidate

    def record_event(self, kind: str, summary: str, **fields) -> None:
        """Add an entry to the activity journal for this agent; never raises."""

        if self._record_events:
            journal.record(self.store, kind, summary, agent=self.agent, at=self._clock(), **fields)

    def _log_usage(
        self, operation: str, project: ProjectIdentity | None, query: str | None, record_ids: list[str],
    ) -> None:
        """Count this use for the usefulness report; a metrics problem never breaks a lookup."""

        project_id = project.id if project is not None else None
        query = _shorten(redact(query), 200) if query else None
        try:
            self.store.log_usage(
                at=self._clock(), operation=operation, agent=self.agent, project_id=project_id,
                query=query, record_ids=record_ids,
            )
        except Exception:
            pass
        where = f" for project {project.name}" if project is not None else ""
        if operation == "recall":
            summary = f'Recall "{query}"{where}: {len(record_ids) or "nothing"} found'
            outcome = "found" if record_ids else "nothing"
        else:
            noun = "memory" if len(record_ids) == 1 else "memories"
            summary = f"Briefing{where}: {len(record_ids)} {noun} shown"
            outcome = "shown" if record_ids else "empty"
        self.record_event(operation, summary, outcome=outcome, project_id=project_id, record_ids=record_ids)


@dataclass(frozen=True)
class _Term:
    """One query keyword, or the parts of a punctuated one (an address, path, or host)."""

    tokens: tuple[str, ...]

    @property
    def prefix(self) -> bool:
        return len(self.tokens) == 1 and len(self.tokens[0]) >= 3


def fts_query(query: str) -> str | None:
    """Build a safe FTS5 query: quoted terms joined by OR, with prefix matching.

    A term containing punctuation becomes a phrase of its parts so that it
    matches the same text the index holds.
    """

    return _fts_match(_query_terms(query))


def significant_words(text: str) -> set[str]:
    """The distinct words of ``text`` that can tell memories apart.

    Stopwords, very short words, bare numbers, and hash-like tokens are left out;
    names with digits, such as host names, are kept.
    """

    return {
        token for token in _tokens(text)
        if len(token) >= 3 and token not in _STOPWORDS and not token.isdigit() and not _HASH_LIKE.fullmatch(token)
    }


def keywords(text: str, *, limit: int) -> list[str]:
    """The most frequent meaningful words of ``text``, for finding related memories.

    Short words, numbers, and long hash-like tokens are skipped; the index's own
    ranking then favors the rarer ones.
    """

    counts: dict[str, int] = {}
    for token in _tokens(text):
        if 4 <= len(token) <= 30 and token not in _STOPWORDS and not any(character.isdigit() for character in token):
            counts[token] = counts.get(token, 0) + 1
    return sorted(counts, key=lambda token: (-counts[token], token))[:limit]


def _query_terms(query: str) -> list[_Term]:
    terms: list[_Term] = []
    for raw in query.split():
        tokens = tuple(_tokens(raw))
        if not tokens or (len(tokens) == 1 and tokens[0] in _STOPWORDS):
            continue
        term = _Term((_stem(tokens[0]),) if len(tokens) == 1 else tokens)
        if term not in terms:
            terms.append(term)
    return terms


def _stem(token: str) -> str:
    """A single keyword without a common ending; recall matches it as a prefix."""

    if token.isdigit():
        return token
    for ending in _ENDINGS:
        root = token[: -len(ending)]
        if token.endswith(ending) and len(root) >= _STEM_MINIMUM and not (ending == "s" and root[-1] in "siu"):
            return root
    return token


def _fts_match(terms: list[_Term]) -> str | None:
    pieces = ['"' + " ".join(term.tokens) + '"' + ("*" if term.prefix else "") for term in terms]
    return " OR ".join(pieces) if pieces else None


def _tokens(text: str) -> list[str]:
    """Casefold, strip accents, and split the way the full-text index does."""

    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(character for character in folded if not unicodedata.combining(character))
    return [token for token in _TERM_SPLIT.split(folded) if token]


def _row_tokens(row: RecordRow) -> list[str]:
    """A memory's text, subjects, and tags, split the way the index splits them."""

    return _tokens(" ".join([row.text, *row.subjects, *row.tags]))


def _covers(tokens: list[str], term: _Term) -> bool:
    if len(term.tokens) == 1:
        needle = term.tokens[0]
        return any(token == needle or (term.prefix and token.startswith(needle)) for token in tokens)
    width = len(term.tokens)
    return any(tuple(tokens[index:index + width]) == term.tokens for index in range(len(tokens) - width + 1))


class _BoundedLines:
    """Collect output lines without exceeding a character budget."""

    def __init__(self, budget: int) -> None:
        self._budget = max(budget, 120)
        self._lines: list[str] = []
        self._used = 0
        self.omitted = 0

    def add(self, line: str) -> None:
        self._lines.append(line)
        self._used += len(line) + 1

    def add_item(self, line: str) -> bool:
        """Add ``line`` if it fits the budget; report whether it was added."""

        if self._used + len(line) + 1 > self._budget:
            self.omitted += 1
            return False
        self.add(line)
        return True

    def force(self, line: str) -> None:
        self.add(line)

    def remaining(self) -> int:
        return self._budget - self._used

    def text(self) -> str:
        return "\n".join(self._lines)


@dataclass(frozen=True)
class _Entry:
    """One memory in a project's table of contents."""

    record: RecordRow
    headline: str
    facet: str
    group: str
    weight: int


def _entry_order(entry: _Entry) -> tuple:
    """Most useful first: the project's own memories, by part of the profile, then the newest."""

    rank = _FACET_ORDER.index(entry.facet) if entry.facet in _FACET_ORDER else len(_FACET_ORDER)
    recency = entry.record.confirmed_at or entry.record.updated_at
    return (entry.record.scope != "project", rank, -entry.weight, _descending(recency))


def _descending(text: str) -> tuple[int, ...]:
    return tuple(-ord(character) for character in text)


def _headline(text: str, group: str) -> str:
    """A memory's headline for a table of contents, without its system's name in front."""

    line = " ".join(text.split()).rstrip(".")
    name = group.casefold()
    if group != "Other" and line.casefold().startswith(name):
        rest = line[len(group):]
        for joint in ("'s ", "\u2019s ", ": ", " "):
            if rest.startswith(joint) and rest[len(joint):].strip():
                line = rest[len(joint):]
                break
    return _shorten(line, HEADLINE_CHARACTERS)


def _add_contents(
    lines: "_BoundedLines", entries: list[_Entry], *, header: str = CONTENTS_HEADER, now: str | None = None,
) -> list[str]:
    """Add the key points and the project's latest memories in full, then a table of contents of the rest;
    returns the ids shown. ``now`` sets what counts as latest; without it there is no latest part."""

    shown: list[str] = []
    key = [entry for entry in entries if entry.record.scope == "project"][:KEY_POINTS]
    latest = _latest(entries, key, now) if now else []
    for title, part in (("Key points for this project:", key), ("Latest for this project:", latest)):
        if not part:
            continue
        lines.add(title)
        for entry in part:
            row = entry.record
            item = f"- [{row.id}] {row.kind}: {_shorten(row.text, BRIEFING_ITEM_CHARACTERS)} ({_briefing_provenance(row)})"
            if lines.add_item(item):
                shown.append(row.id)
    rest = [entry for entry in entries if entry not in key and entry not in latest]
    if not rest:
        return shown
    budget = lines.remaining() - len(header) - 1
    groups: dict[str, list[_Entry]] = {}
    used = 0
    for entry in rest:
        cost = len(entry.headline) + 6 + (0 if entry.group in groups else len(entry.group) + 4)
        if used + cost > budget:
            lines.omitted += 1
            continue
        groups.setdefault(entry.group, []).append(entry)
        used += cost
    if not groups:
        return shown
    lines.add(header)
    order = sorted(groups, key=lambda name: (name == "Other", -len(groups[name])))
    for name in order:
        lines.add(f"- {name}:")
        for entry in groups[name]:
            lines.add(f"  - {entry.headline}")
            shown.append(entry.record.id)
    return shown


def _latest(entries: list[_Entry], key: list[_Entry], now: str) -> list[_Entry]:
    """The project's newest memories from the last few days, which the key points (how-tos first) miss.

    What a chat just found is what the next chat in the project most likely needs, and the table of
    contents would give it only a shortened headline.
    """

    cutoff = _parse_time(now) - timedelta(hours=LATEST_HOURS)
    recent = [entry for entry in entries if entry.record.scope == "project" and entry not in key
              and _parse_time(entry.record.created_at) >= cutoff]
    recent.sort(key=lambda entry: entry.record.created_at, reverse=True)
    return recent[:LATEST_SHOWN]


def _without_instructed(
    rules: list[RecordRow], project: ProjectIdentity | None,
) -> tuple[list[RecordRow], list[tuple[RecordRow, str]]]:
    """Split off the rules the project's own instruction files already state, which agents read anyway."""

    sections = _instruction_sections(project)
    if not sections:
        return rules, []
    kept, instructed = [], []
    for row in rules:
        found = None
        if len(significant_words(row.text)) >= _INSTRUCTED_MINIMUM_WORDS:
            found = next((name for name, section in sections if quoted_in(row.text, [section])), None)
        if found:
            instructed.append((row, found))
        else:
            kept.append(row)
    return kept, instructed


def _instruction_sections(project: ProjectIdentity | None) -> list[tuple[str, str]]:
    """Each section of the project's AGENTS.md and CLAUDE.md, by file name."""

    if project is None or not str(project.root) or str(project.root) == ".":
        return []
    sections = []
    for name in _INSTRUCTION_FILES:
        path = Path(project.root) / name
        try:
            if not path.is_file() or path.stat().st_size > _INSTRUCTION_FILE_LIMIT:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for part in re.split(r"(?m)^(?=#)", text):
            if part.strip():
                sections.append((name, part))
    return sections


def _briefing_provenance(row: RecordRow) -> str:
    date = (row.confirmed_at or row.created_at)[:10]
    return f"stated by the user, {date}" if row.verification == "user_stated" else f"{row.verification}, {date}"


def _require_choice(value: object, choices: tuple[str, ...], name: str) -> None:
    if value not in choices:
        raise MemoryInputError(f"Unknown {name} {value!r}; use one of: {', '.join(choices)}.")


def _clean_text(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    cleaned: list[str] = []
    for line in text.strip().splitlines():
        compact = " ".join(line.split())
        if compact or (cleaned and cleaned[-1]):
            cleaned.append(compact)
    return "\n".join(cleaned).strip()


def _clean_labels(values: Iterable[str], field: str) -> list[str]:
    if isinstance(values, str):
        values = [values]
    seen: set[str] = set()
    labels: list[str] = []
    for value in values:
        label = " ".join(str(value).split())
        if not label:
            continue
        if len(label) > MAX_LABEL_CHARACTERS:
            raise MemoryInputError(f"Each of the {field} must be at most {MAX_LABEL_CHARACTERS} characters.")
        if label.casefold() not in seen:
            seen.add(label.casefold())
            labels.append(label)
    if len(labels) > MAX_LABELS:
        raise MemoryInputError(f"Use at most {MAX_LABELS} {field}.")
    return labels


def _content_hash(text: str, scope: str, project_id: str | None) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", text).casefold().split()).rstrip(".!")
    return hashlib.sha256(f"{scope}|{project_id or ''}|{normalized}".encode("utf-8")).hexdigest()


def _score(row: RecordRow, rank: float, project_id: str | None, now: datetime | None = None) -> float:
    score = -rank
    if project_id is not None and row.project_id == project_id:
        score *= 1.3
    if row.kind == "rule":
        score *= 1.2
    score *= _VERIFICATION_BOOST[VERIFICATION_STRENGTH[row.verification]]
    return score * (_freshness(row, now) if now is not None else 1.0)


def _freshness(row: RecordRow, now: datetime) -> float:
    """1.0 for memories confirmed in the last 90 days, easing to 0.6 over the following year.

    The user's own statements do not decay.
    """

    if row.verification == "user_stated":
        return 1.0
    try:
        age = (now - _parse_time(row.confirmed_at or row.updated_at)).days
    except ValueError:
        return 1.0
    if age <= FRESH_DAYS:
        return 1.0
    return max(FRESHNESS_FLOOR, 1.0 - (age - FRESH_DAYS) / 365 * (1.0 - FRESHNESS_FLOOR))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _scope_label(row: RecordRow, project: ProjectIdentity | None) -> str:
    if row.scope != "project":
        return "global"
    if project is not None and row.project_id == project.id:
        return "this project"
    return f"project {row.project_name or row.project_id}"


def _provenance(row: RecordRow) -> str:
    date = (row.confirmed_at or row.created_at)[:10]
    via = f" via {row.source_agent}" if row.source_agent else ""
    if row.source_session:
        via += f" from session {row.source_session[:8]}"
    if row.verification == "user_stated":
        return f"stated by the user{via}, {date}"
    return f"{row.verification}{via}, {date}"


def _format_recall(
    query: str, records: list[RecordRow], project: ProjectIdentity | None, *,
    partial: Sequence[RecordRow] = (), unknown: Sequence[str] = (),
) -> str:
    missing = f"No memory mentions: {', '.join(unknown)}." if unknown else ""
    if not records and not partial:
        return "\n".join(part for part in (
            f'No memories match "{query}".', missing,
            "Try fewer keywords, or the name of the system, project, or tool.") if part)
    if records:
        noun = "memory" if len(records) == 1 else "memories"
        lines = [f'{len(records)} {noun} for "{query}":']
    else:
        lines = [f'No memory matches all of "{query}".']
    for index, row in enumerate([*records, *partial], 1):
        if partial and index == len(records) + 1:
            lines.append("Partial matches (they share only some of the keywords; check that they fit):")
        details = [row.kind]
        if row.subjects:
            details.append("about " + ", ".join(row.subjects))
        details.append(_scope_label(row, project))
        details.append(_provenance(row))
        lines.append(f"{index}. [{row.id}] " + " | ".join(details))
        lines.extend("   " + part for part in row.text.splitlines())
    if missing:
        lines.append(missing)
    return "\n".join(lines)


def _shorten(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."
