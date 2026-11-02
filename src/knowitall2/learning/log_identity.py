"""One transcript identity across Windows path spellings, without cursor arithmetic."""

from __future__ import annotations

import ntpath
import os
from pathlib import Path


def path_key(value: str | os.PathLike[str]) -> str:
    """Cheap lexical identity; Windows extended drive and UNC paths stay openable."""
    name = os.fspath(value)
    if name[:8].casefold() == "\\\\?\\unc\\":
        name = "\\\\" + name[8:]
    elif name.startswith("\\\\?\\") and len(name) >= 6 and name[5] == ":":
        name = name[4:]
    if os.name == "nt" or ntpath.splitdrive(name)[0] or name.startswith("\\\\"):
        return ntpath.normcase(ntpath.abspath(name) if os.name == "nt" else ntpath.normpath(name))
    return os.path.normcase(os.path.abspath(name))


def same_log(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    if path_key(left) == path_key(right):
        return True
    if path_key(os.path.realpath(left)) == path_key(os.path.realpath(right)):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def unique_logs(logs: list[Path]) -> list[Path]:
    """Normal spelling first; avoid repeatedly learning a known physical alias."""
    found: list[Path] = []
    keys: set[str] = set()
    physical: set[tuple[int, int]] = set()
    for log in sorted(logs, key=lambda p: str(p).startswith("\\\\?\\")):
        key = path_key(log)
        if key in keys:
            continue
        try:
            status = log.stat()
            identity = (status.st_dev, status.st_ino)
            if status.st_ino and identity in physical:
                continue
            if status.st_ino:
                physical.add(identity)
        except OSError:
            pass
        found.append(log)
        keys.add(key)
    return found
