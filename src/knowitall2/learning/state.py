"""Learner settings (the user's consent and choices), per-log progress, and the run lock."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ..files import read_text, write_text_atomic
from ..paths import data_home

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# The learning model on Claude Code: Sonnet found more, and read it more accurately, than Haiku
# in a benchmark of six real sessions (2026-09-29), for about 1.4 times the usage per call.
CLAUDE_MODEL = "sonnet"
# Earlier defaults, moved to the current one by an update (a choice made on purpose looks the same).
FORMER_CLAUDE_MODELS = ("haiku",)
MAX_FAILURES = 3
# Not "lock", which earlier versions created and deleted: during an update, runs of the two never mix.
LOCK_FILE = "run.lock"
CALL_HISTORY_KEPT = 500
FINGERPRINTS_KEPT = 5000
# A log missing this long after its entry last changed is forgotten (its sessions were deleted).
FORGET_MISSING_LOGS_DAYS = 90


@dataclass
class LearnerSettings:
    enabled: bool = False
    backend: str = "claude-cli"
    model: str = CLAUDE_MODEL
    max_calls_per_run: int = 10
    max_calls_per_day: int = 60
    idle_minutes: int = 20


def config_path() -> Path:
    return data_home() / "config.json"


def load_settings() -> LearnerSettings:
    try:
        data = json.loads(read_text(config_path()))
    except (OSError, ValueError):
        return LearnerSettings()
    learning = data.get("learning") if isinstance(data, dict) else None
    if not isinstance(learning, dict):
        return LearnerSettings()
    known = {item.name for item in fields(LearnerSettings)}
    return LearnerSettings(**{key: value for key, value in learning.items() if key in known})


def save_settings(settings: LearnerSettings) -> None:
    path = config_path()
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["learning"] = asdict(settings)
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")


class LearnerState:
    """Progress for each session log, plus recent model calls for the daily budget."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else data_home() / "learner" / "state.json"
        try:
            data = json.loads(read_text(self.path))
        except (OSError, ValueError):
            data = {}
        self.logs: dict[str, dict[str, Any]] = data.get("logs", {}) if isinstance(data, dict) else {}
        self.calls: list[dict[str, Any]] = data.get("calls", []) if isinstance(data, dict) else []
        self.fingerprints: list[str] = data.get("fingerprints", []) if isinstance(data, dict) else []
        self._seen = set(self.fingerprints)

    def seen(self, fingerprint: str) -> bool:
        """True when identical dossier content was already learned from, in any session file."""

        return fingerprint in self._seen

    def mark_seen(self, fingerprint: str) -> None:
        if fingerprint not in self._seen:
            self.fingerprints.append(fingerprint)
            self._seen.add(fingerprint)
            if len(self.fingerprints) > FINGERPRINTS_KEPT:
                for stale in self.fingerprints[:-FINGERPRINTS_KEPT]:
                    self._seen.discard(stale)
                del self.fingerprints[:-FINGERPRINTS_KEPT]

    def entry(self, log: Path) -> dict[str, Any]:
        key = os.path.normcase(str(log))
        return self.logs.setdefault(key, {"offset": 0, "status": "new", "failures": 0})

    def record_call(self, *, at: datetime, session: str, outcome: str) -> None:
        self.calls.append({"at": at.isoformat(), "session": session, "outcome": outcome})
        del self.calls[:-CALL_HISTORY_KEPT]

    def calls_since(self, moment: datetime) -> int:
        """Model calls since ``moment``: the learner's, and those the user started (they count, but wait for no limit)."""

        count = 0
        for call in [*self.calls, *other_calls(self.path.parent)]:
            try:
                if datetime.fromisoformat(call["at"]) >= moment:
                    count += 1
            except (KeyError, TypeError, ValueError):
                continue
        return count

    def save(self) -> None:
        # A log deleted long ago (an agent's old sessions cleaned up) needs no entry: the file stays small.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=FORGET_MISSING_LOGS_DAYS)).isoformat()
        for key in [key for key, entry in self.logs.items()
                    if str(entry.get("updated_at") or "9999") < cutoff and not os.path.exists(key)]:
            del self.logs[key]
        document = {"schema_version": 1, "logs": self.logs, "calls": self.calls, "fingerprints": self.fingerprints}
        write_text_atomic(self.path, json.dumps(document, indent=2) + "\n")


OTHER_CALLS_FILE = "other-calls.jsonl"
OTHER_CALLS_KEPT = 200


def record_other_call(what: str, *, at: datetime | None = None, folder: Path | None = None) -> None:
    """Count a model call the user started, such as an agent looking up a system, toward the daily total.

    Kept apart from the learner's own state, which a running learner rewrites.
    """

    path = (folder if folder is not None else data_home() / "learner") / OTHER_CALLS_FILE
    moment = (at or datetime.now(timezone.utc)).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Appended, so two calls finishing together both count; trimmed now and then, which drops only old lines.
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"at": moment, "session": what, "outcome": "ok"}) + "\n")
    calls = other_calls(path.parent)
    if len(calls) > 2 * OTHER_CALLS_KEPT:
        write_text_atomic(path, "\n".join(json.dumps(call) for call in calls[-OTHER_CALLS_KEPT:]) + "\n")


def other_calls(folder: Path) -> list[dict[str, Any]]:
    try:
        text = read_text(folder / OTHER_CALLS_FILE)
    except (OSError, ValueError):
        return []
    calls = []
    for line in text.splitlines():
        try:
            call = json.loads(line)
        except ValueError:
            continue
        if isinstance(call, dict):
            calls.append(call)
    return calls


class LockBusy(RuntimeError):
    """Another learner run holds the lock."""


class RunLock:
    """An exclusive lock, so only one learner runs at a time.

    The operating system holds it on an open handle to the lock file, so it is
    free the moment its holder ends, however it ends, and a long run never
    looks abandoned. The file itself stays; only the holder can release it.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else data_home() / "learner" / LOCK_FILE
        self._handle: int | None = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.path, os.O_RDWR | os.O_CREAT)
        if not _try_lock(handle):
            os.close(handle)
            raise LockBusy("another learner run is in progress")
        self._handle = handle
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            _unlock(handle)
            os.close(handle)

    def busy(self) -> bool:
        """True while a run holds the lock: tries to take it, and lets go at once."""

        try:
            handle = os.open(self.path, os.O_RDONLY)
        except OSError:
            return False  # never taken on this computer
        try:
            if not _try_lock(handle):
                return True
            _unlock(handle)
            return False
        finally:
            os.close(handle)


def _try_lock(handle: int) -> bool:
    """Lock the file's first byte without waiting; False when another handle holds it."""

    try:
        if sys.platform == "win32":
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        return False
    return True


def _unlock(handle: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass  # closing the handle lets go of it too


@contextmanager
def learner_lock() -> Iterator[RunLock]:
    """The run lock, for work other than the learner's own loop (which learns waiting requests itself).

    A hook that finds the lock taken leaves its request for the holder, so when
    this lock is let go, a learner is started for any request that came meanwhile.
    """

    taken = False
    try:
        with RunLock() as lock:
            taken = True
            yield lock
    finally:
        if taken:
            _start_for_waiting_requests()


def _start_for_waiting_requests() -> None:
    try:
        from .moments import pending_requests

        if pending_requests():
            from ..hooks import maybe_start_learner

            maybe_start_learner(requests=True)
    except Exception as exc:
        from .. import journal

        journal.problem("learning", f"waiting requests could not start a learner: {type(exc).__name__}: {exc}")


def day_ago(now: datetime) -> datetime:
    return now - timedelta(days=1)
