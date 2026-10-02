"""Small file helpers shared across KnowItAll2."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` exactly as given (line endings included) by atomic replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temporary, path)
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
