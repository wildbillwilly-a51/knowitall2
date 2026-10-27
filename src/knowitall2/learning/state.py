"""Learner settings (the user's consent and choices), per-log progress, and the run lock."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Iterator

from ..files import read_text, write_text_atomic
from ..paths import data_home

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# The learning model on Claude Code: Sonnet found more, and read it more accurately, than Haiku
# in a benchmark of six real sessions (2026-09-29), for about 1.4 times the usage per call.
CLAUDE_MODEL = "sonnet"
# Earlier defaults, moved to the current one by an update, unless the user chose the model
# (``LearnerSettings.model_chosen``).
FORMER_CLAUDE_MODELS = ("haiku",)
MAX_FAILURES = 3
# Not "lock", which earlier versions created and deleted: during an update, runs of the two never mix.
LOCK_FILE = "run.lock"
CALL_HISTORY_DAYS = 7  # never discard calls inside an active 24-hour budget window by count
FINGERPRINTS_KEPT = 5000
# A log missing this long after its entry last changed is forgotten (its sessions were deleted).
FORGET_MISSING_LOGS_DAYS = 90


@dataclass
class LearnerSettings:
    enabled: bool = False
    backend: str = "claude-cli"
    model: str = CLAUDE_MODEL
    # The user named the model (in the app, or with --model), so an update leaves it as it is (review 2026-10-24,
    # F-33). Off for a model that came as a default; a choice made before 0.12.0 kept no such mark.
    model_chosen: bool = False
    max_calls_per_run: int = 10
    max_calls_per_day: int = 60
    # How long a chat stays quiet before its final pass, when a stretch has settled (90 minutes lost none of the
    # moments another chat needed what the quick pass had learned, 2026-10-13).
    settle_minutes: int = 90
    # Background upkeep's own calls a day, apart from the daily limit, which learning from the user's work can use
    # up: maintenance made no call for 13 days while it did (2026-10-11).
    maintenance_calls_per_day: int = 8
    catalog_calls_per_day: int = 2
    command_calls_per_day: int = 2


# Where a model call came from: learning from the user's work and its filing ("work"), background learning, the
# final pass of settled chats ("learning"), what the user started ("user"), and background upkeep. Each kind of
# background work has its own daily allowance, which no other can use up: the daily limit is background
# learning's, and work and the user's own calls wait for none (the user, 2026-09-30; upkeep made no call for 13
# days while work used the limit, and background learning none on busy days, 2026-10-13).
UPKEEP_ORIGINS = ("maintenance", "catalog", "commands")


def upkeep_allowance(settings: LearnerSettings, origin: str) -> int:
    return {"maintenance": settings.maintenance_calls_per_day, "catalog": settings.catalog_calls_per_day,
            "commands": settings.command_calls_per_day}[origin]


def call_origin(call: dict[str, Any]) -> str:
    """Where ``call`` came from; one recorded before origins were kept counts toward the daily limit, as it did."""

    origin = call.get("origin") if isinstance(call, dict) else None
    return origin if isinstance(origin, str) and origin else "learning"


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
    defaults = LearnerSettings()
    chosen = {}
    for item in fields(LearnerSettings):
        if item.name not in learning:
            continue
        value, default = learning[item.name], getattr(defaults, item.name)
        # A count edited by hand into something else keeps its default.
        if isinstance(default, int) and not isinstance(default, bool) and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0):
            continue
        chosen[item.name] = value
    if "model_chosen" not in learning and isinstance(learning.get("model"), str) and learning["model"]:
        # Saved before the mark existed (0.12.0 and earlier). Every such install already had its former default
        # moved by an update, so a model other than today's Claude default was chosen; a Codex model, which an
        # update moved only from Codex's fast model, counts as chosen too (Codex's review of step A).
        chosen["model_chosen"] = learning.get("backend", defaults.backend) != "claude-cli" \
            or learning["model"] != CLAUDE_MODEL
    old_wait = learning.get("idle_minutes")
    if "settle_minutes" not in chosen and isinstance(old_wait, int) and not isinstance(old_wait, bool):
        # Before 0.11 the wait was ``idle_minutes``: a longer wait the user chose is kept (candidate review, C-L6).
        chosen["settle_minutes"] = max(old_wait, defaults.settle_minutes)
    return LearnerSettings(**chosen)


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
        self.review_resets: list[dict] = data.get("review_resets", []) if isinstance(data, dict) else []
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
        if key not in self.logs:
            resolved = os.path.normcase(os.path.realpath(log))
            for existing in self.logs:
                if (Path(existing).name.casefold() == log.name.casefold()
                        and os.path.normcase(os.path.realpath(existing)) == resolved):
                    return self.logs[existing]
        return self.logs.setdefault(key, {"offset": 0, "status": "new", "failures": 0})

    def record_call(self, *, at: datetime, session: str, outcome: str, origin: str | None = None) -> None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        call = {"at": at.isoformat(), "session": session, "outcome": outcome}
        if origin:
            call["origin"] = origin
        self.calls.append(call)
        cutoff = at - timedelta(days=CALL_HISTORY_DAYS)
        self.calls[:] = [item for item in self.calls if _recent_call(item, cutoff)]

    def calls_since(self, moment: datetime, *, origins: Collection[str] | None = None) -> int:
        """Model calls since ``moment``: the learner's, and those the user started (they count, but wait for no limit).

        With ``origins``, only the calls that came from one of them.
        """

        count = 0
        # Calls the user started (kept apart, and written before 0.11 without an origin) are the user's.
        started = [{**call, "origin": "user"} for call in other_calls(self.path.parent) if isinstance(call, dict)]
        for call in [*self.calls, *started]:
            if origins is not None and call_origin(call) not in origins:
                continue
            try:
                at = datetime.fromisoformat(call["at"])
                if moment <= (at if at.tzinfo else at.replace(tzinfo=timezone.utc)):
                    count += 1
            except (KeyError, TypeError, ValueError):
                continue
        return count

    def limited_calls_since(self, moment: datetime) -> int:
        """The calls since ``moment`` that count toward the daily limit: background learning's."""

        return self.calls_since(moment, origins=("learning",))

    def save(self) -> None:
        # A log deleted long ago (an agent's old sessions cleaned up) needs no entry: the file stays small.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=FORGET_MISSING_LOGS_DAYS)).isoformat()
        for key in [key for key, entry in self.logs.items()
                    if str(entry.get("updated_at") or "9999") < cutoff and not os.path.exists(key)]:
            del self.logs[key]
        document = {"schema_version": 1, "logs": self.logs, "calls": self.calls, "fingerprints": self.fingerprints,
                    "review_resets": self.review_resets}
        write_text_atomic(self.path, json.dumps(document, indent=2) + "\n")


def _recent_call(call: dict, cutoff: datetime) -> bool:
    try:
        at = datetime.fromisoformat(call["at"])
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) >= cutoff
    except (ValueError, KeyError, TypeError):
        return False


OTHER_CALLS_FILE = "other-calls.jsonl"
OTHER_CALLS_KEPT = 200


def record_other_call(what: str, *, at: datetime | None = None, folder: Path | None = None) -> None:
    """Count a model call the user started, such as an agent looking up a system, toward the daily total.

    Kept apart from the learner's own state, which a running learner rewrites.
    """

    path = (folder if folder is not None else data_home() / "learner") / OTHER_CALLS_FILE
    now = at or datetime.now(timezone.utc)
    moment = now.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with waiting_lock(path.with_suffix(".lock")):
        calls = [item for item in other_calls(path.parent) if _recent_call(item, now - timedelta(days=CALL_HISTORY_DAYS))]
        calls.append({"at": moment, "session": what, "outcome": "ok", "origin": "user"})
        write_text_atomic(path, "\n".join(json.dumps(call) for call in calls) + "\n")


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


@contextmanager
def waiting_lock(path: Path, *, timeout: float = 5.0):
    """Short bookkeeping lock; contention must not masquerade as another learning run."""
    deadline = time.monotonic() + timeout
    lock = RunLock(path)
    while True:
        try:
            lock.__enter__()
            break
        except LockBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    try:
        yield lock
    finally:
        lock.__exit__()


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
