"""Small, task-specific memory excerpts for a user's message, without a model call.

A file pointer or catalog headline is discovery, not the details of a memory. Keep
delivery of these excerpts separate from ``chats.told`` so a headline does not
prevent the relevant procedure from reaching the agent when it needs it.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any

from . import known
from .memory import (DISPUTED_TAG, PROVISIONAL_TAG, _covers, _fts_match, _provenance,
                     _query_terms, _row_tokens, _scope_label, _tokens, significant_words)
from .secrets import redact

MAX_TERMS = 24
MAX_CANDIDATES = 60
MAX_MEMORIES = 2
MAX_PROJECTS = 3
MAX_CHARACTERS = 2600
HEADER = "KnowItAll2 details relevant to this message (check their provenance and applicability):"
EXPERIMENT_ENV = "KNOWITALL2_CONTEXT_DETAILS"


def enabled() -> bool:
    """Opt-in while decision-level evaluation has not established usefulness."""
    return os.environ.get(EXPERIMENT_ENV, "").strip().casefold() in {"1", "true", "on"}


# Conversational/task scaffolding is not a subject. Manual recall keeps its own
# stricter query semantics; this filter applies only to automatic context.
SCAFFOLD = frozenset("work working works use using used please want need help look check investigate "
                     "review continue current latest recent right now instead already available "
                     "something things going make sure would could should also well".split())
_WORD = re.compile(r"[^\W_]+")


def _phrase_length(prompt: str, text: str) -> int:
    """Longest shared phrase with at least two meaningful words (e.g. an error).

    Keeping intervening words distinguishes 'blocked by policy' from unrelated
    memories about blocklists and policies. Nothing is executed or interpreted.
    """
    words = _WORD.findall(prompt.casefold())[:256]
    body = " " + " ".join(_WORD.findall(text.casefold())) + " "
    for size in range(min(8, len(words)), 2, -1):
        for start in range(len(words) - size + 1):
            phrase = " ".join(words[start:start + size])
            if len(significant_words(phrase) - SCAFFOLD) >= 2 and f" {phrase} " in body:
                return size
    return 0


def for_message(memory: Any, prompt: str | None, *, project_path: str | None = None,
                project: Any = None, related_projects: tuple = (), seen: list[str],
                as_of: str | None = None) -> tuple[str, list[str]]:
    """Return a bounded context block and fingerprints of the full excerpts.

    Only the user's actual message is searched. Require a named catalog subject
    and another keyword, or a shared phrase of at least three words; generic
    'continue' messages, ambient app context and incidental single-word matches
    must not add context. Search shared memory, the working projects, and projects
    explicitly named in the message. Never expand silently to every project.
    """
    users, _agents = known.message_parts(prompt)
    text = redact("\n".join(users)[:known.POINTER_TEXT_LIMIT])
    words = significant_words(text) - SCAFFOLD
    if len(words) < 2:
        return "", []
    project = project or (memory.project_for(project_path) if project_path else None)
    project_id = project.id if project else None
    table = known.name_table(memory.store._connection)
    anchors = set(known.pointer_targets(table, users))
    # The hostname of an explicit URL is a subject; a project name incidentally
    # present in an unrelated URL's path remains excluded by command_names.
    anchors.update(known.command_names(text, table))
    names: dict[str, list[str]] = {}
    for row in memory.store._connection.execute("SELECT id, name FROM projects"):
        names.setdefault(known.file_name(str(row[1])), []).append(row[0])
    explicit = [ids[0] for target in known.command_names(text, table)
                if len(ids := names.get(target, [])) == 1]
    scope_ids = list(dict.fromkeys([*([project_id] if project_id else []), *explicit,
                                   *(p.id for p in related_projects if p)]))[:MAX_PROJECTS]
    # Bound database work before counting term frequencies, even for a pasted
    # document. Preserve the message's order rather than alphabetically dropping
    # names at the end of the alphabet.
    terms = _query_terms(" ".join(dict.fromkeys(word for word in _tokens(text) if word in words)))[:MAX_TERMS]
    candidates = memory.store.context_candidates(_fts_match(terms) or "", project_ids=scope_ids,
                                                limit=MAX_CANDIDATES, as_of=as_of)
    # Frequency queries across the complete store cost several seconds on some
    # real prompts. Rank within this one bounded candidate set instead. Manual
    # recall retains its full-store ranking and query-coverage semantics.
    tokenized = {row.id: _row_tokens(row) for row, _ in candidates}
    covered = {term: sum(_covers(tokens, term) for tokens in tokenized.values()) for term in terms}
    weights = {term: math.log(1 + len(candidates) / count) for term, count in covered.items() if count}
    if len(weights) < 2:
        return "", []
    terms = sorted(weights, key=lambda term: (-weights[term], term.tokens))[:MAX_TERMS]
    total = sum(weights[term] for term in terms)
    notes = memory.store.notes_for([row.id for row, _ in candidates])
    ranked = []
    for row, rank in candidates:
        # Automatic excerpts must not promote unchecked learning into an
        # operational instruction. Such leads remain available through recall
        # and the existing labeled knowledge files.
        if row.verification == "unverified" or {DISPUTED_TAG, PROVISIONAL_TAG}.intersection(row.tags):
            continue
        if (notes.get(row.id) or {}).get("facet") == "status":
            continue
        fingerprint = row.id + ":" + hashlib.sha256(
            (row.text + row.verification + repr(row.tags)).encode("utf-8")).hexdigest()[:16]
        if fingerprint in seen:
            continue
        tokens = tokenized[row.id]
        hits = [term for term in terms if _covers(tokens, term)]
        share = sum(weights[term] for term in hits) / total
        if len(hits) < 2 or share < .35:
            continue
        phrase = _phrase_length(text, row.text)
        subjects = anchors.intersection(known.command_names(row.text, table))
        anchor_hits = len(subjects)
        in_task = row.project_id in scope_ids and len(hits) >= 3 and share >= .65
        if not anchor_hits and phrase < 3 and not in_task:
            continue
        # A row about both named subjects is stronger than a longer matching
        # phrase about only an incidental alternative (Brave vs Computer Use).
        ranked.append((anchor_hits, share, phrase, -rank, row.id, row, fingerprint,
                       subjects))
    ranked.sort(key=lambda item: item[:5], reverse=True)
    lines, delivered, ids, covered = [HEADER], [], [], set()
    for _anchors, _share, _phrase, _rank, _id, row, fingerprint, subjects in ranked:
        # A second excerpt must cover a different named subject. Do not pad a
        # useful route with a weaker alternative for the very same subject.
        if delivered and not subjects.difference(covered):
            continue
        entry = (f"- [{row.id}] {_scope_label(row, project)}; {_provenance(row)}\n"
                 f"  {redact(row.text)}")
        # Never cut a procedure halfway through a caveat or a negation.
        if len("\n".join([*lines, entry])) > MAX_CHARACTERS:
            continue
        lines.append(entry)
        delivered.append(fingerprint)
        ids.append(row.id)
        covered.update(subjects)
        if len(delivered) == MAX_MEMORIES:
            break
    if not delivered:
        return "", []
    # This is an offer, not proof the agent used it. Keep it out of the shared
    # usage queue: released servers accept only recall/briefing operations.
    memory._write(lambda: memory.record_event(
        "context", f"Relevant memory details offered: {len(ids)}", outcome="offered",
        project_id=project_id, record_ids=ids))
    return "\n".join(lines), delivered
