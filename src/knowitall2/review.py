"""Questions about memories, settled by whoever can: KnowItAll2, then an agent, then the user.

KnowItAll2 queues a question when it must not decide on its own: when newer,
weaker evidence contradicts a memory, when something the user stated looks
like a temporary status, or when the learner thinks it heard a rule but could
not find the user's own words for it.

Each question moves through stages:

1. ``review``: KnowItAll2's background run looks at it and settles it when the
   memories make the answer clear.
2. ``checking``: otherwise the next agent working on that project, or looking
   up one of those memories, is asked to check. The check is optional, quick,
   and look-only; an agent reports what it saw with the ``settle`` tool.
3. ``ask_user``: only if it is still unclear does the user get it, in plain
   English, with the details on request and a "Not sure" choice.

Rules and statements the user made go straight to the user: nothing but the
user's own word may change them. An answer from the user carries full
authority. Agents are also asked to find missing information about a system
(``find_out`` tasks), reported the same way.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from . import journal
from .memory import QUESTION_CHECK_DAYS, QUESTION_REVIEW_DAYS, QUESTIONS_SHOWN_KEY, Memory, MemoryInputError
from .store import RecordRow, Store

UNCONFIRMED_RULE_PREFIX = "Possible rule, not confirmed by the user: "
LIST_LIMIT = 5
STAGES = ("review", "checking", "ask_user")
CHECK_OFFERS = 6
CHECK_DAYS = QUESTION_CHECK_DAYS
FIND_OUT_OFFERS = 12
FIND_OUT_DAYS = 30
# A question KnowItAll2 has not reviewed within this time (learning off, no engine) goes to the user.
REVIEW_DAYS = QUESTION_REVIEW_DAYS

OPTIONS = {
    "conflict": [
        {"key": "use_new", "label": "The newer one is right; it replaces the older one"},
        {"key": "keep_mine", "label": "The older one is still right; drop the newer one"},
        {"key": "keep_both", "label": "They don't conflict; keep both as they are"},
    ],
    "confirm_rule": [
        {"key": "make_rule", "label": "Yes, make it one of my rules"},
        {"key": "keep_note", "label": "No; keep it only as a note"},
        {"key": "forget", "label": "No; forget it"},
    ],
    "still_true": [
        {"key": "keep", "label": "Still true; keep it"},
        {"key": "forget", "label": "No longer true; forget it"},
    ],
}
PLAIN_LABELS = {
    "conflict": {"use_new": "The newer one is right", "keep_mine": "The older one is right",
                 "keep_both": "Both are right"},
    "confirm_rule": {"make_rule": "Yes, agents should always do this", "keep_note": "No, it only applied to that work",
                     "forget": "No, forget it"},
    "still_true": {"keep": "Yes, it's still true", "forget": "No, not anymore"},
}
# What "Not sure" means for each kind of question: the choice that changes nothing.
NOT_SURE = {"conflict": "keep_both", "confirm_rule": "keep_note", "still_true": "keep"}
_VERIFICATION_WORDS = {"user_stated": "stated by the user", "observed": "seen in tool output", "unverified": "a guess"}


def ask_conflict(memory: Memory, older: RecordRow, newer: RecordRow) -> bool:
    """Newer, weaker evidence disagrees with a memory; find out which is right."""

    said = "You said" if older.verification == "user_stated" else f"KnowItAll2 has ({older.verification})"
    prompt = (
        f'{said}: "{_short(older.text)}" [{older.id}]. A later session suggests: '
        f'"{_short(newer.text)}" [{newer.id}]. Which is right now?'
    )
    return _ask(memory, "conflict", prompt, [older.id, newer.id])


def ask_rule(memory: Memory, note: RecordRow) -> bool:
    rule = note.text.removeprefix(UNCONFIRMED_RULE_PREFIX)
    prompt = f'This sounded like one of your rules: "{_short(rule)}" [{note.id}]. Should it be a rule?'
    return _ask(memory, "confirm_rule", prompt, [note.id])


def ask_still_true(memory: Memory, stated: RecordRow) -> bool:
    """Something the user said looks like a temporary status; only the user may retire it."""

    prompt = f'You said: "{_short(stated.text)}" [{stated.id}]. It looks like a temporary status. Is it still true?'
    return _ask(memory, "still_true", prompt, [stated.id])


def ask_to_forget(memory: Memory, stated: RecordRow, *, reason: str | None) -> str:
    """An agent asked to forget something the user said; only the user may. Returns the question's id.

    When a "still true?" question about it is already open, the user answers that one.
    """

    who = f"An agent ({memory.agent})" if memory.agent else "An agent"
    asked = f'{who} asked to forget it: "{_short(reason, 200)}".' if reason else f"{who} asked to forget it."
    prompt = f'You said: "{_short(stated.text)}" [{stated.id}]. {asked} Is it still true?'
    with memory.store.transaction():
        _ask(memory, "still_true", prompt, [stated.id], reason=asked)
        question = open_question(memory.store, "still_true", [stated.id])
    assert question is not None
    return question["id"]


def open_question(store: Store, kind: str, record_ids: Sequence[str]) -> dict[str, Any] | None:
    """The open question of this kind about exactly these memories, if there is one."""

    wanted = sorted(record_ids)
    return next((question for question in store.open_questions(limit=500)
                 if question["kind"] == kind and sorted(question["record_ids"]) == wanted), None)


def stage(store: Store, question_id: str) -> str:
    row = store.question_stage(question_id)
    return row["stage"] if row else "review"


def for_user(store: Store, *, limit: int = 100, now: datetime | None = None) -> list[dict[str, Any]]:
    """The open questions only the user can settle now, with their plain wording."""

    moment = now or datetime.now(timezone.utc)
    waiting = []
    for question in store.open_questions(limit=500):
        row = store.question_stage(question["id"])
        current = row["stage"] if row else "review"
        created = _parse(question["created_at"])
        # A question no review settled in a few days, or no agent checked within a week after that, is the
        # user's, whether or not a learning run comes to hand it over.
        overdue = (current == "review" and created < moment - timedelta(days=REVIEW_DAYS)) or (
            current == "checking" and created < moment - timedelta(days=REVIEW_DAYS + CHECK_DAYS))
        if current == "ask_user" or overdue:
            waiting.append(with_plain(question, row, store))
    return waiting[:limit]


def in_progress(store: Store, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Open questions KnowItAll2 or an agent is still looking into."""

    user = {item["id"] for item in for_user(store, now=now)}
    found = []
    for question in store.open_questions(limit=500):
        if question["id"] in user:
            continue
        row = store.question_stage(question["id"])
        item = with_plain(question, row, store)
        item["stage"] = row["stage"] if row else "review"
        item["tasks"] = store.tasks(question_id=question["id"])
        found.append(item)
    return found


def with_plain(question: dict[str, Any], row: dict[str, Any] | None, store: Store | None = None) -> dict[str, Any]:
    labels = {**PLAIN_LABELS.get(question["kind"], {}), **((row or {}).get("labels") or {})}
    return {
        **question,
        "plain": (row or {}).get("plain") or (plain_fallback(store, question) if store else question["prompt"]),
        "labels": [{"key": option["key"], "label": labels.get(option["key"], option["label"])}
                   for option in question["options"]],
        "not_sure": NOT_SURE.get(question["kind"]),
        "reason": (row or {}).get("reason"),
        "findings": (row or {}).get("findings") or [],
        "context": question_context(store, question["record_ids"]) if store else "",
    }


_AGENT_WORDS = {"codex": "a Codex chat", "claude-code": "a Claude Code chat"}


def question_context(store: Store, record_ids: Sequence[str]) -> str:
    """Where a question's memory came from, in words: which agent's chat, which project, and when.

    Without it a question can be impossible to answer: the user has many chats in many projects.
    """

    for record_id in record_ids:
        record = store.get(record_id)
        if record is None:
            continue
        agent, project = store.candidate_origin(record_id)
        project = record.project_name or project
        who = _AGENT_WORDS.get(agent or "", "a chat" if record.source_session else "")
        if not who and not project:
            continue
        where = f" in project {project}" if project else ""
        return f"From {who or 'KnowItAll2'}{where}, {record.created_at[:10]}."
    return ""


def plain_fallback(store: Store, question: dict[str, Any]) -> str:
    """A plain line for a question the background review has not reworded yet, from the memories' own summaries."""

    notes = store.notes_for(question["record_ids"])
    records = [store.get(record_id) for record_id in question["record_ids"]]
    systems = [note["system_name"] for note in notes.values() if note.get("system_name")]
    about = f" about {systems[0]}" if systems else ""

    def said(index: int) -> str:
        record = records[index] if index < len(records) else None
        headline = (notes.get(record.id) or {}).get("headline") if record else None
        text = headline or (record.text.removeprefix(UNCONFIRMED_RULE_PREFIX) if record else "")
        return _short(text, 140)

    if question["kind"] == "conflict" and len(records) == 2:
        if records[0] is not None and records[0].verification == "user_stated":
            return f'You said "{said(0)}", but a later note says "{said(1)}". Which is right?'
        return f'Two notes{about} disagree: "{said(0)}" and, later, "{said(1)}". Which is right?'
    if question["kind"] == "confirm_rule":
        return f'While working, an agent treated this as one of your rules: "{said(0)}". Is it?'
    if question["kind"] == "still_true":
        return f'You said: "{said(0)}". Is that still true?'
    return question["prompt"]


def list_questions(memory: Memory, *, limit: int = LIST_LIMIT, how_to_answer: str | None = None) -> str:
    """The questions waiting for the user, in plain words, for an agent to show the user (or for the user)."""

    questions = for_user(memory.store, now=_parse(memory.now()))
    memory.store.set_meta(QUESTIONS_SHOWN_KEY, memory.now())
    if not questions:
        return "KnowItAll2 has no questions for the user."
    guidance = how_to_answer or "Ask the user, then record each choice with the answer tool."
    lines = [f"KnowItAll2 has {len(questions)} question(s) for the user. {guidance}"]
    for question in questions[:limit]:
        lines.append(f"[{question['id']}] {question['plain']}")
        if question.get("context"):
            lines.append(f"   ({question['context']} Tell the user this, so they know what it is about.)")
        lines.extend(f"   - {option['key']}: {option['label']}" for option in question["labels"])
        if question["not_sure"]:
            lines.append(f"   (If the user is not sure: {question['not_sure']}.)")
    if len(questions) > limit:
        lines.append(f"(+{len(questions) - limit} more after these are answered.)")
    return "\n".join(lines)


def answer(memory: Memory, question_id: str, choice: str) -> str:
    """Apply the user's choice. The choice is the user's own word, so it carries full authority.

    The question and its memories are read again once no one else can write,
    so two answers given at once never both apply.
    """

    question = _still_open(memory, question_id)
    keys = [option["key"] for option in question["options"]]
    if choice not in keys:
        raise MemoryInputError(f"Unknown choice {choice!r}; use one of: {', '.join(keys)}.")
    with memory.store.transaction():
        question = _still_open(memory, question_id)
        records = [memory.store.get(record_id) for record_id in question["record_ids"]]
        if any(record is None or record.status != "active" for record in records):
            _close(memory, question["id"], f"{choice} (memories had already changed)")
            _close_tasks(memory, question["id"], "done")
            return (f"[{question['id']}] no longer applies: its memories changed since it was asked. "
                    "Nothing else was done.")
        outcome = _apply(memory, question, records, choice)
        _close_tasks(memory, question["id"], "done")
        memory.record_event(
            "answer", outcome, outcome=choice, record_ids=question["record_ids"],
            details={"question": question["id"], "kind": question["kind"], "prompt": question["prompt"]},
        )
    return f"Answered [{question['id']}]: {outcome}"


def settle(memory: Memory, question: dict[str, Any], choice: str, *, by: str, evidence: str) -> str | None:
    """Apply a decision KnowItAll2 or an agent is sure of; None when only the user may make it.

    Only conflicts are settled this way, and never by replacing or retiring
    something the user said. The question and its memories are read again once
    no one else can write, since what the caller saw may have changed: the user
    may have answered, or confirmed one of the memories, in the meantime.
    Everything settled can be undone with restore.
    """

    if question["kind"] != "conflict" or choice not in ("use_new", "keep_mine", "keep_both"):
        return None
    now = memory.now()
    note = " ".join(f"settled by {by}: {evidence}".split())[:200]
    checked = by != "KnowItAll2" and bool(evidence.strip())
    with memory.store.transaction():
        current = memory.store.question(question["id"])
        if current is None or current["status"] != "open":
            return f"[{question['id']}] was already answered; nothing was changed."
        records = [memory.store.get(record_id) for record_id in current["record_ids"]]
        if any(record is None or record.status != "active" for record in records):
            _close(memory, current["id"], "memories had already changed")
            _close_tasks(memory, current["id"], "done")
            return f"[{current['id']}] no longer applies: its memories had already changed; nothing was changed."
        older, newer = records
        if choice == "use_new" and older.verification == "user_stated":
            return None
        if choice == "keep_mine" and newer.verification == "user_stated":
            return None
        if choice == "use_new":
            if checked and newer.verification == "unverified":
                memory.store.set_verification(newer.id, "observed", now=now)
            memory.store.supersede(older.id, newer.id, now=now, reason=note)
            outcome = f"[{newer.id}] is right and replaces [{older.id}]."
        elif choice == "keep_mine":
            memory.store.retire(newer.id, reason=note, now=now)
            verification = "observed" if checked and older.verification == "unverified" else older.verification
            memory.store.confirm(older.id, verification=verification, now=now)
            outcome = f"[{older.id}] is still right; retired [{newer.id}]."
        else:
            memory.store.confirm(older.id, verification=older.verification, now=now)
            memory.store.confirm(newer.id, verification=newer.verification, now=now)
            outcome = f"Kept both [{older.id}] and [{newer.id}]; they do not conflict."
        _close(memory, current["id"], f"{choice} (settled by {by})")
        _close_tasks(memory, current["id"], "done")
        journal.record(
            memory.store, "settle", f"{outcome} {evidence}".strip(), outcome=choice, agent=by,
            record_ids=current["record_ids"], details={"question": current["id"], "evidence": evidence}, at=now,
        )
    return outcome


def _still_open(memory: Memory, question_id: str) -> dict[str, Any]:
    question = memory.store.question((question_id or "").strip().strip("[]"))
    if question is None:
        raise MemoryInputError(f"There is no question [{question_id}].")
    if question["status"] != "open":
        raise MemoryInputError(f"[{question['id']}] was already answered ({question['answer']}).")
    return question


def _close(memory: Memory, question_id: str, answer: str) -> None:
    """Close an open question; a question someone else answered meanwhile stops the whole change."""

    if not memory.store.close_question(question_id, answer=answer, now=memory.now()):
        raise MemoryInputError(f"[{question_id}] was already answered.")


def start_check(memory: Memory, question: dict[str, Any], records: list[RecordRow], *, plain: str | None,
                labels: dict[str, str] | None, reason: str | None) -> str:
    """Ask the next relevant agent to check a conflict; returns the task id."""

    older, newer = records
    task_id = "t-" + uuid.uuid4().hex[:8]
    prompt = (
        f'Two memories disagree. [{older.id}] says: "{_short(older.text, 200)}" [{newer.id}] says: '
        f'"{_short(newer.text, 200)}" If this session\'s work, or one quick look-only check, shows which is right, '
        f"call settle with task {task_id}, choice use_new (the second is right), keep_mine (the first is right), or "
        "keep_both (both hold), and what you saw. Otherwise ignore this; never change anything to find out."
    )
    project = older.project_id or newer.project_id or _learned_in(memory.store, [older.id, newer.id])
    now = memory.now()
    with memory.store.transaction():
        memory.store.add_task(task_id=task_id, kind="check", question_id=question["id"], project_id=project,
                              prompt=prompt, now=now)
        memory.store.set_question_stage(question["id"], stage="checking", plain=plain, labels=labels, reason=reason,
                                        now=now)
        journal.record(memory.store, "task", "Asked the next agent to check which memory is right", outcome="check",
                       agent="KnowItAll2", project_id=project, record_ids=question["record_ids"],
                       details={"question": question["id"], "task": task_id}, at=now)
    return task_id


def ask_to_find_out(memory: Memory, system: dict[str, Any], facet: str, label: str, *, detail: str = "",
                    project_id: str | None = None) -> str:
    """Ask agents to find a missing part of a system's profile; returns the task id."""

    task_id = "t-" + uuid.uuid4().hex[:8]
    wanted = f"{label.lower()} for {system['name']}" + (f" ({detail})" if detail else "")
    prompt = (
        f"KnowItAll2 does not know {wanted}. If this session shows it, call settle with task {task_id} and what you "
        "found. Never report a password or key: say where it is kept instead."
    )
    now = memory.now()
    with memory.store.transaction():
        memory.store.add_task(task_id=task_id, kind="find_out", system_id=system["id"], project_id=project_id,
                              facet=facet, prompt=prompt, now=now)
        journal.record(memory.store, "task", f"Asked agents to find out {wanted}", outcome="find out",
                       agent=memory.agent, project_id=project_id, details={"task": task_id, "system": system["id"]},
                       at=now)
    return task_id


def escalate(memory: Memory, question_id: str, *, finding: str | None, reason: str | None = None) -> None:
    """Hand a question to the user, with what was found so far."""

    now = memory.now()
    with memory.store.transaction():
        memory.store.set_question_stage(question_id, stage="ask_user", finding=finding, reason=reason, now=now)
        _close_tasks(memory, question_id, "escalated")


def settle_task(memory: Memory, task_id: str, *, choice: str, found: str, certain: bool) -> str:
    """An agent's report on a check or a request (the ``settle`` tool)."""

    task = memory.store.task((task_id or "").strip().strip("[]"))
    if task is None or task["status"] != "open":
        raise MemoryInputError(f"There is no open KnowItAll2 task [{task_id}].")
    who = memory.agent or "an agent"
    found = " ".join(str(found or "").split())[:600]
    if task["kind"] == "find_out":
        return _found_out(memory, task, found, certain)
    question = memory.store.question(task["question_id"] or "")
    if question is None or question["status"] != "open":
        memory.store.update_task(task["id"], status="done", now=memory.now())
        return "That question was already settled. Thanks."
    records = [memory.store.get(record_id) for record_id in question["record_ids"]]
    if any(record is None or record.status != "active" for record in records):
        memory.store.close_question(question["id"], answer="memories had already changed", now=memory.now())
        memory.store.update_task(task["id"], status="done", now=memory.now())
        return "Those memories already changed, so nothing was needed. Thanks."
    if certain and found:
        outcome = settle(memory, question, choice, by=who, evidence=found)
        if outcome:
            return f"Settled: {outcome}"
        escalate(memory, question["id"], finding=f"{who} checked: {found}",
                 reason="Only you can change something you said yourself.")
        return "Thanks. This one involves the user's own words, so the user decides, with your finding."
    escalate(memory, question["id"], finding=f"{who} could not tell: {found}" if found else f"{who} could not tell.")
    return "Thanks. The user will be asked, with your note."


def offer_tasks(store: Store, *, project_id: str | None, record_ids: list[str] | tuple = (), limit: int = 1,
                now: str) -> list[dict[str, Any]]:
    """Open tasks an agent in this project, or looking at these memories, could help with; counts the offer."""

    wanted = set(record_ids)
    systems = {note["system_id"] for note in store.notes_for(list(wanted)).values() if note.get("system_id")}
    chosen = []
    for task in store.tasks(status="open"):
        relevant = project_id is not None and task["project_id"] == project_id
        if task["kind"] == "check" and not relevant and wanted:
            question = store.question(task["question_id"] or "")
            relevant = question is not None and bool(wanted.intersection(question["record_ids"]))
        if task["kind"] == "find_out" and not relevant:
            relevant = task["system_id"] in systems
        if relevant:
            chosen.append(task)
        if len(chosen) >= limit:
            break
    for task in chosen:
        store.update_task(task["id"], offered=True, now=now)
    return chosen


def expire_tasks(memory: Memory, *, now: datetime | None = None) -> dict[str, int]:
    """Checks no agent could do go to the user; requests no agent could fill are given up."""

    moment = now or _parse(memory.now())
    counts = {"escalated": 0, "given up": 0}
    for task in memory.store.tasks(status="open"):
        age = moment - _parse(task["created_at"])
        if task["kind"] == "check" and (task["offered"] >= CHECK_OFFERS or age > timedelta(days=CHECK_DAYS)):
            finding = (f"Agents were asked {task['offered']} times, but none could tell." if task["offered"]
                       else "No agent worked on it within a week.")
            escalate(memory, task["question_id"], finding=finding)
            counts["escalated"] += 1
        elif task["kind"] == "find_out" and (task["offered"] >= FIND_OUT_OFFERS or age > timedelta(days=FIND_OUT_DAYS)):
            memory.store.update_task(task["id"], status="given_up", now=memory.now())
            counts["given up"] += 1
    return counts


def _found_out(memory: Memory, task: dict[str, Any], found: str, certain: bool) -> str:
    from .catalog import FACET_KINDS

    if not found:
        return "Nothing was recorded; the request stays open for another session."
    system = memory.store.system(task["system_id"] or "")
    result = memory.remember(
        found, kind=FACET_KINDS.get(task["facet"] or "", "fact"), subjects=[system["name"]] if system else [],
        scope="global", source="observed" if certain else "inferred", detect_project=False,
    )
    now = memory.now()
    with memory.store.transaction():
        if system is not None:
            # A memory already known keeps its note, such as one the user wrote.
            memory.store.add_note(result.record.id, headline=_short(found, 120), system_id=system["id"],
                                  facet=task["facet"] or "other", written_by=memory.agent or "agent", now=now)
        memory.store.update_task(task["id"], status="done", result=result.record.id, now=now)
    return f"Saved [{result.record.id}]. Thanks."


def _close_tasks(memory: Memory, question_id: str, status: str) -> None:
    for task in memory.store.tasks(status="open", question_id=question_id):
        memory.store.update_task(task["id"], status=status, now=memory.now())


def _learned_in(store: Store, record_ids: list[str]) -> str | None:
    """The project of the session a memory was learned in, from the journal, when known."""

    for record_id in record_ids:
        for event in store.events(kinds=["candidate", "remember"], record_id=record_id, limit=5):
            if event["project_id"]:
                return event["project_id"]
    return None


def _apply(memory: Memory, question: dict, records: list[RecordRow], choice: str) -> str:
    now = memory.now()
    if question["kind"] == "conflict":
        older, newer = records
        if choice == "use_new":
            memory.store.set_verification(newer.id, "user_stated", now=now)
            memory.store.supersede(older.id, newer.id, now=now)
            outcome = f"[{newer.id}] is now your statement and replaces [{older.id}]."
        elif choice == "keep_mine":
            memory.store.retire(newer.id, reason="the user kept the older memory", now=now)
            memory.store.confirm(older.id, verification="user_stated", now=now)
            outcome = f"Kept [{older.id}]; retired [{newer.id}]."
        else:
            # "They don't conflict" is not the user vouching for every detail, so
            # each memory keeps its own verification and is only refreshed.
            memory.store.confirm(older.id, verification=older.verification, now=now)
            memory.store.confirm(newer.id, verification=newer.verification, now=now)
            outcome = f"Kept both [{older.id}] and [{newer.id}] as they are."
    elif question["kind"] == "still_true":
        [stated] = records
        if choice == "keep":
            memory.store.confirm(stated.id, verification="user_stated", now=now)
            outcome = f"Kept [{stated.id}]."
        else:
            memory.store.retire(stated.id, reason="the user said it is no longer true", now=now)
            outcome = f"Forgot [{stated.id}]."
    else:
        [note] = records
        if choice == "make_rule":
            rule = memory.promote_rule(note, note.text.removeprefix(UNCONFIRMED_RULE_PREFIX))
            outcome = f"Saved your rule [{rule.id}]; it replaces [{note.id}]."
        elif choice == "keep_note":
            outcome = f"Kept [{note.id}] as a note."
        else:
            memory.store.retire(note.id, reason="the user declined the proposed rule", now=now)
            outcome = f"Forgot [{note.id}]."
    _close(memory, question["id"], choice)
    return outcome


def _ask(memory: Memory, kind: str, prompt: str, record_ids: list[str], *, reason: str | None = None) -> bool:
    from .learning.state import load_settings

    question_id = "q-" + uuid.uuid4().hex[:8]
    added = memory.store.add_question(
        question_id=question_id, kind=kind, prompt=prompt, record_ids=record_ids,
        options=OPTIONS[kind], now=memory.now(),
    )
    if added:
        # Without learning there is no background review, so the user is asked directly.
        first = "review" if load_settings().enabled else "ask_user"
        memory.store.set_question_stage(question_id, stage=first, reason=reason, now=memory.now())
        # Asked on the learner's or maintenance's behalf too, so this is always recorded.
        journal.record(
            memory.store, "question", prompt, outcome="asked", agent=memory.agent, record_ids=record_ids,
            details={"question": question_id, "kind": kind}, at=memory.now(),
        )
    return added


def describe_record(record: RecordRow) -> str:
    """How a memory was learned, in words, for the background review."""

    where = f" in project {record.project_name}" if record.project_name else ""
    via = f" via {record.source_agent}" if record.source_agent else ""
    return f"{record.kind}, {_VERIFICATION_WORDS.get(record.verification, record.verification)}, saved {record.created_at[:10]}{via}{where}"


def _short(text: str, limit: int = 220) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
