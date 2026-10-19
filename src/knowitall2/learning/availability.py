"""Persistent backend availability, shared by every native model consumer.

Availability is deliberately separate from session progress and call budgets.
A quota rejection says nothing about the transcript being learned.
"""

from __future__ import annotations

import json
import re
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..files import write_text_atomic
from ..paths import data_home
from ..secrets import redact
from .state import waiting_lock, LockBusy


def _path() -> Path:
    return data_home() / "learner" / "backend-availability.json"


def read() -> dict:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def active(backend: str, *, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    try:
        item = read().get(backend)
        return item if item and datetime.fromisoformat(item["retry_at"]) > now else None
    except (ValueError, KeyError, TypeError):
        # Damaged availability state must not silently unleash automatic calls.
        return {"reason": "backend availability state needs repair; restore learner/backend-availability.json from a valid copy",
                "retry_at": "unknown"}


def reset_time(envelope: dict, message: str, *, now: datetime | None = None) -> tuple[str, str]:
    """Use explicit reset metadata first; unknown resets get a labelled 15 minute probe interval."""
    now = now or datetime.now(timezone.utc)
    containers = [envelope]
    containers += [envelope[key] for key in ("error", "rate_limit", "rate_limit_info")
                   if isinstance(envelope.get(key), dict)]
    for source in containers:
        for key in ("retry_at", "reset_at", "resets_at", "resetsAt"):
            value = source.get(key)
            try:
                at = (datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float))
                      and not isinstance(value, bool) else datetime.fromisoformat(str(value).replace("Z", "+00:00")))
                if at.tzinfo is not None and at > now:
                    return at.astimezone(timezone.utc).isoformat(), f"backend {key}"
            except (ValueError, TypeError, OSError, OverflowError):
                pass
        seconds = source.get("retry_after")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 < seconds <= 8 * 86400:
            return (now + timedelta(seconds=seconds)).isoformat(), "backend retry_after"
    # Claude's human-readable reset: "resets 4pm (America/Chicago)" or "resets Oct 21, 1pm (UTC)".
    found = re.search(r"resets\s+(?:(\w{3})\s+(\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", message, re.I)
    if found:
        month, day, hour, minute, meridiem, zone = found.groups()
        try:
            local = now.astimezone(timezone.utc if zone.upper() == "UTC" else ZoneInfo(zone))
            hour = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
            at = local.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
            if month:
                at = at.replace(month=datetime.strptime(month, "%b").month, day=int(day))
            if at <= local:
                at = at.replace(year=at.year + 1) if month else at + timedelta(days=1)
            if at - local <= timedelta(days=8):
                return at.astimezone(timezone.utc).isoformat(), "backend reset message"
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return (now + timedelta(minutes=15)).isoformat(), "fallback probe interval; reset unknown"


def note(backend: str, error) -> None:
    now = datetime.now(timezone.utc)
    with waiting_lock(_path().with_suffix(".lock")):
        data = read()
        previous = data.get(backend, {})
        attempts = int(previous.get("attempts", 0)) + 1
        retry_at = error.retry_at or (now + timedelta(minutes=min(60, 2 * 2 ** min(attempts - 1, 5)))).isoformat()
        if error.reset_source and error.reset_source.startswith("fallback"):
            retry_at = (now + timedelta(minutes=min(360, 15 * 2 ** min(attempts - 1, 5)))).isoformat()
        data[backend] = {"reason": redact(str(error))[:300], "category": error.category,
                         "retry_at": retry_at,
                         "reset_source": error.reset_source or "transient probe interval",
                         "at": now.isoformat(), "attempts": attempts}
        write_text_atomic(_path(), json.dumps(data, indent=2) + "\n")


def before_call(extractor) -> None:
    from .extractor import ExtractionError
    item = active(extractor.name)
    if item:
        if getattr(extractor, "retry_backend_once", False):
            extractor.retry_backend_once = False
            return
        raise ExtractionError(f"{extractor.name} paused until {item['retry_at']}: {item['reason']}",
                              blocking=True, category="cooldown", attempted=False)


def clear(backend: str, *, started: datetime | None = None) -> None:
    if backend not in read():
        return
    with waiting_lock(_path().with_suffix(".lock")):
        data = read()
        if backend in data and (started is None or datetime.fromisoformat(data[backend]["at"]) <= started):
            del data[backend]
            write_text_atomic(_path(), json.dumps(data, indent=2) + "\n")


def guarded(call):
    """All production model calls pass here, including exploration and upkeep."""
    @wraps(call)
    def invoke(extractor, *args, **kwargs):
        from .extractor import ExtractionError, run_bounded
        # Injected runners are isolated simulations, unless a test explicitly enables the gate.
        enabled = getattr(extractor, "check_availability", extractor._runner is run_bounded)
        if enabled:
            before_call(extractor)
        started = datetime.now(timezone.utc)
        try:
            result = call(extractor, *args, **kwargs)
        except ExtractionError as exc:
            if enabled and exc.blocking and exc.category != "cooldown":
                try:
                    note(extractor.name, exc)
                except (OSError, LockBusy, ValueError, TypeError, KeyError):
                    from .. import journal
                    journal.problem("availability", "could not save the backend pause; work remains pending")
            if enabled:
                record_attempt(extractor, started, category=exc.category, outcome="blocked" if exc.blocking else "failed")
            raise
        if enabled:
            try:
                clear(extractor.name, started=started)
            except (OSError, LockBusy, ValueError, TypeError, KeyError):
                pass  # a valid model result must survive bookkeeping contention
            record_attempt(extractor, started, category="ok", outcome="ok")
        return result
    return invoke


def record_attempt(extractor, started: datetime, *, category: str, outcome: str) -> None:
    """Include rejected attempts without charging them as completed learning calls."""
    path = _path().with_name("backend-attempts.jsonl")
    now = datetime.now(timezone.utc)
    entry = {"backend": extractor.name, "model": extractor.model, "started_at": started.isoformat(),
             "completed_at": now.isoformat(), "outcome": outcome, "category": category,
             "usage": getattr(extractor, "last_usage", None)}
    try:
        with waiting_lock(path.with_suffix(".lock")):
            old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            cutoff = now - timedelta(days=7)
            kept = []
            for line in old:
                item = json.loads(line)
                if datetime.fromisoformat(item["completed_at"]) >= cutoff:
                    kept.append(line)
            write_text_atomic(path, "\n".join([*kept, json.dumps(entry)]) + "\n")
    except (OSError, ValueError, KeyError, TypeError, LockBusy):
        from .. import journal
        journal.problem("availability", "could not record a backend attempt")
