"""Small file helpers shared across KnowItAll2."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, TypeVar

# On Windows a file cannot be replaced while another process has it open, and cannot be
# opened while it is being replaced; either side waits about this long for the other.
BUSY_SECONDS = 1.0

_T = TypeVar("_T")


def _while_busy(action: Callable[[], _T]) -> _T:
    """``action``, tried again with short pauses while Windows says the file is in use."""

    deadline = time.monotonic() + BUSY_SECONDS
    pause = 0.005
    while True:
        try:
            return action()
        except PermissionError:
            if sys.platform != "win32" or time.monotonic() >= deadline:
                raise
        time.sleep(pause)
        pause = min(pause * 2, 0.1)


def read_text(path: Path) -> str:
    """The text of a file another process may be replacing, or that the user saved with a byte order mark."""

    return _while_busy(lambda: Path(path).read_text(encoding="utf-8-sig"))


def write_text_atomic(path: Path, text: str, *, follow_links: bool = True) -> None:
    """Write ``text`` exactly as given (line endings included) by atomic replacement.

    A symbolic link stays a link: the file it points to is written. With
    ``follow_links`` off, the link itself is replaced by the file instead, for
    places another party controls, such as a repository. On POSIX a file keeps
    its mode; a new one is readable only by the user.

    The text is on the disk before it replaces the file: a crash after a
    replace whose text was still in memory left seven files as zeros
    (2026-10-05, release-candidate review R-M3).
    """

    if follow_links and path.is_symlink():
        path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if sys.platform != "win32":
            try:
                current = os.stat(path) if follow_links else os.lstat(path)
                if stat.S_ISREG(current.st_mode):
                    os.chmod(temporary, stat.S_IMODE(current.st_mode))
            except FileNotFoundError:
                pass
        _while_busy(lambda: os.replace(temporary, path))
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    if sys.platform != "win32":   # and the replace itself, which POSIX keeps in the folder
        try:
            folder = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        except OSError:
            pass


def short_path(value: str) -> str | None:
    """Windows' short form of an existing path, or None where short names are unavailable."""

    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        size = get_short(value, None, 0)
        if not size:
            return None
        buffer = ctypes.create_unicode_buffer(size)
        return buffer.value if get_short(value, buffer, size) else None
    except (AttributeError, OSError):
        return None
