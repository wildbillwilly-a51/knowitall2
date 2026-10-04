"""Learning at the moments work happens, and telling the user what was learned.

Learning starts when a turn ends with a commit in it, when a session ends, or
when an agent asks (because the user asked, or a piece of work finished).
Each start is a *request* for one session log; the learner handles requests
first, then writes one *news* entry per session saying what it learned, what
it turned down, or why it could not learn yet. Agent hooks show that news to
the user in the session itself, and the app shows it on the Learning page.

This module is read by agent hooks on every turn, so it stays small: plain
files under ``learner/`` in the data home, no database, no model calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ..files import read_text, write_text_atomic
from ..paths import data_home

NEWS_KEPT = 40
SHOWN_ITEMS = 5
ITEM_CHARACTERS = 160
# A request older than this is dropped instead of learned: its session is
# picked up by the regular pass anyway, and the user no longer waits for it.
REQUEST_DAYS = 2

REASONS = ("commit", "session end", "asked", "finished", "catch-up")
# A look for commits at more of a log than this notes it as read first.
LOOK_AHEAD_BYTES = 1024 * 1024
# Where each session's log was found last, by place and agent: newest_session_log looks there first.
FOUND_LOGS_FILE = "found-logs.json"
FOUND_LOGS_KEPT = 100

# A command that makes a commit: ``git commit`` (also with ``-C dir`` or
# ``-c name=value`` first), ``git cherry-pick``, or ``git revert``.
_GIT_COMMIT = re.compile(
    r"""\bgit(?:\.exe)?(?:\s+-[cC]\s+(?:"[^"]*"|'[^']*'|\S+))*\s+(?:commit|cherry-pick|revert)\b(?![-\w])"""
)
_NOT_A_COMMIT = re.compile(r"--dry-run|--help|\s-h\b")
# What those print on success: ``[main 1a2b3c4] Subject``, ``[main (root-commit) 1a2b3c4]``,
# ``[detached HEAD 1a2b3c4]``. With ``-q`` they print nothing.
_COMMIT_LINE = re.compile(r"\[(?:detached HEAD|[\w./+-]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] ")
_FAILED = re.compile(r"^\s*(?:Exit code [1-9]|\(exit code [1-9])|nothing to commit|nothing added to commit|"
                     r"^(?:fatal|error): ", re.MULTILINE)
_SHELLS = frozenset({"Bash", "PowerShell", "shell"})


def learner_dir() -> Path:
    return data_home() / "learner"


def key(value: str) -> str:
    """A short, stable file-name key for a path or id."""

    return hashlib.sha1(os.path.normcase(value).encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Commits -------------------------------------------------------------------


def commits_since_last_look(transcript: str | os.PathLike[str], *,
                            first_look_from: Callable[[], int] | None = None) -> list[str]:
    """Commits the session made since the previous look, oldest first: short ids, or "" when none was printed.

    A commit counts when a shell command ran ``git commit`` (or cherry-pick,
    or revert) and did not fail; text that merely quotes commit output does
    not count. Remembers how far it read in ``learner/watch``, so each turn
    reads only what it added. The first look starts at ``first_look_from``:
    the part of the log that was already learned from needs no new learning.
    """

    from .transcripts import read_session

    path = Path(transcript)
    marker = learner_dir() / "watch" / f"{key(str(path))}.json"
    try:
        start = int(json.loads(read_text(marker)).get("offset", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        start = max(0, first_look_from()) if first_look_from else 0
    try:
        size = path.stat().st_size
        if size < start:
            start = 0  # the log was replaced
        if size - start > LOOK_AHEAD_BYTES:
            # A long stretch, as at the first look at a long session: noted as read before reading it, so a
            # hook stopped meanwhile (the agent's time limit) does not try it again every turn. Its commits
            # are learned with the rest of the session anyway.
            try:
                write_text_atomic(marker, json.dumps({"offset": size}) + "\n")
            except OSError:
                pass
        session, end = read_session(path, start=start)
    except OSError:
        return []
    found = []
    for event in session.events:
        if event.kind != "tool" or event.tool not in _SHELLS or not _GIT_COMMIT.search(event.text):
            continue
        output = event.output or ""
        printed = _COMMIT_LINE.search(output)
        if printed:
            # Printed by the commit itself, even if a later command in the same line failed.
            found.append(printed.group(1)[:7])
        elif not _NOT_A_COMMIT.search(event.text) and not _FAILED.search(output):
            found.append("")  # a quiet commit (-q) prints nothing
    if end > start:
        write_text_atomic(marker, json.dumps({"offset": end}) + "\n")
    return found


# Requests ------------------------------------------------------------------


def request(*, transcript: str | None, session_id: str | None, agent: str, cwd: str | None, reason: str,
            detail: str = "", ignore_limit: bool = False) -> dict[str, Any] | None:
    """Ask the learner to learn one session now; returns the request, or None without a log to learn."""

    if not transcript:
        return None
    item = {
        "id": "l-" + uuid.uuid4().hex[:8], "at": now_iso(), "reason": reason, "detail": detail[:120],
        "agent": agent, "session_id": session_id or "", "transcript": str(transcript), "cwd": cwd or "",
        "ignore_limit": bool(ignore_limit),
    }
    write_text_atomic(learner_dir() / "requests" / f"{item['id']}.json", json.dumps(item) + "\n")
    return item


def pending_requests() -> list[dict[str, Any]]:
    """Waiting requests, oldest first; unreadable or stale ones are dropped."""

    folder = learner_dir() / "requests"
    items = []
    try:
        paths = sorted(folder.glob("l-*.json"))
    except OSError:
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - REQUEST_DAYS * 86400
    for path in paths:
        try:
            item = json.loads(read_text(path))
            stale = path.stat().st_mtime < cutoff
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if stale or not isinstance(item, dict) or not item.get("transcript"):
            path.unlink(missing_ok=True)
            continue
        item["_path"] = str(path)
        items.append(item)
    return sorted(items, key=lambda item: str(item.get("at")))


def done_with(items: Iterable[dict[str, Any]]) -> None:
    for item in items:
        if item.get("_path"):
            Path(item["_path"]).unlink(missing_ok=True)


def by_session(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Requests grouped by session log, in the order they first arrived."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(os.path.normcase(str(item["transcript"])), []).append(item)
    return list(groups.values())


def newest_session_log(cwd: str | os.PathLike[str], agent: str | None, *, within_minutes: int = 30) -> Path | None:
    """The log of the session working in ``cwd`` right now: the newest one there, written recently.

    It looks first where that log would be: today's and yesterday's Codex
    date folders, and the folder of the log found last for this place and
    agent (a session asks on every tool call, each in a fresh process, so
    that is kept in a file). Only when those have none does it go through
    every log, as for a session that began days ago.
    """

    from .transcripts import claude_code_listing, codex_listing, folder_listing, recent_codex_listing

    wanted = _same_folder(str(cwd))
    cutoff = datetime.now(timezone.utc).timestamp() - within_minutes * 60
    place = key(f"{agent}\n{wanted}")
    found_before = _found_logs().get(place)
    likely = recent_codex_listing() if agent in ("codex", None) else []
    if isinstance(found_before, str):
        likely += folder_listing(Path(found_before).parent)
    found = _newest_here(sorted(set(likely), reverse=True), wanted, cutoff)
    if found is None:
        listing: list[tuple[float, Path]] = []
        if agent in ("codex", None):
            listing += codex_listing()
        if agent in ("claude-code", None):
            listing += claude_code_listing()
        found = _newest_here(sorted(listing, key=lambda item: item[0], reverse=True), wanted, cutoff)
    if found is not None and str(found) != found_before:
        _remember_found(place, found)
    return found


def _found_logs() -> dict[str, Any]:
    try:
        found = json.loads((learner_dir() / FOUND_LOGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _remember_found(place: str, log: Path) -> None:
    found = {name: value for name, value in _found_logs().items() if name != place}
    found[place] = str(log)
    try:
        write_text_atomic(learner_dir() / FOUND_LOGS_FILE,
                          json.dumps(dict(list(found.items())[-FOUND_LOGS_KEPT:])) + "\n")
    except OSError:
        pass  # it is only a shortcut


def _newest_here(listing: list[tuple[float, Path]], wanted: str, cutoff: float) -> Path | None:
    from .transcripts import session_folder

    for modified, log in listing:  # newest first
        if modified < cutoff:
            return None
        folder = session_folder(log)
        if folder and _same_folder(folder) == wanted:
            return log
    return None


def _same_folder(value: str) -> str:
    return os.path.normcase(os.path.normpath(value)).rstrip("\\/")


# What is happening now -----------------------------------------------------


def set_now(state: dict[str, Any] | None) -> None:
    path = learner_dir() / "now.json"
    if state is None:
        path.unlink(missing_ok=True)
    else:
        write_text_atomic(path, json.dumps({**state, "pid": os.getpid()}) + "\n")


def read_now() -> dict[str, Any] | None:
    """What the learner is doing, or None when it is not running."""

    from .state import RunLock

    try:
        state = json.loads(read_text(learner_dir() / "now.json"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not RunLock().busy():
        return None  # left behind by a run that stopped
    return state


# News ----------------------------------------------------------------------


def add_news(entry: dict[str, Any]) -> dict[str, Any]:
    """Add one entry, newest first.

    A session still waiting for the daily limit is told so once: a new "not
    learned yet" entry replaces an earlier one about the same session that
    has not been shown anywhere.
    """

    entry = {"id": "n-" + uuid.uuid4().hex[:8], "at": now_iso(), **entry}
    items = news()
    if entry.get("status") == "limit" and entry.get("sessions"):
        same = set(entry["sessions"])
        items = [item for item in items if not (
            item.get("status") == "limit" and set(item.get("sessions") or []) == same and not _shown(item["id"], "any"))]
    items.insert(0, entry)
    write_text_atomic(learner_dir() / "news.json", json.dumps({"entries": items[:NEWS_KEPT]}, indent=1) + "\n")
    return entry


def news() -> list[dict[str, Any]]:
    """Recent news, newest first."""

    try:
        data = json.loads(read_text(learner_dir() / "news.json"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return [item for item in entries or [] if isinstance(item, dict)]


def find_news(entry_id: str) -> dict[str, Any] | None:
    return next((item for item in news() if item.get("id") == entry_id), None)


def news_to_show(*, transcript: str | None, session_id: str | None, cwd: str | None) -> list[dict[str, Any]]:
    """News this session has not shown yet, oldest first.

    A session shows the news about itself. News about a session that has
    ended, or about earlier sessions, is shown once, by the next session in
    the same folder (or by any session, for earlier sessions).
    """

    me = _session_key(transcript, session_id)
    if me is None:
        return []
    folder = _same_folder(cwd) if cwd else None
    chosen = []
    for item in reversed(news()):
        if _shown(item["id"], me):
            continue
        mine = me in (item.get("sessions") or [])
        unclaimed = not _shown(item["id"], "any") and (
            item.get("reason") == "catch-up"
            or (folder is not None and folder in [_same_folder(value) for value in item.get("folders") or []])
        )
        if mine or unclaimed:
            chosen.append(item)
    return chosen


def mark_shown(items: Iterable[dict[str, Any]], *, transcript: str | None, session_id: str | None) -> None:
    me = _session_key(transcript, session_id)
    folder = learner_dir() / "shown"
    for item in items:
        for who in (me, "any"):
            if who:
                write_text_atomic(folder / f"{item['id']}.{who}", "")


def first_mention(what: str, *, transcript: str | None, session_id: str | None) -> bool:
    """True the first time a session mentions ``what`` (such as one learning run in progress), then False."""

    me = _session_key(transcript, session_id)
    if me is None:
        return False
    marker = learner_dir() / "shown" / f"{key(what)}.{me}"
    if marker.exists():
        return False
    write_text_atomic(marker, "")
    return True


def session_keys(transcript: str | None, session_id: str | None) -> list[str]:
    """The keys news uses to name a session: by log path, and by id when there is one."""

    return [value for value in (_session_key(transcript, None), _session_key(None, session_id)) if value]


def _session_key(transcript: str | None, session_id: str | None) -> str | None:
    if transcript:
        return "t" + key(str(transcript))
    if session_id:
        return "s" + key(session_id)
    return None


def _shown(entry_id: str, who: str) -> bool:
    return (learner_dir() / "shown" / f"{entry_id}.{who}").exists()


def prune_shown() -> None:
    """Forget shown-markers for news that is gone."""

    kept = {item.get("id") for item in news()}
    folder = learner_dir() / "shown"
    try:
        for path in folder.iterdir():
            if path.name.split(".", 1)[0] not in kept:
                path.unlink(missing_ok=True)
    except OSError:
        pass


# Plain words ---------------------------------------------------------------


def why(reason: str, detail: str = "") -> str:
    """Why learning started, as the end of a sentence."""

    if reason == "commit":
        return f"after your commit {detail}" if detail else "after your commit"
    if reason == "session end":
        return "when the session ended"
    if reason == "asked":
        return "because you asked"
    if reason == "finished":
        return "when the work finished"
    return ""  # catch-up: the sessions themselves say why ("that ended without being learned")


def started_message(reason: str, detail: str = "") -> str:
    return f"KnowItAll2 is learning from this session ({why(reason, detail)}). What it learns will show here."


def describe(entry: dict[str, Any], *, here: bool = True) -> str:
    """One news entry, as the user reads it in a session."""

    status = entry.get("status")
    reason, detail = str(entry.get("reason") or ""), str(entry.get("detail") or "")
    what = _what(entry, here)
    because = f" ({why(reason, detail)})" if why(reason, detail) else ""
    if status == "limit":
        limit = entry.get("limit")
        used = f"today's limit of {limit} learning calls is used up" if limit else "today's learning limit is used up"
        return (f"KnowItAll2 has not learned from {what} yet: {used}. "
                "To learn it now, open KnowItAll2, then Learning, then Learn anyway.")
    if status == "stopped":
        return f"KnowItAll2 could not learn from {what}: {entry.get('problem') or 'the model was not available'}. It will try again later."
    if status == "off":
        return f"KnowItAll2 did not learn from {what}: learning is turned off."
    saved = entry.get("saved") or []
    updated = entry.get("updated") or []
    lines = []
    if saved or updated:
        count = len(saved) + len(updated)
        lines.append(f"KnowItAll2 learned {count} {'thing' if count == 1 else 'things'} from {what}{because}:")
        shown = [*saved, *updated][:SHOWN_ITEMS]
        lines += [f"  - {_short(item.get('text'))}" for item in shown]
        if count > len(shown):
            lines.append(f"  (and {count - len(shown)} more; see KnowItAll2, Learning)")
    else:
        lines.append(f"KnowItAll2 checked {what}{because}: nothing new to remember.")
    extras = []
    if entry.get("already_known"):
        extras.append(f"{entry['already_known']} already known")
    turned_down = entry.get("turned_down") or []
    if turned_down:
        extras.append(f"{len(turned_down)} turned down")
    if entry.get("questions"):
        extras.append(f"{entry['questions']} with a question for you")
    if extras:
        lines.append("  (" + ", ".join(extras) + ")")
    if entry.get("partial"):
        rest = ("waits for today's learning limit" if entry.get("reason") == "catch-up"
                else "is learned at its next commit or end, or when you ask")
        lines.append(f"  The rest of this session {rest}.")
    return "\n".join(lines)


def _what(entry: dict[str, Any], here: bool) -> str:
    if entry.get("reason") == "catch-up":
        count = len(entry.get("sessions_learned") or []) or 1
        return ("an earlier session" if count == 1 else f"{count} earlier sessions") + " that ended without being learned"
    return "this session" if here else "your last session here"


def _short(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= ITEM_CHARACTERS else value[: ITEM_CHARACTERS - 3].rstrip() + "..."
