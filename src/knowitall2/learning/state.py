"""Learner settings (the user's consent and choices), per-log progress, and the run lock."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..files import write_text_atomic
from ..paths import data_home

# The learning model on Claude Code: Sonnet found more, and read it more accurately, than Haiku
# in a benchmark of six real sessions (2026-09-29), for about 1.4 times the usage per call.
CLAUDE_MODEL = "sonnet"
# Earlier defaults, moved to the current one by an update (a choice made on purpose looks the same).
FORMER_CLAUDE_MODELS = ("haiku",)
MAX_FAILURES = 3
LOCK_STALE_SECONDS = 30 * 60
CALL_HISTORY_KEPT = 500
FINGERPRINTS_KEPT = 5000


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
        data = json.loads(config_path().read_text(encoding="utf-8"))
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
        data = json.loads(path.read_text(encoding="utf-8"))
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
            data = json.loads(self.path.read_text(encoding="utf-8"))
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
    kept = other_calls(path.parent)[-(OTHER_CALLS_KEPT - 1):]
    lines = [json.dumps(call) for call in [*kept, {"at": moment, "session": what, "outcome": "ok"}]]
    write_text_atomic(path, "\n".join(lines) + "\n")


def other_calls(folder: Path) -> list[dict[str, Any]]:
    try:
        text = (folder / OTHER_CALLS_FILE).read_text(encoding="utf-8")
    except OSError:
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
    """An exclusive lock file, so only one learner runs at a time. Stale locks are reclaimed."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else data_home() / "learner" / "lock"
        self._held = False

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale():
                    self.path.unlink(missing_ok=True)
                    continue
                raise LockBusy("another learner run is in progress") from None
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
            self._held = True
            return self
        raise LockBusy("could not take the learner lock")

    def __exit__(self, *exc_info: object) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def busy(self) -> bool:
        """True while another run holds a lock that is not stale."""

        return self.path.exists() and not self._stale()

    def _stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS
        except OSError:
            return True


def day_ago(now: datetime) -> datetime:
    return now - timedelta(days=1)
