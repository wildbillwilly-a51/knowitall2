"""What KnowItAll2 did, and what went wrong: the record behind ``activity`` and the app.

Events go into the memory database's journal. Problems go into a small file
beside it, so a failure is recorded even when the database itself cannot be
opened. Writing either never raises: a journal problem must never break the
operation it describes. Every text is redacted and capped, like everything
else KnowItAll2 keeps. Events are kept for ``EVENTS_KEPT_DAYS``, and ``tidy``
drops older ones once a day.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from .paths import data_home, database_path

# What the journal records. Agents' use: briefing, recall. Changes by anyone:
# remember, forget, restore, confirm (the user vouches for a memory), answer
# (the user's choice), question (a question about memories), settle (a
# question KnowItAll2 or an agent settled), task (a request to agents),
# settings, catalog (the background filing of memories under systems), move (a
# memory filed under another project). Learning: learning (one per run), candidate (each proposed
# memory and its outcome), change (one maintenance change), maintenance (a
# manual review run). Sharing: sync (changes sent to and received from a
# KnowItAll2 server).
EVENT_KINDS = (
    "briefing", "recall", "remember", "forget", "restore", "confirm", "question", "answer", "settle", "task",
    "settings", "learning", "candidate", "change", "maintenance", "catalog", "move", "sync",
)
EVENTS_KEPT_DAYS = 90
# Each briefing's and recall's own row (usage) is kept this long; older ones live on as per-memory totals.
USAGE_KEPT_DAYS = 365
TIDIED_KEY = "journal.tidied_at"
# How far this computer has sent its uses to a server (connected.USAGE_CURSOR_KEY), when it is connected.
_USAGE_SENT_KEY = "sync.usage_cursor"
SUMMARY_CHARACTERS = 300
DETAIL_CHARACTERS = 600
DETAIL_ITEMS = 50
PROBLEMS_FILE = "problems.jsonl"
PROBLEMS_MAX_BYTES = 256 * 1024
PROBLEM_REPEAT_SECONDS = 10 * 60

# The same problem from one process is written once every few minutes, so a
# broken server that fails every call cannot flood the file.
_last_problem: dict[str, float] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(
    store: Any,
    kind: str,
    summary: str,
    *,
    outcome: str | None = None,
    agent: str | None = None,
    project_id: str | None = None,
    session: str | None = None,
    run: str | None = None,
    record_ids: Sequence[str] = (),
    details: dict[str, Any] | None = None,
    at: str | None = None,
) -> None:
    """Add one event to the journal in ``store``; never raises."""

    try:
        store.add_event(
            at=at or utc_now(), kind=kind, summary=clean_text(summary, SUMMARY_CHARACTERS), outcome=outcome,
            agent=agent, project_id=project_id, session=session, run_id=run,
            record_ids=[str(item) for item in record_ids if item],
            details=clean_details(details or {}),
        )
    except Exception:
        pass


def record_standalone(kind: str, summary: str, **fields: Any) -> None:
    """Record an event from code that holds no open store, such as a settings change."""

    try:
        from .store import Store

        store = Store.open(database_path())
    except Exception as exc:
        problem("journal", f"could not record a {kind} event: {exc}")
        return
    try:
        record(store, kind, summary, **fields)
    finally:
        store.close()


def prune(store: Any, *, now: datetime | None = None) -> None:
    """Drop events older than the retention period; never raises."""

    moment = now or datetime.now(timezone.utc)
    try:
        store.prune_events(before=_stamp(moment - timedelta(days=EVENTS_KEPT_DAYS)))
    except Exception:
        pass


def tidy(store: Any, *, now: datetime | None = None) -> None:
    """Daily housekeeping of a store: drop old events, and fold old uses into each memory's totals; never raises.

    Every briefing and recall adds an event and a use, whether learning is on
    or not, so this runs where those happen (the session-start hook, the
    tools, the server's daily housekeeping), at most once a day; until then it
    costs one lookup. Uses not yet sent to a server are kept until they are.
    """

    moment = now or datetime.now(timezone.utc)
    try:
        if not _tidy_due(store.get_meta(TIDIED_KEY), moment):
            return
        with store.transaction():
            if not _tidy_due(store.get_meta(TIDIED_KEY), moment):
                return  # another process just did it
            store.prune_events(before=_stamp(moment - timedelta(days=EVENTS_KEPT_DAYS)))
            sent = store.get_meta(_USAGE_SENT_KEY)
            store.roll_up_usage(before=_stamp(moment - timedelta(days=USAGE_KEPT_DAYS)),
                                sent_up_to=int(sent) if sent is not None else None)
            store.set_meta(TIDIED_KEY, _stamp(moment))
    except Exception as exc:
        problem("journal", f"old events and uses could not be tidied up: {type(exc).__name__}: {exc}")


def _tidy_due(last: str | None, moment: datetime) -> bool:
    # A time ahead of now (a clock put back) does not put it off.
    return not last or not _stamp(moment - timedelta(days=1)) < last <= _stamp(moment)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def problems_path() -> Path:
    return data_home() / "logs" / PROBLEMS_FILE


def problem(source: str, message: str, *, details: dict[str, Any] | None = None) -> None:
    """Append one problem to the problems file; never raises.

    ``source`` names the part that failed, for example "MCP server (codex)".
    """

    try:
        key = f"{source}\n{message}"
        moment = time.monotonic()
        if moment - _last_problem.get(key, -PROBLEM_REPEAT_SECONDS - 1) < PROBLEM_REPEAT_SECONDS:
            return
        _last_problem[key] = moment
        path = problems_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > PROBLEMS_MAX_BYTES:
            path.replace(path.with_name(path.stem + ".1" + path.suffix))
        entry = {
            "at": utc_now(), "source": clean_text(source, 80), "message": clean_text(message, DETAIL_CHARACTERS),
            "details": clean_details(details or {}),
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_problems(*, since: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Recorded problems, newest first, from the current and the previous problems file."""

    path = problems_path()
    entries: list[tuple[str, int, dict[str, Any]]] = []
    for candidate in (path.with_name(path.stem + ".1" + path.suffix), path):
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("at"), str) and (since is None or entry["at"] >= since):
                # Within one second, a later line is the newer problem.
                entries.append((entry["at"], len(entries), entry))
    entries.sort(key=lambda item: item[:2], reverse=True)
    return [entry for _, _, entry in entries[:limit]]


def clean_text(value: object, limit: int) -> str:
    # Loaded when something is written: its patterns take a while, and most hooks write nothing.
    from .secrets import redact

    text = redact(" ".join(str(value or "").split()))
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def clean_details(value: Any, *, depth: int = 0) -> Any:
    """Details made safe to keep: strings redacted and capped, containers bounded."""

    if isinstance(value, str):
        return clean_text(value, DETAIL_CHARACTERS)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= 3:
        return clean_text(value, DETAIL_CHARACTERS)
    if isinstance(value, dict):
        return {str(key)[:80]: clean_details(item, depth=depth + 1) for key, item in list(value.items())[:DETAIL_ITEMS]}
    if isinstance(value, (list, tuple, set)):
        return [clean_details(item, depth=depth + 1) for item in list(value)[:DETAIL_ITEMS]]
    return clean_text(value, DETAIL_CHARACTERS)
