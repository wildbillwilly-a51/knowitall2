"""Local, durable source ownership and explicit queues for unlocated reviews.

The meta table is local bookkeeping, survives journal pruning, and is included
in database backups. Nothing here changes the shared-memory wire format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .log_identity import path_key, same_log

SOURCE = "learning.source."
PENDING = "learning.source-review."
LEGACY_MAX_BYTES = 256 * 1024 * 1024


def descriptor(dossier) -> dict | None:
    if not dossier.source_path or dossier.start_offset is None:
        return None
    start = [dossier.start_offset, dossier.start_event_index]
    end = [dossier.end_offset, dossier.end_event_index]
    if tuple(end) <= tuple(start):
        return None
    return {"version": 1, "path": str(dossier.source_path), "session": dossier.session_id,
            "start": start, "end": end, "fingerprint": dossier.fingerprint,
            "events": [[[e.start, e.index], [e.start, e.index + 1]] for e in dossier.source_events]}


def valid(value: Any) -> bool:
    basic = (isinstance(value, dict) and value.get("version") == 1
            and isinstance(value.get("path"), str) and isinstance(value.get("session"), str)
            and all(isinstance(value.get(k), list) and len(value[k]) == 2
                    and all(type(n) is int and n >= 0 for n in value[k]) for k in ("start", "end"))
            and tuple(value["start"]) < tuple(value["end"]))
    if not basic:
        return False
    events = value.get("events", [])
    return isinstance(events, list) and all(
        isinstance(span, list) and len(span) == 2
        and all(isinstance(pos, list) and len(pos) == 2 and all(type(n) is int and n >= 0 for n in pos)
                for pos in span)
        and tuple(value["start"]) <= tuple(span[0]) < tuple(span[1]) <= tuple(value["end"])
        for span in events)


def get(store, record_id: str) -> dict | None:
    raw = store.get_meta(SOURCE + record_id)
    try:
        value = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None
    return value if valid(value) else None


def put(store, record_id: str, value: dict) -> None:
    if not valid(value):
        raise ValueError("invalid transcript source range")
    store.set_meta(SOURCE + record_id, json.dumps(value))


def intersects(source: dict, parts) -> bool:
    return any((part := descriptor(d)) is not None and source["session"] == part["session"]
               and same_log(source["path"], part["path"])
               and tuple(source["start"]) < tuple(part["end"])
               and tuple(part["start"]) < tuple(source["end"]) for d in parts)


def owned(source: dict, parts) -> bool:
    """The complete source must be covered, not just overlap a repeated quote."""
    spans = [p for d in parts if (p := descriptor(d)) is not None
             and source["session"] == p["session"] and same_log(source["path"], p["path"])]
    ranges = source.get("events") or [[source["start"], source["end"]]]
    return bool(spans) and all(any(tuple(p["start"]) <= tuple(start) and tuple(p["end"]) >= tuple(end)
                                  for p in spans) for start, end in ranges)


def hold(store, record, reason: str) -> None:
    store.set_meta(PENDING + record.id, json.dumps({"session": record.source_session, "reason": reason}))


def release(store, record_id: str) -> None:
    store.connection.execute("DELETE FROM meta WHERE key=?", (PENDING + record_id,))


def waiting(store) -> list[dict]:
    """Discard stale local bookkeeping after sync replacement or a completed review."""
    found = []
    store.connection.execute("DELETE FROM meta WHERE key LIKE ? AND substr(key, ?) NOT IN (SELECT id FROM records)",
                             (SOURCE + "%", len(SOURCE) + 1))
    rows = store.connection.execute("SELECT key,value FROM meta WHERE key LIKE ?", (PENDING + "%",)).fetchall()
    for key, raw in rows:
        record_id = key[len(PENDING):]
        record = store.get(record_id)
        if record is None or record.status != "active" or "provisional" not in record.tags:
            release(store, record_id)
            if record is None:
                store.connection.execute("DELETE FROM meta WHERE key=?", (SOURCE + record_id,))
            continue
        try:
            info = json.loads(raw)
        except ValueError:
            info = {"reason": "source review bookkeeping needs repair"}
        found.append({"id": record_id, "session": record.source_session, **info})
    return found


def held(store, record, *, state=None) -> bool:
    if store.get_meta(PENDING + record.id):
        return True
    if record.source_agent == "learner" and record.source_session and get(store, record.id) is None:
        hold(store, record, "source has not been located for review")
        return True
    if source := get(store, record.id):
        from .state import LearnerState
        entry = (state or LearnerState()).entry(Path(source["path"]))
        if entry.get("history_hold") or entry.get("identity_conflicts"):
            return True  # temporary conditions must not create permanent queues
        if tuple(source["end"]) <= (int(entry.get("settled", 0)), int(entry.get("settled_event_index", 0))):
            hold(store, record, "source precedes final progress")
            return True
    return False


def safe_cut(memory, session, start: tuple[int, int], cut: tuple[int, int]) -> tuple[int, int]:
    """Stop a partial final pass before any original source window it would split."""
    cache, sources = {}, []
    for record in memory.store.tagged_active("provisional", session=session.session_id, limit=100_000):
        source = locate_legacy(memory, record, session.path, cache)
        if source is None:
            hold(memory.store, record, "original source has not been located unambiguously")
        elif same_log(source["path"], session.path):
            sources.append(source)
    while True:
        crossing = [tuple(source["start"]) for source in sources
                    if start <= tuple(source["start"]) < cut < tuple(source["end"])]
        if not crossing:
            return cut
        cut = min(crossing)


def locate_legacy(memory, record, path: Path, cache: dict, *, persist: bool = True) -> dict | None:
    """Recover one unambiguous original extraction dossier, never a repeated quote's new occurrence.

    Match the saved candidate to its original journaled run, timestamp and
    evidence. Cache each run's reconstruction; persist successful bindings.
    Missing, ambiguous or pruned evidence remains explicit pending work.
    """
    if source := get(memory.store, record.id):
        return source
    from .dossier import build_dossiers
    from .learner import _when_said
    from .settle import _shows
    from .transcripts import read_session

    rows = memory.store.events(kinds=["candidate"], record_id=record.id, limit=100)
    originals = [r for r in reversed(rows) if r["record_ids"][:1] == [record.id]
                 and str(r.get("outcome", "")).startswith("saved")]
    if not originals:
        return None
    event = originals[0]
    run, detail = event.get("run_id"), event["details"]
    if not run or not detail.get("evidence") or not detail.get("said_at"):
        return None
    cache_key = (run, record.source_session, path_key(path))
    if cache_key not in cache:
        cache[cache_key] = []
        row = memory.store.connection.execute(
            "SELECT details FROM events WHERE kind='learning' AND run_id=? ORDER BY seq DESC LIMIT 1", (run,)).fetchone()
        if row:
            try:
                ranges = [r for r in json.loads(row[0]).get("sessions", [])
                          if r.get("session") == record.source_session and same_log(r.get("path", ""), path)]
                if len(ranges) == 1:
                    span = ranges[0]
                    start, end = span["start"], span["end"]
                    if type(start) is int and type(end) is int and 0 <= start < end and end - start <= LEGACY_MAX_BYTES:
                        session, read_end = read_session(path, start=start, stop=end)
                        if session.session_id == record.source_session and read_end == end:
                            session.events = [e for e in session.events if e.start != start
                                              or e.index >= span.get("event_index", 0)]
                            cache[cache_key] = build_dossiers(session, require_user=start == 0
                                                             and not span.get("event_index", 0))
            except (OSError, ValueError, TypeError, KeyError):
                pass
    matches = [d for d in cache[cache_key] if _shows(detail["evidence"], d)
               and _when_said(detail["evidence"], d) == detail["said_at"]]
    if len(matches) == 1 and (source := descriptor(matches[0])) is not None:
        if persist:
            put(memory.store, record.id, source)
        return source
    return None
