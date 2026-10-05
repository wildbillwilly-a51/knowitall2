"""Catch up on past sessions that were skipped as already covered.

``learn --start-from-now`` marks an agent's existing session logs as read,
for history captured elsewhere. When that turns out to be wrong, for example
because the other system stopped reading new sessions at some point, the user
can choose, by project folder, which of those sessions to learn from after
all. Learning then reads them within its normal budget, newest sessions first.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .dossier import build_dossiers
from .learner import LEARN_TO, RESUME_AT
from .state import LearnerState, learner_lock
from .transcripts import read_session, session_folder, session_logs

SKIPPED = "baseline"
# Where a session's set-aside part ends. It stays when later activity in the same
# log is learned, so the part before it can still be caught up.
SET_ASIDE_TO = "baseline_to"


def is_skipped(entry: dict[str, Any]) -> bool:
    """Whether part of a session was set aside as already covered and has not been caught up."""

    if entry.get("caught_up_at"):
        return False
    return entry.get("status") == SKIPPED or int(entry.get(SET_ASIDE_TO) or 0) > 0


REPAIRED_MARKER = "set-aside-repaired"


def repair_once(store) -> int:
    """Run ``repair`` once per installation (from the update); returns how many sessions were put back."""

    from ..paths import data_home

    marker = data_home() / "learner" / REPAIRED_MARKER
    if marker.exists():
        return 0
    with learner_lock():
        state = LearnerState()
        repaired = repair(store, state)
        state.save()
    marker.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    return repaired


def repair(store, state: LearnerState) -> int:
    """Put back the set-aside mark on sessions a learning run marked done without learning them.

    Until 0.6.0 a run that found only a few new lines in a set-aside log
    marked it done, which hid it from catching up. Such a session has no
    learning candidate and no memory in the store. Returns how many were put back.
    """

    repaired = 0
    for entry in state.logs.values():
        if entry.get("status") != "done" or is_skipped(entry) or entry.get("caught_up_at"):
            continue
        session = entry.get("session_id")
        if not session or not int(entry.get("offset") or 0):
            continue
        if not store.learned_from(session):
            entry[SET_ASIDE_TO] = int(entry["offset"])
            repaired += 1
    return repaired


def groups(*, since: str | None = None) -> list[dict[str, Any]]:
    """The skipped sessions, grouped by the folder they worked in, most sessions first."""

    state = LearnerState()
    found: dict[str, dict[str, Any]] = {}
    for log, active in _skipped(state, since=since):
        folder = session_folder(log) or "(unknown folder)"
        key = os.path.normcase(folder)
        group = found.setdefault(key, {"folder": folder, "sessions": 0, "last_active": active})
        group["sessions"] += 1
        group["last_active"] = max(group["last_active"], active)
    return sorted(found.values(), key=lambda group: (-group["sessions"], group["folder"].casefold()))


def estimate(folder: str, *, since: str | None = None) -> dict[str, int]:
    """How many sessions under ``folder`` would be learned, and about how many model calls that takes."""

    sessions = calls = 0
    state = LearnerState()
    for log in _matching(folder, since=since, state=state):
        session, _ = read_session(log, stop=_set_aside_end(state.entry(log)))
        sessions += 1
        calls += len(build_dossiers(session))
    return {"sessions": sessions, "calls": calls}


def _set_aside_end(entry: dict[str, Any]) -> int | None:
    """Where the part to catch up ends, when learning went on past it; None for the whole log."""

    bound = int(entry.get(SET_ASIDE_TO) or 0)
    return bound if bound and int(entry.get("offset") or 0) > bound else None


def catch_up(folder: str, *, since: str | None = None, max_calls: int | None = None) -> int:
    """Mark the skipped sessions under ``folder`` (and its subfolders) to be learned; returns how many.

    ``max_calls`` takes the newest sessions first, up to about that many model calls.
    """

    moment = datetime.now(timezone.utc).isoformat()
    with learner_lock():
        state = LearnerState()
        marked = calls = 0
        for log in _matching(folder, since=since, state=state):
            if max_calls is not None:
                session, _ = read_session(log, stop=_set_aside_end(state.entry(log)))
                needed = len(build_dossiers(session))
                if calls and calls + needed > max_calls:
                    continue
                calls += needed
            entry = state.entry(log)
            resume, bound = int(entry.get("offset") or 0), int(entry.get(SET_ASIDE_TO) or 0)
            entry.update({"offset": 0, "status": "new", "failures": 0, "updated_at": moment, "caught_up_at": moment})
            if bound and resume > bound:
                # Learning went on past the set-aside part: read only that part, then carry on from there.
                entry.update({LEARN_TO: bound, RESUME_AT: resume})
            marked += 1
        state.save()
    return marked


def _matching(folder: str, *, since: str | None, state: LearnerState | None = None) -> list[Path]:
    target = os.path.normcase(os.path.normpath(folder))
    matched = []
    for log, _ in sorted(_skipped(state or LearnerState(), since=since), key=lambda item: item[1], reverse=True):
        worked_in = session_folder(log)
        if worked_in is None:
            continue
        candidate = os.path.normcase(os.path.normpath(worked_in))
        if candidate == target or candidate.startswith(target.rstrip(os.sep) + os.sep):
            matched.append(log)
    return matched


def _skipped(state: LearnerState, *, since: str | None) -> list[tuple[Path, str]]:
    """Logs marked as already covered, with when each was last active (UTC ISO), optionally since a date."""

    found = []
    for log in session_logs():
        if not is_skipped(state.logs.get(os.path.normcase(str(log)), {})):
            continue
        try:
            active = datetime.fromtimestamp(log.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            continue
        if since and active[:10] < since:
            continue
        found.append((log, active))
    return found
