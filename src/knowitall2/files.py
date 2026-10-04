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


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` exactly as given (line endings included) by atomic replacement.

    A symbolic link stays a link: the file it points to is written. On POSIX a
    file keeps its mode; a new one is readable only by the user.
    """

    if path.is_symlink():
        path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        if sys.platform != "win32":
            try:
                os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
            except FileNotFoundError:
                pass
        _while_busy(lambda: os.replace(temporary, path))
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


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
