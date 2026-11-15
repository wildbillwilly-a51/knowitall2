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
    # Learning on the engine that did the work (``engines``; plan step 2, 2026-11-13), on by default: no engine may
    # be a requirement, and each learns in its own right (the user, 2026-11-15). ``backend``/``model`` stay the primary
    # engine (upkeep, documents, finding out); either engine takes over when the other cannot run.
    route_by_source: bool = True
    claude_model: str = CLAUDE_MODEL
    codex_model: str = "gpt-6.1-sol"
    codex_effort: str = "medium"
    # No longer used: Codex was once allowed to learn only after a quality gate against Sonnet. Kept so an older
    # configuration still reads.
    codex_qualified: str = ""


# Settings checked by type, whatever the file says: a hand-edited "false" must not turn routing on.
STRICT_SETTINGS = ("route_by_source", "claude_model", "codex_model", "codex_effort", "codex_qualified")


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
        if item.name in STRICT_SETTINGS and type(value) is not type(default):
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


# When learning first saw a log (an entry made since 0.12.10). Set-aside uses it to tell a chat learning knew and
# missed from history that was already old when first seen (review 2026-11-13: whether an entry existed when a run
# began was not that, since a run that runs out of budget still makes entries).
SEEN_AT = "seen_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_held(entry: dict[str, Any]) -> bool:
    return bool(entry.get("history_hold") or entry.get("identity_conflicts"))


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
        self.log_aliases: dict[str, str] = data.get("log_aliases", {}) if isinstance(data, dict) else {}
        self._file_ids: dict[str, tuple | None] = {}
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

    def entry(self, log: Path, *, physical: bool = True) -> dict[str, Any]:
        from . import codex_store
        from .log_identity import path_key, same_log

        # A Codex log Codex compressed or archived keeps its plain spelling's entry: one identity per transcript.
        log = codex_store.canonical(Path(log))
        key = path_key(log)
        if not physical and key in self.log_aliases and self.log_aliases[key] in self.logs:
            return self.logs[self.log_aliases[key]]
        matches = [(name, entry) for name, entry in self.logs.items() if path_key(name) == key]
        stored = codex_store.is_rollout(log.name) and not os.path.exists(log)
        if physical and not stored:
            # A compressed or archived Codex log has no file at its plain spelling to compare: it is matched by that
            # spelling and its name (below), not by a file-system comparison with every entry.
            def file_id(name):
                if name not in self._file_ids:
                    try:
                        status = os.stat(name)
                        self._file_ids[name] = (status.st_dev, status.st_ino) if status.st_ino else None
                    except OSError:
                        self._file_ids[name] = None
                return self._file_ids[name]
            identity = file_id(str(log))
            matched = {name for name, _ in matches}
            # At most one stat per indexed path in a learner run. Hot hooks
            # use the persisted alias map and lexical normalization only.
            matches += [(name, entry) for name, entry in self.logs.items() if name not in matched
                        and ((identity is not None and file_id(entry.get("log_path", name)) == identity)
                             or (identity is None and same_log(entry.get("log_path", name), log)))]
        by_name = False
        if not matches and codex_store.is_rollout(log.name):
            # An archived log's date folder is read from its name; an entry recorded under another day's folder of
            # the same Codex home (a time-zone edge) is the same transcript. Several such entries are kept together,
            # so differing progress is held as a conflict instead of starting again from 0.
            home = codex_store.home_of(log)
            matches = [(name, entry) for name, entry in self.logs.items()
                       if Path(str(entry.get("log_path") or name)).name.casefold() == log.name.casefold()
                       and codex_store.home_of(Path(str(entry.get("log_path") or name))) == home]
            by_name = bool(matches)
        if physical and (stored or by_name):
            # The whole recorded alias group of what matched is compared, so a hold on independent work in another
            # spelling stays (review 2026-11-13): persisted aliases, and entries whose recorded path is this spelling.
            group = {self.log_aliases[key]} if key in self.log_aliases else set()
            group |= {self.log_aliases.get(path_key(name), name) for name, _ in matches}
            matched = {name for name, _ in matches}
            matches += [(name, entry) for name, entry in self.logs.items() if name not in matched and (
                name in group or self.log_aliases.get(path_key(name)) in group
                or path_key(str(entry.get("log_path") or name)) == key)]
        if not matches:
            return self.logs.setdefault(key, {"offset": 0, "status": "new", "failures": 0,
                                             "log_path": str(log.absolute()), SEEN_AT: _now()})
        established = set(self.log_aliases.values())
        matches.sort(key=lambda pair: (pair[0] not in established,
                                       str(pair[0]).startswith("\\\\?\\"), "~" in pair[0], pair[0]))
        name, chosen = matches[0]
        conflicts = []
        for alias, other in matches[1:]:
            if (other.get("pending_reviews") or other.get("recovery_ranges")
                    or any(other.get(field) for field in ("failures", "final_failures", "recovery_failures"))
                    or (other.get("session_id") and chosen.get("session_id")
                        and other["session_id"] != chosen["session_id"])
                    or any((int(other.get(field, 0)), int(other.get(index, 0))) >
                           (int(chosen.get(field, 0)), int(chosen.get(index, 0)))
                           for field, index in (("offset", "event_index"), ("settled", "settled_event_index")))):
                conflicts.append(alias)
        if conflicts:
            chosen["identity_conflicts"] = conflicts
        else:
            chosen.pop("identity_conflicts", None)
        if physical:
            chosen["log_path"] = str(log.absolute())
            for alias, _ in matches:
                self.log_aliases[path_key(alias)] = name
            self.log_aliases[key] = name
        return chosen

    def _canonical_entries(self) -> dict[str, dict[str, Any]]:
        """One entry per transcript, however many spellings of its path were recorded."""
        from .log_identity import path_key
        entries: dict[str, dict[str, Any]] = {}
        for name in sorted(self.logs, key=lambda name: (name.startswith("\\\\?\\"), "~" in name, name)):
            key = path_key(name)
            canonical = self.log_aliases.get(key)
            if canonical in self.logs:
                entries[canonical] = self.logs[canonical]
            else:
                entries.setdefault(key, self.logs[name])
        return entries

    def backlog(self, *, quiet_before: float) -> dict[str, Any]:
        """The user's chats not yet learned: ``waiting`` (quiet since before ``quiet_before``, a POSIX time), when
        the longest-waiting of them went quiet (``oldest``), ``set_aside``, those the learner set aside unlearned,
        and ``held``, transcripts held for a history repair (learning waits for the repair, not for budget).

        A chat waits while its log holds more than its final pass settled, or while a queued recovery range is not
        yet replayed. A settled cursor alone does not mean a chat was learned:
        the learner's own set-aside moves it to the end without reading (review 2026-11-12), so those are counted
        apart until ``learn --catch-up`` takes them. A skip the user chose (``--start-from-now``) is not counted.
        Runs other programs started (Codex ``exec``, headless Claude Code) are not the user's chats. Logs held for a
        checkpoint conflict are reported by doctor's progress check.
        """
        from . import codex_store
        from .catchup import is_skipped
        from .learner import SET_ASIDE_AT
        from .transcripts import session_activity, session_folder
        waiting, oldest, set_aside, held, unreadable = 0, None, 0, 0, 0
        decodable = None
        for name, entry in self._canonical_entries().items():
            if entry.get("identity_conflicts"):
                continue
            if entry.get("history_hold"):
                held += 1  # learning defers it whatever the budget; only its history repair releases it
                continue
            path = Path(str(entry.get("log_path") or name))
            try:
                status = codex_store.status(path)   # a compressed or archived Codex log where it is now
            except codex_store.UnreadableLog:
                unreadable += 1
                continue
            except OSError:
                continue
            # A compressed log is not decoded here (doctor stays quick): a run other programs started is finished
            # by the first learning run that reads it, so one still waiting is counted.
            interactive = (lambda: True) if status.compressed else (
                lambda: session_folder(path, interactive_only=True) is not None)
            if entry.get(SET_ASIDE_AT) and is_skipped(entry):
                if interactive():
                    set_aside += 1
                continue
            settled = int(entry.get("settled", entry.get("offset", 0)) or 0)
            pending = bool(entry.get("recovery_ranges"))
            if (status.st_size <= settled and not pending) or status.st_mtime >= quiet_before:
                continue
            if status.compressed:
                decodable = codex_store.decoder() is not None if decodable is None else decodable
                if not decodable:
                    unreadable += 1
                    continue
            if not interactive():
                continue
            quiet_since = session_activity(path, modified=status.st_mtime)  # Windows delays an open log's mtime
            if quiet_since >= quiet_before:
                continue
            waiting += 1
            oldest = quiet_since if oldest is None else min(oldest, quiet_since)
        return {"waiting": waiting, "oldest": oldest, "set_aside": set_aside, "held": held, "unreadable": unreadable}

    def progress(self) -> dict[str, int]:
        """Quick completion and pending final work are independent phases."""
        entries = self._canonical_entries()
        return {
            "quick_done": sum(e.get("quick_status", e.get("status")) == "done" for e in entries.values()),
            "final_pending": sum(bool(e.get("pending_reviews", {}).get("final")) for e in entries.values()),
            "final_failures": sum(int(e.get("final_failures", 0)) for e in entries.values()),
            "awaiting_final": sum((int(e.get("offset", 0)), int(e.get("event_index", 0))) >
                                  (int(e.get("settled", 0)), int(e.get("settled_event_index", 0)))
                                  for e in entries.values()),
            "identity_conflicts": sum(bool(e.get("identity_conflicts")) for e in self.logs.values()),
            "history_holds": sum(bool(e.get("history_hold")) for e in entries.values()),
        }

    def record_call(self, *, at: datetime, session: str, outcome: str, origin: str | None = None,
                    attempt: str | None = None) -> None:
        """Charge one model call to the budgets; ``attempt`` names it in the call ledger (``ledger``)."""
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        call = {"at": at.isoformat(), "session": session, "outcome": outcome}
        if origin:
            call["origin"] = origin
        if isinstance(attempt, str) and attempt:   # a ledger id; anything else (an engine stand-in's) is left out
            call["attempt"] = attempt
            from . import ledger

            ledger.charge(attempt, origin=origin, outcome=outcome, session=session)
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
        # A log deleted long ago (an agent's old sessions cleaned up) needs no entry: the file stays small. A Codex
        # log compressed or archived still exists (review 2026-11-13: forgetting it would learn it again from 0).
        from . import codex_store

        cutoff = (datetime.now(timezone.utc) - timedelta(days=FORGET_MISSING_LOGS_DAYS)).isoformat()
        aliases = {alias for entry in self.logs.values() for alias in entry.get("identity_conflicts", [])}
        for key in [key for key, entry in self.logs.items() if key not in aliases and not source_held(entry)
                    and str(entry.get("updated_at") or "9999") < cutoff
                    and not codex_store.exists(entry.get("log_path", key))]:
            del self.logs[key]
        document = {"schema_version": 1, "logs": self.logs, "calls": self.calls, "fingerprints": self.fingerprints,
                    "review_resets": self.review_resets, "log_aliases": self.log_aliases}
        write_text_atomic(self.path, json.dumps(document, indent=2) + "\n")


def _recent_call(call: dict, cutoff: datetime) -> bool:
    try:
        at = datetime.fromisoformat(call["at"])
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) >= cutoff
    except (ValueError, KeyError, TypeError):
        return False


def chargeable(error: BaseException | None) -> bool:
    """Whether a model call counts toward a budget, for every consumer alike (joint review 2026-11-13).

    An answer counts. A failure counts when the engine was launched (``attempted``) and the failure was not
    the engine itself being unavailable (``blocking``: a usage limit, a sign-in, a timeout, a missing
    engine), which stops the run without blaming its work. A refusal before launch (a backend pause, a
    provenance hold) and an unexpected error that is not an engine's answer count nothing. The call ledger
    still records every one of them, so what ran stays visible whether or not it was charged.
    """

    if error is None:
        return True
    return bool(getattr(error, "attempted", False)) and not getattr(error, "blocking", True)


OTHER_CALLS_FILE = "other-calls.jsonl"
OTHER_CALLS_KEPT = 200


def record_other_call(what: str, *, at: datetime | None = None, folder: Path | None = None, outcome: str = "ok",
                      attempt: str | None = None) -> None:
    """Count a model call the user started, such as an agent looking up a system, toward the daily total.

    Kept apart from the learner's own state, which a running learner rewrites.
    """

    path = (folder if folder is not None else data_home() / "learner") / OTHER_CALLS_FILE
    now = at or datetime.now(timezone.utc)
    moment = now.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with waiting_lock(path.with_suffix(".lock")):
        calls = [item for item in other_calls(path.parent) if _recent_call(item, now - timedelta(days=CALL_HISTORY_DAYS))]
        call = {"at": moment, "session": what, "outcome": outcome, "origin": "user"}
        if isinstance(attempt, str) and attempt:
            call["attempt"] = attempt
            from . import ledger

            ledger.charge(attempt, origin="user", outcome=outcome, session=what)
        calls.append(call)
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
        from .moments import runnable_requests

        if runnable_requests():
            from ..hooks import maybe_start_learner

            maybe_start_learner(requests=True)
    except Exception as exc:
        from .. import journal

        journal.problem("learning", f"waiting requests could not start a learner: {type(exc).__name__}: {exc}")


def day_ago(now: datetime) -> datetime:
    return now - timedelta(days=1)
