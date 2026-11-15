"""Where a Codex transcript is now, and its bytes, whatever Codex has done with the file.

Codex compresses a quiet chat's rollout in place (``rollout-*.jsonl`` becomes ``rollout-*.jsonl.zst`` about a
week after the chat went quiet) and moves a chat the user archives to ``~/.codex/archived_sessions`` (one flat
folder), compressed or not. On 2026-11-13 Codex chats that KnowItAll2 had not yet learned were lost to learning
that way: the reader only knew plain files under ``sessions``.

A transcript keeps the one identity it always had: its plain spelling under ``sessions/YYYY/MM/DD``, the key of
its learning progress. This module finds whichever form exists now and reads it, so offsets stay in the bytes of
the original transcript (checked 2026-11-13: each frame declares its decoded size, which equalled the size
recorded before compression, with every recorded offset on a line boundary).

Reading ``.zst`` needs a zstd decoder: Python 3.14's ``compression.zstd``, the ``zstandard`` module, the ``zstd``
program, or Node.js with ``zlib.zstdDecompressSync`` (22.15 and later), the first that works. KnowItAll2 itself
depends on none of them. Without one, a compressed transcript raises ``UnreadableLog``: learning leaves it waiting
with its progress kept, never reads it as empty, and doctor says what is missing.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

COMPRESSED = ".zst"
SESSIONS = "sessions"
ARCHIVE = "archived_sessions"
_ROLLOUT = re.compile(r"^rollout-(\d{4})-(\d{2})-(\d{2})T.*\.jsonl(?:\.zst)?$")
_ZSTD_MAGIC = 0xFD2FB528
# A decoder that runs as another program gets this long; the largest rollout seen decoded to about 256 MB.
DECODE_SECONDS = 600


class UnreadableLog(OSError):
    """A transcript exists but cannot be read now: no zstd decoder, or a damaged frame. Its progress is kept."""


@dataclass(frozen=True)
class LogStatus:
    """What learning needs of a transcript's file: its size in transcript bytes and when it was last written."""

    st_size: int
    st_mtime: float
    physical: Path
    compressed: bool


def is_rollout(name: str) -> bool:
    """A Codex transcript file in any form Codex keeps it."""

    return name.startswith("rollout-") and (name.endswith(".jsonl") or name.endswith(".jsonl" + COMPRESSED))


def canonical(path: Path) -> Path:
    """The plain ``sessions/YYYY/MM/DD`` spelling of a Codex transcript, its identity; other paths unchanged."""

    name = path.name
    if not is_rollout(name):
        return path
    plain = name[:-len(COMPRESSED)] if name.endswith(COMPRESSED) else name
    if path.parent.name.casefold() == ARCHIVE:
        match = _ROLLOUT.match(plain)
        if match:
            return path.parent.parent.joinpath(SESSIONS, *match.groups(), plain)
    return path.with_name(plain)


def forms(path: Path) -> list[Path]:
    """Where a Codex transcript may be, in order: plain, compressed in place, archived plain, archived compressed."""

    plain = canonical(path)
    found = [plain, plain.with_name(plain.name + COMPRESSED)]
    parents = plain.parents
    if len(parents) > 4 and parents[3].name.casefold() == SESSIONS:
        archive = parents[4] / ARCHIVE
        found += [archive / plain.name, archive / (plain.name + COMPRESSED)]
    return found


def home_of(path: Path) -> str | None:
    """The Codex home a transcript belongs to (the folder holding ``sessions``), as a comparable key."""

    from .log_identity import path_key

    plain = canonical(path)
    parents = plain.parents
    if len(parents) > 4 and parents[3].name.casefold() == SESSIONS:
        return path_key(parents[4])   # the same spelling rules as every other identity comparison
    return None


def physical(path: Path) -> Path | None:
    """The file a transcript is in now, or None when it is gone."""

    if not is_rollout(path.name):
        return path if path.is_file() else None
    for candidate in forms(path):
        if candidate.is_file():
            return candidate
    return None


def exists(path: str | os.PathLike[str]) -> bool:
    return physical(Path(path)) is not None


def status(path: Path) -> LogStatus:
    """A transcript's size in its own (decoded) bytes and its file's time, from whichever form exists now."""

    for _ in range(2):  # Codex may compress or archive it between finding and reading
        found = physical(path)
        if found is None:
            raise FileNotFoundError(str(path))
        try:
            stat = found.stat()
        except FileNotFoundError:
            continue
        if not found.name.endswith(COMPRESSED):
            return LogStatus(stat.st_size, stat.st_mtime, found, False)
        size = frame_size(found)
        if size is None:
            size = len(read_bytes(found))
        return LogStatus(size, stat.st_mtime, found, True)
    raise FileNotFoundError(str(path))


def open_log(path: Path) -> BinaryIO:
    """The transcript's bytes from the start, whichever form it is in now."""

    for _ in range(2):
        found = physical(path)
        if found is None:
            raise FileNotFoundError(str(path))
        try:
            if found.name.endswith(COMPRESSED):
                return io.BytesIO(read_bytes(found))
            return found.open("rb")
        except FileNotFoundError:
            continue
    raise FileNotFoundError(str(path))


def is_compressed(path: Path) -> bool:
    found = physical(path)
    return found is not None and found.name.endswith(COMPRESSED)


# Decoding --------------------------------------------------------------------------------------------------------

_cache: tuple[tuple[str, int, int], bytes] | None = None


def frame_size(path: Path) -> int | None:
    """The decoded size a zstd frame declares (Frame_Content_Size), or None when it does not say."""

    with path.open("rb") as stream:
        head = stream.read(18)
    if len(head) < 6 or struct.unpack("<I", head[:4])[0] != _ZSTD_MAGIC:
        raise UnreadableLog(f"not a zstd frame: {path}")
    descriptor = head[4]
    size_flag, single, dictionary = descriptor >> 6, (descriptor >> 5) & 1, descriptor & 3
    at = 5 + (0 if single else 1) + (0, 1, 2, 4)[dictionary]
    width = (1 if single else 0, 2, 4, 8)[size_flag]
    if not width or len(head) < at + width:
        return None
    value = int.from_bytes(head[at:at + width], "little")
    return value + 256 if width == 2 else value


def read_bytes(path: Path) -> bytes:
    """The decoded bytes of a compressed transcript; the last one read is kept for the next read of it."""

    global _cache
    stat = path.stat()
    key = (os.path.normcase(str(path)), stat.st_size, stat.st_mtime_ns)
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    declared = frame_size(path)
    use = decoder()
    if use is None:
        raise UnreadableLog(f"no zstd decoder is available to read {path.name} ({DECODER_HINT})")
    _cache = None   # the last transcript's bytes are let go before the next is decoded: one large log at a time
    try:
        data = use[1](path)
    except UnreadableLog:
        raise
    except Exception as exc:  # each decoder has its own error types (zstandard.ZstdError, compression.zstd.ZstdError)
        raise UnreadableLog(f"{use[0]} could not decode {path.name}: {exc}") from exc
    if declared is not None and len(data) != declared:
        raise UnreadableLog(f"{use[0]} decoded {len(data)} bytes of {path.name}, which declares {declared}")
    _cache = (key, data)
    return data


DECODER_HINT = ("install the zstd program, or Python 3.14 or later, or Node.js 22.15 or later, "
                "or run pip install zstandard for the Python that runs KnowItAll2")
_decoder: tuple[str, Callable[[Path], bytes]] | None | bool = False


def decoder() -> tuple[str, Callable[[Path], bytes]] | None:
    """The first zstd decoder that works here, as (its name, a function from a file to its decoded bytes)."""

    global _decoder
    if _decoder is False:
        _decoder = _find_decoder()
    return _decoder  # type: ignore[return-value]


def reset_decoder() -> None:
    """Look for a decoder again (tests, or after one was installed)."""

    global _decoder, _cache
    _decoder, _cache = False, None


def _find_decoder() -> tuple[str, Callable[[Path], bytes]] | None:
    try:
        from compression import zstd as stdlib_zstd  # Python 3.14+

        return "Python compression.zstd", lambda path: stdlib_zstd.decompress(path.read_bytes())
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore[import-not-found]

        def with_zstandard(path: Path) -> bytes:
            with path.open("rb") as source:
                return zstandard.ZstdDecompressor().stream_reader(source).read()

        return "zstandard", with_zstandard
    except ImportError:
        pass
    program = shutil.which("zstd")
    if program and _works([program, "--version"]):
        return "zstd", lambda path: _run([program, "-d", "-c", "-q", "--", str(path)])
    node = shutil.which("node")
    if node and _works([node, "-e", "process.exit(typeof require('zlib').zstdDecompressSync === 'function' ? 0 : 3)"]):
        script = ("const z=require('zlib'),fs=require('fs');"
                  "process.stdout.write(z.zstdDecompressSync(fs.readFileSync(process.argv[1])));")
        return "Node.js", lambda path: _run([node, "-e", script, str(path)])
    return None


def _works(command: list[str]) -> bool:
    try:
        return subprocess.run(command, capture_output=True, timeout=30, shell=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run(command: list[str]) -> bytes:
    done = subprocess.run(command, capture_output=True, timeout=DECODE_SECONDS, shell=False)
    if done.returncode != 0:
        raise UnreadableLog(done.stderr.decode("utf-8", "replace").strip()[-300:] or f"exit code {done.returncode}")
    return done.stdout
