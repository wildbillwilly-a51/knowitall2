"""The final pass, which learns a settled stretch of a chat and decides what the quick pass learned from it.

While a chat goes on, the quick pass learns at each commit (or when asked)
from an excerpt, without knowing how the chat will end; what it saves is
provisional, and other chats see it at once. Once the chat has been quiet
for a while, or has ended, this pass reads the whole stretch since the last
one, in parts of at most ``PART_CHARACTERS``, each told how the stretch
ended. For each provisional memory it decides: confirm, correct, or retire;
and it adds what the quick pass missed. Then one call checks each memory
kept against the memories most like it: new, an update of one (merged),
a repeat, or not lasting after all.

Replayed on ten real chats (2026-10-13): 65% of the memories kept passed
every check, against 37% for learning at commits alone; the bad provisional
memories were mostly retired or corrected.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .. import journal
from ..memory import PREFERENCE_TAG, PROVISIONAL_TAG, Memory, significant_words
from ..quotes import nearly_quoted_in, quoted_in, short_quote_in
from ..store import RecordRow
from .dossier import Dossier, _render
from .extractor import render_input
from .prompts import FINAL_CANDIDATES, FINAL_PROMPT, FINAL_SCHEMA, RECONCILE_PROMPT, RECONCILE_SCHEMA
from .transcripts import Session

PART_CHARACTERS = 150_000
ENDING_CHARACTERS = 10_000
NEAR_PER_PROVISIONAL = 5
NEAR_PER_CANDIDATE = 8
NEAR_ITEM_CHARACTERS = 300
# Reasons the final pass gives for retiring, as the journal and the news say them.
_RETIRED = {
    "overturned": "the chat later overturned it", "wrong": "the chat does not support it",
    "status": "a status, not lasting knowledge", "work record": "a record of the chat's own work",
    "repeat": "already known", "unresolved": "left unresolved", "not lasting": "not lasting",
}


@dataclass
class _Item:
    key: str
    candidate: dict[str, Any]
    record: RecordRow | None = None   # the provisional memory, for the quick pass's
    status: str = "pending"           # pending, deferred, confirmed, corrected, retired
    part: int | None = None           # where a correction or an addition was made
    reason: str = ""
    repeat_of: str = ""


@dataclass
class Settled:
    calls: int = 0
    outcomes: Counter = field(default_factory=Counter)
    results: list[dict[str, Any]] = field(default_factory=list)
    usage: Counter = field(default_factory=Counter)


def needed_calls(parts: list[Dossier]) -> int:
    """The calls the final pass of these parts takes: one a part, and one to check what it keeps."""

    return len(parts) + 1


def ending_of(session: Session) -> str:
    """How the stretch ended: its closing messages (the user's, the agent's, and helper reports; not tool calls)."""

    entries, size = [], 0
    for event in reversed(session.events):
        entry, _user, _output = _render(event)
        if not entry or entry.startswith("[tool "):
            continue
        entries.append(entry)
        size += len(entry)
        if size >= ENDING_CHARACTERS:
            break
    return "".join(reversed(entries))[-(ENDING_CHARACTERS + 2_000):]


def settle(memory: Memory, extractor: Any, parts: list[Dossier], *, session: Session, run: str | None,
           on_call: Any = None, partial: bool = False) -> Settled:
    """The final pass over ``parts``, the settled stretch of ``session``; see the module's description.

    ``on_call`` is told of each model call as it is made, for the budget. An ``ExtractionError`` stops the pass
    before anything is changed, so a retry starts over. ``partial``: the parts are only the first slice of the
    stretch (it needs more calls than a day allows), so a provisional memory no part bore on stays provisional
    for the next slice, instead of standing.
    """

    from .learner import known_context

    report = Settled()
    session_id = parts[0].session_id if parts else session.session_id
    provisional = memory.store.tagged_active(PROVISIONAL_TAG, session=session_id) if session_id else []
    candidates = [(record, _as_candidate(memory, record)) for record in provisional]
    if partial:
        # A slice reviews only what it shows; the rest waits for the slice that reads its stretch (C-L11).
        candidates = [(record, candidate) for record, candidate in candidates if _in_parts(candidate, parts)]
    items = [_Item(f"p{number}", candidate, record) for number, (record, candidate) in enumerate(candidates, 1)]
    mine = {record.id for record in provisional}
    ending = ending_of(session)
    reviewing = callable(getattr(extractor, "run", None))
    for number, part in enumerate(parts):
        part.known = known_context(memory, part, exclude=mine)
        if reviewing:
            payload = _call(extractor, report, _final_input(memory, items, part, number, len(parts), ending, mine,
                                                            partial=partial),
                            FINAL_SCHEMA, FINAL_PROMPT, on_call)
            reviews = payload.get("reviews") if isinstance(payload.get("reviews"), list) else []
            memories = payload.get("memories") if isinstance(payload.get("memories"), list) else []
        else:
            # An engine without a final pass (a stand-in): what it learns, with nothing to review.
            memories = _extract(extractor, report, part, on_call)
            reviews = []
        _review(items, reviews, number)
        for candidate in [item for item in memories if isinstance(item, dict)][:FINAL_CANDIDATES]:
            items.append(_Item(f"n{len(items) + 1}", candidate, status="added", part=number))
    for item in items:
        if item.status in ("pending", "deferred") and not partial:
            item.status = "confirmed"   # no part bore on it: the quick pass's word stands
    kept = [item for item in items if item.status in ("confirmed", "corrected", "added")]
    decisions: dict[int, dict[str, Any]] = {}
    shown: dict[int, list[RecordRow]] = {}
    if kept and reviewing:
        decisions, shown = _reconcile(memory, extractor, report, kept, mine, on_call)
    _apply(memory, items, kept, decisions, shown, parts, report, run)
    return report


def _as_candidate(memory: Memory, record: RecordRow) -> dict[str, Any]:
    """A provisional memory as the candidate it was, with the evidence the journal kept of it."""

    evidence = ""
    for event in memory.store.events(kinds=["candidate"], record_id=record.id, limit=5):
        if event["record_ids"][:1] == [record.id] and event["details"].get("evidence"):
            evidence = str(event["details"]["evidence"])
            break
    kind = "preference" if record.kind == "note" and "preference" in record.tags else record.kind
    return {"text": record.text, "kind": kind, "subjects": list(record.subjects), "scope": record.scope,
            "project": record.project_name or "", "evidence": evidence, "relation": "new", "known_id": ""}


def _call(extractor: Any, report: Settled, text: str, schema: dict[str, Any], prompt: str,
          on_call: Any) -> dict[str, Any]:
    try:
        answer = extractor.run(text, schema=schema, system_prompt=prompt)
    finally:
        report.usage.update(getattr(extractor, "last_usage", None) or {})
    _made(report, on_call)
    return answer


def _extract(extractor: Any, report: Settled, part: Dossier, on_call: Any) -> list[dict[str, Any]]:
    try:
        answer = extractor.extract(part)
    finally:
        report.usage.update(getattr(extractor, "last_usage", None) or {})
    _made(report, on_call)
    return answer


def _made(report: Settled, on_call: Any) -> None:
    """Count a call that answered; one that failed is counted by the caller, which knows whether it was the
    engine's fault (and then no call is blamed on anything)."""

    report.calls += 1
    if on_call is not None:
        on_call()


def _final_input(memory: Memory, items: list[_Item], part: Dossier, number: int, total: int, ending: str,
                 mine: set[str], *, partial: bool = False) -> str:
    living = [item for item in items if item.record is not None and item.status not in ("retired",)]
    lines = ["Provisional memories from this session (review each):" if living else
             "Provisional memories from this session: none."]
    for item in living:
        candidate = item.candidate
        place = candidate.get("scope") or ""
        if place == "project" and candidate.get("project"):
            place += f" {candidate['project']}"
        lines.append(f"- [{item.key}] {candidate.get('kind')} ({place}): {_line(candidate.get('text'))}")
        for _share, row in memory.nearest(_about(candidate), project_id=None, scope="everywhere",
                                          limit=NEAR_PER_PROVISIONAL, exclude=mine):
            lines.append(f"    nearest existing [{row.id}] {row.kind} ({_where(row)}): "
                         f"{_line(row.text)[:NEAR_ITEM_CHARACTERS]}")
    body = render_input(part).replace("Session excerpt:\n", f"Session, part {number + 1} of {total}:\n", 1)
    if not part.known:
        body = f"Session, part {number + 1} of {total}:\n" + body
    last = number == total - 1 and not partial   # a slice's last part is not the end (D-M3)
    tail = ("\n\nThis part is the end of the stretch." if last else
            "\n\nHow the stretch ended (its closing messages):\n" + ending)
    return "\n".join(lines) + "\n\n" + body + tail


def _review(items: list[_Item], reviews: list[Any], number: int) -> None:
    by_key = {item.key: item for item in items if item.record is not None}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        item = by_key.get(str(review.get("id") or "").strip().strip("[]"))
        if item is None or item.status == "retired":
            continue
        action = review.get("action")
        if action == "retire" and not _lasting_lesson(item, review):
            item.status, item.reason = "retired", str(review.get("retire_reason") or "")
            item.repeat_of = str(review.get("repeat_of") or "").strip().strip("[]")
        elif action == "correct":
            item.candidate = _corrected(item.candidate, review)
            item.status, item.part = "corrected", number
        elif action in ("confirm", "retire"):
            if item.status != "corrected":
                item.status = "confirmed"
        elif action == "not_here" and item.status == "pending":
            item.status = "deferred"


def _lasting_lesson(item: _Item, review: dict[str, Any]) -> bool:
    """A tool's quirk is lasting: one retired as "not lasting" was among the good memories the replay lost."""

    return review.get("retire_reason") == "not lasting" and item.candidate.get("kind") == "lesson"


def _corrected(old: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(old)
    for key in ("text", "kind", "scope", "project", "evidence"):
        if review.get(key):
            candidate[key] = review[key]
    if isinstance(review.get("subjects"), list) and review["subjects"]:
        candidate["subjects"] = review["subjects"]
    if candidate.get("scope") == "global":
        candidate["project"] = ""
    candidate["relation"], candidate["known_id"] = "new", ""
    return candidate


def _reconcile(memory: Memory, extractor: Any, report: Settled, kept: list[_Item], mine: set[str],
               on_call: Any) -> tuple[dict[int, dict[str, Any]], dict[int, list[RecordRow]]]:
    lines, shown = [], {}
    words = [_words(item.candidate) for item in kept]
    for index, item in enumerate(kept):
        candidate = item.candidate
        rows = [row for _share, row in memory.nearest(_about(candidate), project_id=None, scope="everywhere",
                                                       limit=NEAR_PER_CANDIDATE, exclude=mine)]
        shown[index] = rows
        project = f", project {candidate['project']}" if candidate.get("project") else ""
        lines.append(f"Candidate {index} ({candidate.get('kind')}, {candidate.get('scope')}{project}): "
                     f"{_line(candidate.get('text'))}")
        lines += [f"  - [{row.id}] {row.kind} ({_where(row)}): {_line(row.text)[:400]}" for row in rows]
        # An earlier candidate of this session saying much the same: a repeat of it names [c<index>]. The chat
        # learning itself again was most of the repeats (candidate review, C-M4).
        lines += [f"  - [c{other}] candidate {other} of this session: {_line(kept[other].candidate.get('text'))[:400]}"
                  for other in range(index) if _overlap(words[index], words[other]) >= CANDIDATE_OVERLAP]
        lines.append("")
    payload = _call(extractor, report, "\n".join(lines), RECONCILE_SCHEMA, RECONCILE_PROMPT, on_call)
    decisions = {}
    for decision in payload.get("decisions") or []:
        if isinstance(decision, dict) and isinstance(decision.get("index"), int):
            decisions[decision["index"]] = decision
    return decisions, shown


def _apply(memory: Memory, items: list[_Item], kept: list[_Item], decisions: dict[int, dict[str, Any]],
           shown: dict[int, list[RecordRow]], parts: list[Dossier], report: Settled, run: str | None) -> None:
    # One transaction: a store failure part-way changes nothing, and the retry starts over (C-L10).
    with memory.store.transaction():
        _apply_all(memory, items, kept, decisions, shown, parts, report, run)


def _apply_all(memory: Memory, items: list[_Item], kept: list[_Item], decisions: dict[int, dict[str, Any]],
               shown: dict[int, list[RecordRow]], parts: list[Dossier], report: Settled, run: str | None) -> None:
    from .learner import preference_match, publish_with_ids, result_text

    standing: dict[int, str | None] = {}   # each kept candidate's memory that stands for it, once decided

    def note(outcome: str, item: _Item, ids: list[str]) -> None:
        report.outcomes[outcome] += 1
        report.results.append({"outcome": outcome, "ids": ids, "text": result_text(outcome, item.candidate)})

    siblings = {item.key: item for item in items if item.record is not None}
    repeats: list[tuple[_Item, _Item]] = []   # repeats of another provisional memory, decided after it is
    for item in items:
        if item.status == "retired" and item.record is not None:
            sibling = siblings.get(item.repeat_of)
            if sibling is not None and sibling is not item:
                repeats.append((item, sibling))
                continue
            _retire(memory, item, run)
            note("retired", item, [item.record.id])
    for index, item in enumerate(kept):
        decision = decisions.get(index) or {"action": "new"}
        action = decision.get("action")
        earlier = _earlier_candidate(decision, index)
        if action == "repeat" and earlier is not None:
            target = _standing(memory, standing.get(earlier))
            if target is not None:
                if item.record is not None and target != item.record.id:
                    item.reason, item.repeat_of = "repeat", target
                    _retire(memory, item, run)
                    note("retired", item, [item.record.id])
                elif item.record is None:
                    note("rejected (repeat)", item, [])
                standing[index] = target
                continue
            # What it repeats did not stand: it is decided on its own (second candidate review, D-M5).
            decision, action = {"action": "new"}, "new"
        if action in ("repeat", "drop"):
            known = _shown(shown, index, decision)
            if item.record is not None:
                item.reason = "repeat" if action == "repeat" else "not lasting"
                item.repeat_of = known.id if known is not None else ""
                _retire(memory, item, run)
                note("retired", item, [item.record.id])
            else:
                note(f"rejected ({'repeat' if action == 'repeat' else 'not lasting'})", item, [])
            standing[index] = known.id if action == "repeat" and known is not None else None
            continue
        candidate = dict(item.candidate)
        dossier = _dossier_for(candidate, item, parts)
        merge = False
        if action == "update":
            known = _shown(shown, index, decision)
            if known is not None:
                dossier = dataclasses.replace(dossier, known=[*dossier.known, known])
                candidate.update(relation="updates", known_id=known.id, own_text=candidate.get("text"),
                                 text=str(decision.get("text") or "").strip() or candidate.get("text"))
                merge = True
        preference = item.record is not None and PREFERENCE_TAG in item.record.tags and not merge
        if preference:
            candidate.update(kind="preference", relation="new", known_id="")
            # Only when the user said it again, changed it, or a rule holds it is there anything to do; otherwise it
            # stands as it is, under the id other chats were shown (second candidate review, D-L1).
            preference = item.status == "corrected" or preference_match(memory, candidate, dossier,
                                                                         exclude=item.record.id)
        if item.record is not None and item.status == "confirmed" and not merge and not preference:
            _confirm(memory, item.record)
            note("confirmed", item, [item.record.id])
            standing[index] = item.record.id
            continue
        if preference:
            # Out of the way while it is checked, so it is not taken for the preference it may repeat; one that
            # something already decided is left as it is (D-L3).
            if not memory.store.retire(item.record.id, reason="checked again by the final pass", now=memory.now()):
                standing[index] = _standing(memory, item.record.id)
                continue
            outcome, ids = publish_with_ids(memory, candidate, dossier, run=run)
            memory.store.restore(item.record.id, verification=item.record.verification, now=memory.now())
        else:
            outcome, ids = publish_with_ids(memory, candidate, dossier, run=run, merge=merge)
        if item.record is None:
            note(outcome, item, ids)
            standing[index] = ids[0] if ids and not outcome.startswith("rejected") else None
            continue
        if (not ids or outcome.startswith("rejected")) and item.status == "corrected":
            # The final pass found something wrong with it, and its correction did not pass the checks: neither
            # is kept, as in the tested design (the 2026-10-13 replay of the built code).
            item.reason = "wrong"
            _retire(memory, item, run)
            note("retired", item, [item.record.id])
            standing[index] = None
        elif not ids or outcome.startswith("rejected") or ids[0] == item.record.id:
            # A preference checked again and turned down for a reason of the checks' own stands as it was.
            _confirm(memory, item.record)
            note("confirmed", item, [item.record.id])
            standing[index] = item.record.id
        else:
            memory.store.supersede(item.record.id, ids[0], now=memory.now(),
                                   reason=f"the final pass corrected it as [{ids[0]}]")
            note("corrected" if outcome in ("saved", "saved with a question") else outcome, item, ids)
            standing[index] = ids[0]
    for item, sibling in repeats:
        # A repeat of a sibling points to what stands for the sibling. When nothing does, the sibling was found
        # wrong, overturned, or not lasting, and what repeats it goes the same way (the third replay, 2026-10-14:
        # a repeat of a memory retired as overturned came back as a work record).
        target = _standing(memory, sibling.record.id)
        if target == item.record.id:
            _confirm(memory, item.record)
            note("confirmed", item, [item.record.id])
            continue
        item.repeat_of = target or ""
        if target is None:
            item.reason = sibling.reason or "repeat"
        _retire(memory, item, run)
        note("retired", item, [item.record.id])
    if shown:
        # What was checked against its nearest memories needs no maintenance review until it changes.
        from .maintenance import mark_reviewed

        checked = {result["ids"][0] for result in report.results
                   if result["ids"] and result["outcome"] in ("confirmed", "corrected", "saved", "updated")}
        mark_reviewed(memory, [record for record in map(memory.store.get, sorted(checked)) if record])


def _standing(memory: Memory, record_id: str | None) -> str | None:
    """The active memory that stands for ``record_id``: itself, or what replaced it, through any number of steps."""

    seen: set[str] = set()
    record = memory.store.get(record_id) if record_id else None
    while record is not None and record.status == "superseded" and record.superseded_by and record.id not in seen:
        seen.add(record.id)
        record = memory.store.get(record.superseded_by)
    return record.id if record is not None and record.status == "active" else None


def _shown(shown: dict[int, list[RecordRow]], index: int, decision: dict[str, Any]) -> RecordRow | None:
    known_id = str(decision.get("known_id") or "").strip().strip("[]")
    for row in shown.get(index, []):
        if row.id == known_id and row.status == "active":
            return row
    return None


def _dossier_for(candidate: dict[str, Any], item: _Item, parts: list[Dossier]) -> Dossier:
    """The part whose text holds the candidate's evidence, so the checks see where it came from."""

    evidence = _line(candidate.get("evidence"))
    preferred = [parts[item.part]] if item.part is not None and item.part < len(parts) else []
    for dossier in [*preferred, *parts]:
        if evidence and _shows(evidence, dossier):
            return dossier
    return preferred[0] if preferred else parts[0]


def _confirm(memory: Memory, record: RecordRow) -> None:
    with memory.store.transaction():
        current = memory.store.get(record.id)
        if current is None or PROVISIONAL_TAG not in current.tags:
            return
        memory.store.set_tags(record.id, [tag for tag in current.tags if tag != PROVISIONAL_TAG], now=memory.now())


def _retire(memory: Memory, item: _Item, run: str | None) -> None:
    record = item.record
    assert record is not None
    why = _RETIRED.get(item.reason, item.reason or "not kept")
    now = memory.now()
    target = memory.store.get(item.repeat_of) if item.repeat_of else None
    with memory.store.transaction():
        if target is not None and target.status == "active" and target.id != record.id:
            memory.store.supersede(record.id, target.id, now=now, reason=f"the final pass found it {why} [{target.id}]")
        else:
            memory.store.retire(record.id, reason=f"the final pass retired it ({why})", now=now)
    journal.record(memory.store, "candidate", record.text, outcome="retired", agent=memory.agent,
                   project_id=record.project_id, session=record.source_session, run=run, record_ids=[record.id],
                   details={"result": "retired by the final pass", "reason": why}, at=now)


# Two candidates of one pass this alike are shown to the check side by side.
CANDIDATE_OVERLAP = 0.5


def _words(candidate: dict[str, Any]) -> set[str]:
    return set(significant_words(_about(candidate)))


def _overlap(first: set[str], second: set[str]) -> float:
    return len(first & second) / min(len(first), len(second)) if first and second else 0.0


def _earlier_candidate(decision: dict[str, Any], index: int) -> int | None:
    """The earlier candidate a "repeat" names as [c<n>], if it names one."""

    known_id = str(decision.get("known_id") or "").strip().strip("[]")
    if known_id.startswith("c") and known_id[1:].isdigit() and int(known_id[1:]) < index:
        return int(known_id[1:])
    return None


def _in_parts(candidate: dict[str, Any], parts: list[Dossier]) -> bool:
    """Whether the stretch shows the evidence of ``candidate``: a memory whose evidence is not known counts."""

    evidence = _line(candidate.get("evidence"))
    if not evidence:
        return True
    return any(_shows(evidence, part) for part in parts)


def _shows(evidence: str, part: Dossier) -> bool:
    """Whether ``part`` holds ``evidence``, by the same quote tests ``learner.validate`` uses (D-M1)."""

    body = [part.text[part.body_start:]]
    return quoted_in(evidence, body) or nearly_quoted_in(evidence, body) or short_quote_in(evidence, body)


def _about(candidate: dict[str, Any]) -> str:
    return " ".join([str(candidate.get("text") or ""), *(str(item) for item in candidate.get("subjects") or [])])


def _where(row: RecordRow) -> str:
    return "global" if row.scope == "global" else f"project {row.project_name or row.project_id}"


def _line(text: object) -> str:
    return " ".join(str(text or "").split())



# A provisional memory whose chat never got its final pass (learning turned off, the log removed) stands as the
# quick pass saved it after this long.
PROVISIONAL_DAYS = 7


def confirm_session(memory: Memory, session_id: str | None) -> int:
    """Let what the quick pass saved from ``session_id`` stand, as it is; returns how many."""

    if not session_id:
        return 0
    records = memory.store.tagged_active(PROVISIONAL_TAG, session=session_id)
    for record in records:
        _confirm(memory, record)
    return len(records)


def confirm_stale(memory: Memory, *, now: datetime, mine_only: bool = False) -> int:
    """Let provisional memories older than ``PROVISIONAL_DAYS`` stand: their chat will not be settled. With
    ``mine_only``, only those this computer learned."""

    from ..store import computer_name

    cutoff = (now - timedelta(days=PROVISIONAL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    here = computer_name()
    stale = [record for record in memory.store.tagged_active(PROVISIONAL_TAG) if record.created_at < cutoff
             and (not mine_only or record.source_computer in (None, "", here))]
    for record in stale:
        _confirm(memory, record)
    return len(stale)
