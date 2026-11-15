"""The call ledger: one record of every production model call, whatever asked for it.

Joint review 2026-11-13 (docs/research/2026-11-13-learning-joint-review): learner calls could be told apart
only by time joins, and several consumers counted calls no model made. Every production call passes
``availability.guarded``, which writes the call's events here; callers add what the call was for (a
``context``) and what came of its answer (``result``). Budgets stay in ``LearnerState.calls``; a budget entry
names its call (``attempt``), so what was charged and what ran can be reconciled without guessing.

Events, one JSON object a line in ``learner/backend-attempts.jsonl`` (the file that held one line per finished
attempt before; those lines are read as ``legacy``):

- ``start``: a call is about to be made (intent, not proof it ran), with its context.
- ``end``: how it ended: ``outcome`` (ok, failed, blocked, error), ``category``, ``launch`` (confirmed: the
  engine ran; none: it never started; unknown: it may have), and the usage the engine reported. A ``start``
  with no ``end`` means the process was stopped mid-call: whether the model ran is unknown.
- ``deferred``: refused before any launch (a backend pause, a provenance hold), with its reason.
- ``result``: what the caller did with an answer (memories published, records filed, and so on). A result for a
  whole unit of work made of several calls (a final review and its checks) has ``scope`` work and lists them.
- ``charge``: a budget charge (``LearnerState.record_call``), written when it is made: the budget file is saved
  later, so a stop in between leaves the charge here even if the budget file lost it.

Writing never stands in the way of the call: a ledger that cannot be written is reported in the journal as
incomplete, and the call goes on. No transcript text or model output is kept here.
"""

from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ..paths import data_home

VERSION = 2
# Two adjacent reset-to-reset weeks, with a day to spare, so one week can be compared with the next.
KEPT_DAYS = 15
LOCK_SECONDS = 1.0   # bookkeeping waits this long at most for another writer, then gives up (fail-open)
# What a context may carry: everything else is dropped, so no payload text can slip in.
FIELDS = ("purpose", "origin", "mode", "trigger", "run", "work", "session", "source", "preferred_engine",
          "route_reason")

_context: ContextVar[dict[str, Any]] = ContextVar("knowitall2_call_context", default={})


def path() -> Path:
    return data_home() / "learner" / "backend-attempts.jsonl"


@contextmanager
def context(**fields: Any) -> Iterator[None]:
    """What the calls made inside are for; inner contexts add to (and override) outer ones."""

    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown call-ledger fields: {sorted(unknown)}")
    merged = {**_context.get(), **{key: value for key, value in fields.items() if value is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def current() -> dict[str, Any]:
    return dict(_context.get())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return "a-" + secrets.token_hex(6)


def start(extractor: Any) -> str:
    """A call is about to be made; returns its id."""

    attempt = _id()
    engine = getattr(extractor, "name", None)
    # Until learning is routed by the engine that did the work (plan step 2), the preferred engine is the one used.
    _append({"event": "start", "id": attempt, "at": _now(), "backend": engine,
             "model_configured": getattr(extractor, "model", None), "preferred_engine": engine,
             "actual_engine": engine, **current()})
    return attempt


def end(attempt: str, extractor: Any, *, outcome: str, category: str, launch: str) -> None:
    _append({"event": "end", "id": attempt, "at": _now(), "outcome": outcome, "category": category,
             "launch": launch, "model_resolved": getattr(extractor, "resolved_model", None),
             "usage": getattr(extractor, "last_usage", None)})


def deferred(*, reason: str, category: str, backend: str | None = None) -> str:
    """A call refused before anything was launched; returns its id."""

    attempt = _id()
    _append({"event": "deferred", "id": attempt, "at": _now(), "backend": backend, "category": category,
             "reason": reason[:300], **current()})
    return attempt


def result(attempt: str | None, **counts: Any) -> None:
    """What came of a call's answer: counts and short labels only (never text from the call)."""

    if not attempt or not isinstance(attempt, str):
        return
    _append({"event": "result", "id": attempt, "at": _now(), **current(), **_counts(counts)})


def work_result(attempts: list[Any], **counts: Any) -> None:
    """What came of a unit of work made of several calls (``attempts``, in order): its totals belong to all of them."""

    ids = [item for item in attempts if isinstance(item, str) and item]
    if not ids:
        return
    _append({"event": "result", "scope": "work", "id": ids[-1], "attempts": ids, "at": _now(), **current(),
             **_counts(counts)})


def charge(attempt: Any, *, origin: str | None, outcome: str, session: str) -> None:
    """A budget charge for the call ``attempt``, as it is made (see the module's description)."""

    if not isinstance(attempt, str) or not attempt:
        return
    _append({"event": "charge", "id": attempt, "at": _now(), "origin": origin, "outcome": outcome,
             "session": str(session)[:120]})


def _counts(counts: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in counts.items()
            if isinstance(value, (int, float, bool)) or value is None
            or (isinstance(value, dict) and all(isinstance(v, (int, float)) for v in value.values()))
            or (isinstance(value, str) and len(value) <= 80)}


def _time(item: Any) -> datetime | None:
    if not isinstance(item, dict):
        return None
    value = item.get("at") or item.get("completed_at") or item.get("started_at")
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read(since: datetime | None = None, *, folder: Path | None = None) -> list[dict[str, Any]]:
    """Every readable event (``since`` a moment), oldest first; damaged lines are skipped."""

    file = (folder / "backend-attempts.jsonl") if folder is not None else path()
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if "event" not in item:
            # Written before 0.12.12: one line per finished attempt, with no launch or context recorded.
            item = {**item, "event": "legacy"}
        moment = _time(item)
        if since is not None and (moment is None or moment < since):
            continue
        events.append(item)
    return events


def _append(event: dict[str, Any]) -> None:
    from .. import journal
    from .state import LockBusy, waiting_lock

    file = path()
    try:
        # An odd value (an engine stand-in's name, say) is written as text: a record never stops a call.
        line = json.dumps({"v": VERSION, **event}, ensure_ascii=False, default=str)
        file.parent.mkdir(parents=True, exist_ok=True)
        with waiting_lock(file.with_suffix(".lock"), timeout=LOCK_SECONDS):
            _prune(file)
            with file.open("ab") as stream:
                if stream.tell():
                    # A line cut short by a crash would swallow this one: start a fresh line after it.
                    with file.open("rb") as check:
                        check.seek(-1, 2)
                        if check.read(1) != b"\n":
                            stream.write(b"\n")
                stream.write(line.encode("utf-8") + b"\n")
    except Exception as exc:   # whatever the file holds, the call goes on: a record never stops one
        try:
            journal.problem("call ledger", f"a model call could not be recorded; the ledger is incomplete: "
                                           f"{type(exc).__name__}")
        except Exception:
            pass


def _prune(file: Path) -> None:
    """Drop events older than ``KEPT_DAYS``, once the oldest is a day past it; a call's events go together."""

    try:
        with file.open("rb") as stream:
            first = stream.readline()
    except FileNotFoundError:
        return
    try:
        oldest = _time(json.loads(first))   # None for a damaged or unexpected first line: then it is pruned
    except ValueError:
        oldest = None
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEPT_DAYS)
    if oldest is not None and oldest >= cutoff - timedelta(days=1):
        return
    from ..files import write_text_atomic

    lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    parsed = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue   # damaged lines go at pruning
        if isinstance(item, dict):
            parsed.append((line, item))
    recent = {item.get("id") for _, item in parsed if (_time(item) or cutoff) >= cutoff and item.get("id")}
    kept = [line for line, item in parsed if (_time(item) or cutoff) >= cutoff or item.get("id") in recent]
    write_text_atomic(file, "".join(line + "\n" for line in kept))


def summary(since: datetime, *, folder: Path | None = None) -> dict[str, Any]:
    """Counts per purpose for calls that ended (or, with no end, started) since ``since``.

    A call's events are joined before the window applies, so one started before ``since`` and ended after counts.
    Kinds overlap on purpose (a call launched and failed counts in both): ``launched`` (the engine ran),
    ``uncertain`` (no end seen, or a launch that may have happened), ``ok``, ``failed``, ``blocked``,
    ``deferred`` (refused before launch), and for budgets ``charged``, ``exempt`` (the charge rule exempts it:
    blocked, or never launched) and ``charge missing`` (it should have been charged and no charge was recorded,
    as after a stop before the budget was saved). Lines written before the ledger count as ``legacy`` with
    their outcome; whether they launched is not known. A call whose ``start`` was not recorded still counts from
    what was (its end, its charge), as ``incomplete`` and unlabelled.
    """

    events = read(since - timedelta(days=1), folder=folder)
    starts = {item["id"]: item for item in events if item.get("event") == "start" and item.get("id")}
    ended = {item["id"]: item for item in events if item.get("event") == "end" and item.get("id")}
    charges = {item["id"]: item for item in events if item.get("event") == "charge" and item.get("id")}
    charged = set(charges)
    by: dict[str, dict[str, int]] = {}

    def add(purpose: str | None, key: str) -> None:
        row = by.setdefault(purpose or "unlabelled", {})
        row[key] = row.get(key, 0) + 1

    def within(item: dict[str, Any]) -> bool:
        moment = _time(item)
        return moment is not None and moment >= since

    # Every call any event survives for: one event's record may have failed (fail-open) while another's was kept.
    for attempt in sorted(set(starts) | set(ended) | charged):
        item, finish = starts.get(attempt), ended.get(attempt)
        if not within(finish or item or charges[attempt]):
            continue
        purpose = (item or {}).get("purpose")
        if item is None:
            add(purpose, "incomplete")   # what the call was for was not recorded
        if finish is None:
            add(purpose, "uncertain")
            add(purpose, "charged" if attempt in charged else "charge unknown")
            continue
        launch, outcome = finish.get("launch"), finish.get("outcome")
        if launch == "confirmed":
            add(purpose, "launched")
        elif launch == "unknown":
            add(purpose, "uncertain")
        add(purpose, {"ok": "ok", "blocked": "blocked"}.get(outcome, "failed"))
        # The charge rule (``state.chargeable``), read back from how the call ended.
        due = outcome == "ok" or (outcome == "failed" and launch != "none")
        add(purpose, "charged" if attempt in charged else "charge missing" if due else "exempt")
    for item in events:
        if not within(item):
            continue
        if item.get("event") == "deferred":
            add(item.get("purpose"), "deferred")
        elif item.get("event") == "legacy":
            add(None, "legacy")
            add(None, {"ok": "ok", "blocked": "blocked"}.get(item.get("outcome"), "failed"))
    return by
