"""Locate the KnowItAll2 data home."""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENVIRONMENT_VARIABLE = "KNOWITALL2_HOME"
DATABASE_NAME = "knowitall2.db"
_PACKAGE_STORAGE_MARKER = os.path.join("appdata", "local", "packages", "")


def data_home() -> Path:
    """Return the folder that holds all KnowItAll2 state for this user.

    The default is ``~/.knowitall2`` on every platform, beside ``~/.codex`` and
    ``~/.claude``. ``KNOWITALL2_HOME`` overrides it. On Windows, files that a
    packaged desktop app (such as the Claude app) creates under ``AppData``
    are silently redirected into that app's private storage, which other
    programs cannot see and which is deleted with the app. The user profile
    is not redirected, so memories stay shared and durable.
    """

    configured = os.environ.get(HOME_ENVIRONMENT_VARIABLE)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".knowitall2"


def database_path() -> Path:
    return data_home() / DATABASE_NAME


def in_package_storage(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` really lives in a Windows app package's private storage."""

    return _PACKAGE_STORAGE_MARKER in os.path.normcase(os.path.realpath(path)) + os.sep
