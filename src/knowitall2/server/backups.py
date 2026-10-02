"""The server's daily backup: a consistent copy made with SQLite's backup method, the last seven kept.

Getting the backups off the server's computer is the user's own backup
routine; they live in ``backups/`` in the data home (the container's volume).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEEP = 7
EVERY = timedelta(hours=24)
_PREFIX = "knowitall2-"


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


def make(database: Path, folder: Path, *, now: datetime, keep: int = KEEP) -> Path:
    """Copy the database into ``folder`` and keep only the newest ``keep`` copies."""

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_PREFIX}{now.astimezone(timezone.utc):%Y%m%d-%H%M%S}.db"
    partial = target.with_suffix(".partial")
    origin = sqlite3.connect(str(database), timeout=5.0)
    copy = sqlite3.connect(str(partial))
    try:
        origin.backup(copy)
    finally:
        copy.close()
        origin.close()
    partial.replace(target)
    for old in backups(folder)[keep:]:
        old.unlink(missing_ok=True)
    return target
