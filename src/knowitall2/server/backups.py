"""The server's daily backup: a consistent copy made with SQLite's backup method.

Kept: the newest copy of each of the last seven days (UTC) that have one,
and the newest three whatever their day, so copies made after bursts of
changes never push out the daily ones. Getting the backups off the server's
computer is the user's own backup routine; they live in ``backups/`` in the
data home (the container's volume).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEEP_DAYS = 7
KEEP_NEWEST = 3
EVERY = timedelta(hours=24)
_PREFIX = "knowitall2-"
# ``exchange.RUN_KEY``: a copy's run id never matches a running server's, so a copy put back begins a new epoch.
_RUN_KEY = "server.run"
COPY_RUN = "copied"


def copy_database(database: Path, target: Path) -> None:
    """A consistent copy of the server's database, marked as a copy (see ``exchange.begin_run``)."""

    origin = sqlite3.connect(str(database), timeout=5.0)
    copy = sqlite3.connect(str(target))
    try:
        origin.backup(copy)
        copy.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (_RUN_KEY, COPY_RUN))
        copy.commit()
    finally:
        copy.close()
        origin.close()


def backups(folder: Path) -> list[Path]:
    """The backups in ``folder``, newest first."""

    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{_PREFIX}*.db"), key=lambda path: path.name, reverse=True)


def latest(folder: Path) -> Path | None:
    found = backups(folder)
    return found[0] if found else None


def made_at(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem[len(_PREFIX):], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def due(folder: Path, *, now: datetime) -> bool:
    newest = latest(folder)
    moment = made_at(newest) if newest else None
    return moment is None or now - moment >= EVERY


def _kept(found: list[Path]) -> set[Path]:
    kept, days = set(found[:KEEP_NEWEST]), set()
    for path in found:  # newest first, so each day's first is its newest
        moment = made_at(path)
        if moment is None:
            kept.add(path)  # not named like a backup this server made: left alone
        elif moment.date() not in days and len(days) < KEEP_DAYS:
            days.add(moment.date())
            kept.add(path)
    return kept


def make(database: Path, folder: Path, *, now: datetime) -> Path:
    """Copy the database into ``folder``; then remove the copies no longer kept (see the module)."""

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_PREFIX}{now.astimezone(timezone.utc):%Y%m%d-%H%M%S}.db"
    partial = target.with_suffix(".partial")
    for left in folder.glob(f"{_PREFIX}*.partial"):  # from a copy cut short (the server stopped part-way)
        left.unlink(missing_ok=True)
    copy_database(database, partial)
    partial.replace(target)
    found = backups(folder)
    kept = _kept(found)
    for old in found:
        if old not in kept:
            old.unlink(missing_ok=True)
    return target
