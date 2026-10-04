"""KnowItAll2's block in an agent's global instructions (Claude Code's CLAUDE.md, Codex's AGENTS.md).

Agents read their global instructions at the start of every chat, before any
project file, so this is where KnowItAll2 becomes part of how an agent orients
itself: read the briefing, and look things up before working them out again.

KnowItAll2 changes only the text between its own markers and the blank lines
before them, and re-reads the file just before writing it. The rest of the
file, its line endings, and a byte-order mark are kept; a file that held only
the block is removed with it.
"""

from __future__ import annotations

from pathlib import Path

from ..files import write_text_atomic
from .base import AgentError, Check

BEGIN = "<!-- BEGIN knowitall2 (managed by KnowItAll2 setup; removed by `knowitall2 uninstall`) -->"
END = "<!-- END knowitall2 -->"
TEXT = """## KnowItAll2 memory

KnowItAll2 (the `knowitall2` tools) is this user's long-term memory for coding work. Use it as a source of
knowledge alongside the project's own files, the way you would search before digging:

- When you orient yourself on a task, read the KnowItAll2 briefing at the start of the chat, including what it
  lists as known for the project; if the chat has none, call `briefing`.
- Before you work out something that may have been found before (a host or service, how to reach or sign in to
  a system, a procedure, a tool's quirk, a past decision), call `recall` with a few keywords first.
- When you or the user establish something durable, save it with `remember`. Never save secrets; save where a
  credential is kept instead.
- If KnowItAll2 is not available, continue normally."""
_BOM = "\ufeff"
_WRITE_ATTEMPTS = 3


def render(newline: str = "\n") -> str:
    return newline.join([BEGIN, *TEXT.split("\n"), END])


def state(path: Path) -> str:
    """``current``, ``outdated``, ``missing``, or ``damaged`` (unreadable, or its markers are out of order)."""

    try:
        text = _read(path)
        span = _span(text, path) if text else None
    except AgentError:
        return "damaged"
    if span is None:
        return "missing"
    newline = "\r\n" if "\r\n" in text else "\n"
    return "current" if text[span[0]:span[1]] == render(newline) else "outdated"


def ensure_editable(path: Path) -> None:
    """Fail before any change if the file cannot be read or KnowItAll2's markers in it are damaged."""

    _span(_read(path), path)


def install(path: Path) -> str | None:
    """Add or refresh the block; returns a description of the change, or None when it was current."""

    for _ in range(_WRITE_ATTEMPTS):
        text = _read(path)
        bom = _BOM if text.startswith(_BOM) else ""
        body = text[len(bom):]
        newline = "\r\n" if "\r\n" in body else "\n"
        block = render(newline)
        span = _span(body, path)
        if span is None:
            if not body.strip():
                updated = block + newline
            else:
                separator = "" if body.endswith(newline * 2) else (newline if body.endswith(newline) else newline * 2)
                updated = body + separator + block + newline
        else:
            if body[span[0]:span[1]] == block:
                return None
            updated = body[: span[0]] + block + body[span[1]:]
        if _replace_if_unchanged(path, text, bom + updated):
            return (f"added KnowItAll2's instructions to {path}" if span is None
                    else f"updated KnowItAll2's instructions in {path}")
    raise AgentError(f"{path} kept changing while setup ran; run setup again.")


def remove(path: Path) -> str | None:
    for _ in range(_WRITE_ATTEMPTS):
        if not path.is_file():
            return None
        text = _read(path)
        bom = _BOM if text.startswith(_BOM) else ""
        body = text[len(bom):]
        span = _span(body, path)
        if span is None:
            return None
        newline = "\r\n" if "\r\n" in body else "\n"
        before, after = body[: span[0]], body[span[1]:]
        if after.startswith(newline):
            after = after[len(newline):]
        while before.endswith(newline * 2):
            before = before[: -len(newline)]
        updated = before + after
        if not updated.strip():
            if _replace_if_unchanged(path, text, None):
                return f"removed {path}, which held only KnowItAll2's instructions"
        elif _replace_if_unchanged(path, text, bom + updated):
            return f"removed KnowItAll2's instructions from {path}"
    raise AgentError(f"{path} kept changing; run uninstall again.")


def check(display_name: str, path: Path, fix: str) -> Check:
    name = f"{display_name} instructions"
    current = state(path)
    if current == "current":
        return Check(name, True, f"current in {path}")
    if current == "damaged":
        return Check(name, False, f"{path} cannot be read, or KnowItAll2's markers in it are out of order",
                     f"Fix or remove the lines between {BEGIN} and {END} in {path}, then run: {fix.removeprefix('Run: ')}")
    return Check(name, False, f"KnowItAll2's instructions are {current} in {path}", fix)


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentError(f"cannot read {path}: {exc}") from exc


def _replace_if_unchanged(path: Path, original: str, text: str | None) -> bool:
    """Write ``text`` (or remove the file, for None) unless the file changed since it was read as ``original``.

    Agents and editors may save the file at any time; reading it again just
    before writing keeps their change from being overwritten.
    """

    if _read(path) != original:
        return False
    if text is None:
        path.unlink()
    else:
        write_text_atomic(path, text)
    return True


def _span(text: str, path: Path) -> tuple[int, int] | None:
    """Where the block is: from its first marker to the end of its last; None when there is none."""

    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        return None
    start = text.find(BEGIN)
    end = text.find(END)
    if begins != 1 or ends != 1 or end < start:
        raise AgentError(
            f"{path} has KnowItAll2's instruction markers out of order or more than once; "
            "fix or remove them, then run setup again.")
    return start, end + len(END)
