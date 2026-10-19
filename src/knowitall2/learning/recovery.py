"""Explicit, hash-bound recovery of failed reviews; planning is read-only.

Old journals have session IDs and dossier counts, but no byte boundaries.
Those incidents are labelled conservative whole-session re-reviews, never
presented as reconstructed exact ranges. They need a separate explicit opt-in.
Normal learning cursors are never rewound.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..files import write_text_atomic
from ..paths import database_path, data_home
from .state import LearnerState, learner_lock
from .transcripts import line_boundary, read_session


def prefix_hash(path: Path, end: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        left = end
        while left:
            data = stream.read(min(left, 1024 * 1024))
            if not data:
                raise ValueError("transcript is shorter than the recovery range")
            digest.update(data)
            left -= len(data)
    return digest.hexdigest()


def range_id(item: dict) -> str:
    identity = {key: item[key] for key in ("path", "session", "start", "event_index", "end",
                                         "end_event_index", "sha256", "scope")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def plan(connection, state: LearnerState, *, since: str = "") -> dict:
    incidents = {}
    for at, details in connection.execute("SELECT at, details FROM events WHERE kind='learning' AND at >= ? ORDER BY seq", (since,)):
        detail = json.loads(details)
        for session in detail.get("skipped", []):
            incidents.setdefault(session, []).append({"at": at, "dossiers": next(
                (item.get("dossiers") for item in detail.get("sessions", []) if item.get("session") == session), None)})
    entries, unresolved = [], []
    for session, events in incidents.items():
        matches = [(Path(path), entry) for path, entry in state.logs.items()
                   if entry.get("session_id") == session or session in path]
        usable = False
        for path, entry in matches:
            if not path.is_file():
                continue
            end = line_boundary(path)
            parsed, _ = read_session(path, stop=end)
            if parsed.session_id != session:
                continue
            item = {"path": str(path.resolve()), "session": session, "start": 0, "event_index": 0,
                    "end": end, "end_event_index": 0, "sha256": prefix_hash(path, end),
                    "scope": "historical_whole_session", "incidents": events,
                    "reason": "old journal omitted byte boundaries; conservative rereview requires explicit approval"}
            item["id"] = range_id(item)
            entries.append(item)
            usable = True
        if not usable:
            unresolved.append({"session": session, "reason": "no verified source transcript remains", "incidents": events})
    resets = []
    for log in (data_home() / "logs").glob("*problem*.jsonl"):
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("source") != "maintenance" or event.get("at", "") < since:
                continue
            if not any(word in event.get("message", "").casefold() for word in ("session limit", "weekly limit", "usage limit")):
                continue
            rows = connection.execute("SELECT fingerprint, scope_key, reviewed_at, failures FROM reviews "
                                      "WHERE reviewed_at=? AND outcome='failed'", (event["at"],)).fetchall()
            if len(rows) == 1:
                fingerprint, scope, at, failures = rows[0]
                item = {"fingerprint": fingerprint, "scope_key": scope, "reviewed_at": at, "failures": failures}
                if item not in resets:
                    resets.append(item)
    return {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "ranges": entries,
            "review_resets": resets,
            "unresolved": unresolved, "warning": "Multiple copies of a session are explicit separate paths. Select the intended source; do not queue all copies blindly."}


def validate(item: dict) -> None:
    if item.get("id") != range_id(item):
        raise ValueError("recovery manifest identity does not match its range")
    if item["scope"] not in {"historical_whole_session", "exact_range"}:
        raise ValueError("unknown recovery scope")
    for key in ("start", "event_index", "end", "end_event_index"):
        if type(item[key]) is not int or item[key] < 0:
            raise ValueError("recovery boundaries must be nonnegative integers")
    if item["start"] >= item["end"]:
        raise ValueError("recovery range must be nonempty")
    path = Path(item["path"])
    if not path.is_absolute() or str(path.resolve()) != item["path"]:
        raise ValueError("recovery transcript must use its exact absolute path")
    if prefix_hash(path, item["end"]) != item["sha256"]:
        raise ValueError("recovery transcript changed; prepare a new plan")
    with path.open("rb") as source:
        for boundary in (item["start"], item["end"]):
            if boundary:
                source.seek(boundary - 1)
                if source.read(1) != b"\n":
                    raise ValueError("recovery range must end on complete transcript lines")
    session, _ = read_session(path, stop=item["end"])
    if session.session_id != item["session"]:
        raise ValueError("recovery transcript session does not match")


def queue(manifest: dict, state: LearnerState, *, allow_whole_session: bool = False) -> int:
    if not isinstance(manifest, dict) or manifest.get("format") != 1 or not isinstance(manifest.get("ranges"), list):
        raise ValueError("unsupported recovery manifest")
    if manifest.get("unresolved"):
        raise ValueError("recovery plan still contains unresolved source transcripts")
    entries = manifest["ranges"]
    seen_sessions = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("recovery ranges must be objects")
        validate(item)
        if not any(Path(path).resolve() == Path(item["path"]) for path in state.logs):
            raise ValueError("recovery transcript is not indexed in this installation's learner state")
        if item["scope"] == "historical_whole_session" and not allow_whole_session:
            raise ValueError("historical boundaries are unknown; --allow-whole-session is required")
        if item["session"] in seen_sessions:
            raise ValueError("select one verified source path per session before queueing recovery")
        seen_sessions.add(item["session"])
    resets = manifest.get("review_resets", [])
    for item in resets:
        if (not isinstance(item, dict) or set(item) != {"fingerprint", "scope_key", "reviewed_at", "failures"}
                or not all(isinstance(item[key], str) for key in ("fingerprint", "scope_key", "reviewed_at"))
                or type(item["failures"]) is not int or item["failures"] < 1):
            raise ValueError("invalid reviewed maintenance recovery entry")
    added = 0
    for item in entries:
        # Windows logs may already be indexed using an 8.3 spelling of the same path.
        entry = next((value for path, value in state.logs.items()
                      if Path(path).resolve() == Path(item["path"])), None)
        if entry is None:
            entry = state.entry(Path(item["path"]))
        if item["id"] in entry.get("recovery_completed", []) or any(
                old["id"] == item["id"] for old in entry.get("recovery_ranges", [])):
            continue
        entry.setdefault("recovery_ranges", []).append({**item, "cursor": item["start"],
                                                        "cursor_event_index": item["event_index"]})
        entry.pop("recovery_retry_at", None)
        added += 1
    for item in resets:
        if item not in state.review_resets:
            state.review_resets.append(item)
    return added


def reset_reviews(store, state: LearnerState) -> int:
    """Reopen only explicitly approved, unchanged failure snapshots; safe to replay after interruption."""
    count = 0
    with store.transaction():
        for item in state.review_resets:
            count += store.connection.execute("DELETE FROM reviews WHERE fingerprint=? AND scope_key=? "
                "AND reviewed_at=? AND failures=? AND outcome='failed'", (item["fingerprint"], item["scope_key"],
                item["reviewed_at"], item["failures"])).rowcount
    state.review_resets.clear()
    state.save()
    return count


def run(arguments) -> int:
    try:
        if arguments.recovery_plan:
            connection = sqlite3.connect(database_path().resolve().as_uri() + "?mode=ro", uri=True)
            try:
                result = plan(connection, LearnerState(), since=arguments.since or "")
            finally:
                connection.close()
            target = Path(arguments.recovery_plan)
            if target.exists():
                raise ValueError("recovery plan already exists; choose a new output path")
            write_text_atomic(target, json.dumps(result, indent=2) + "\n")
            print(f"Prepared {len(result['ranges'])} candidate ranges; {len(result['unresolved'])} unresolved. No learning or progress changed.")
        else:
            manifest = json.loads(Path(arguments.recover).read_text(encoding="utf-8"))
            with learner_lock():
                state = LearnerState()
                count = queue(manifest, state, allow_whole_session=arguments.allow_whole_session)
                state.save()
            print(f"Queued {count} approved recovery range(s). Normal progress was preserved; learning runs separately.")
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
        print(f"Recovery was not applied: {exc}")
        return 1
