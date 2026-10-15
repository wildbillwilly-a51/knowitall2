"""What KnowItAll2 knows, as files in each project, so agents reach it with their own tools.

Active memories are written to ``knowitall2-known/`` at the root of each project folder agents work in:
one file per catalogued system or project, newest first, in short lines a search shows whole. A project's
folder holds its own memories and the files of the systems they use, and the general file
(docs/knowledge-files-design.md; narrowed by the user's choice after the review of 2026-10-11).

Git never sees the folder: it is listed in the repository's shared exclude file, which every worktree
reads and no commit carries. Search tools do: ``rg`` and Claude Code's Grep skip what Git ignores, so a
root ``.ignore`` file allows the folder again. A folder or ``.ignore`` that KnowItAll2 did not write is
left alone: KnowItAll2 keeps a record of the folders it wrote in the data home (``known.json``).

Each computer writes the files from its own copy of the memory, after a change, in a detached process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Sequence

from . import commands, journal
from .files import read_text, write_text_atomic
from .identity import repository_dirs
from .paths import data_home, database_path

if TYPE_CHECKING:  # hooks import this module; SQLite and the secret screen load only when needed
    import sqlite3

FOLDER = "knowitall2-known"
INDEX = "README.md"
GENERAL = "General"
LINE_WIDTH = 160
# The first line of the index: a folder without it, and missing from KnowItAll2's record of the folders it
# wrote, was not written by KnowItAll2 and is never touched.
MARKER = "<!-- Written by KnowItAll2. Do not edit: change memories with KnowItAll2's remember and forget. -->"
IGNORE_FILE = ".ignore"
IGNORE_LINE = f"!{FOLDER}/"
IGNORE_TEXT = (f"# Written by KnowItAll2: lets search tools see {FOLDER}/, which Git ignores.\n"
               f"# Removed by `knowitall2 known --off` or uninstalling KnowItAll2.\n{IGNORE_LINE}\n")
EXCLUDE_BEGIN = f"# BEGIN KnowItAll2: {FOLDER}/ and its .ignore stay out of Git"
EXCLUDE_END = "# END KnowItAll2"
EXCLUDE_LINES = (f"/{FOLDER}/", f"/{IGNORE_FILE}")
ENVIRONMENT_SWITCH = "KNOWITALL2_KNOWN_FILES"
STATUS_FILE = "known.json"
LOCK_FILE = "known.lock"
LOCK_STALE_SECONDS = 600
# Names Windows refuses as file names, whatever follows the first dot (``aux.lan.md`` as well as ``aux.md``).
_RESERVED = {"con", "prn", "aux", "nul", *(f"{device}{number}" for device in ("com", "lpt") for number in "0123456789¹²³")}


# --- What the files say --------------------------------------------------------------------------

def file_name(name: str) -> str:
    """The file for a system or project name: case-insensitive, safe on every platform, never the index."""

    slug = re.sub(r"[^\w.-]+", "-", name.casefold()).strip("-.") or "unnamed"
    first, dot, rest = slug.partition(".")
    if first in _RESERVED:
        slug = f"{first}_{dot}{rest}"
    elif slug == Path(INDEX).stem.casefold():
        slug += "-"
    return slug + ".md"


def render(connection: sqlite3.Connection) -> dict[str, str]:
    """Every file there is, by name: one per system or project, and the index of them all."""

    return render_files(connection).everything()


@dataclass
class Rendered:
    files: dict[str, str]        # each system's or project's file, by name, in the index's order
    lines: dict[str, str]        # each file's line in the index
    uses: dict[str, set[str]]    # each project's files: its own, and those of the systems its memories use
    generic: set[tuple[str, ...]] = field(default_factory=set)   # names most memories use in passing (``GENERIC_SHARE``)
    tools: set[str] = field(default_factory=set)                 # the files of the agent's own tools
    systems: set[str] = field(default_factory=set)               # the files of catalogued systems
    counts: dict[str, int] = field(default_factory=dict)         # each file's own memories

    def everything(self) -> dict[str, str]:
        return {**self.files, INDEX: _index(list(self.lines.values()))}

    def folder(self, projects: Sequence[str], extra: Sequence[str] = ()) -> dict[str, str]:
        """The files of a folder of ``projects``: their own, those of the systems their memories are filed
        under or name, and the general file; the index lists only those (review 2026-10-11, the user's choice).

        ``extra``: the files of what else the projects depend on (``dependencies``).
        """

        chosen = {file_name(GENERAL)}.union(*(self.uses.get(project_id, set()) for project_id in projects), extra)
        files = {name: text for name, text in self.files.items() if name in chosen}
        return {**files, INDEX: _index([self.lines[name] for name in files])}


def render_files(connection: sqlite3.Connection) -> Rendered:
    """Every file there is, with the files each project's folder gets."""

    from . import secrets
    from .memory import PROVISIONAL_LABEL, PROVISIONAL_TAG

    from .catalog import workstation_tool

    catalogued = list(connection.execute("SELECT id, name, aliases, kind FROM systems"))
    systems = {row[0]: (row[1], _aliases(row[2])) for row in catalogued}
    tools = {file_name(row[1]) for row in catalogued if workstation_tool(row[1], row[3], _aliases(row[2]))}
    projects = dict(connection.execute("SELECT id, name FROM projects"))
    notes = dict(connection.execute("SELECT record_id, system_id FROM record_notes WHERE system_id IS NOT NULL"))
    facets = dict(connection.execute("SELECT record_id, facet FROM record_notes"))
    table = name_table(connection)
    system_files = {file_name(name) for name, _ in systems.values()}
    uses: dict[str, set[str]] = {project_id: {file_name(str(name))} for project_id, name in projects.items()}
    groups: dict[str, dict[str, Any]] = {}
    # A project that is itself a system (saltbox) uses what that system's shared memories name, too.
    as_system: dict[str, list[str]] = {}
    for project_id, name in projects.items():
        as_system.setdefault(file_name(str(name)), []).append(project_id)
    shared_names: list[tuple[list[str], dict[str, set[tuple[str, ...]]]]] = []
    # How specific each name is (``GENERIC_SHARE``): the memories that use it, and those of them filed in its file.
    using: dict[tuple[str, ...], int] = {}
    filed_there: dict[tuple[str, ...], int] = {}
    # The files a memory names besides its own, each with the names it used, and its entry there.
    elsewhere: list[tuple[dict[str, set[tuple[str, ...]]], str]] = []
    rows = connection.execute(
        "SELECT id, kind, text, scope, project_id, verification, created_at, tags FROM records "
        "WHERE status = 'active' ORDER BY created_at DESC, seq DESC"
    )
    for record_id, kind, text, scope, project_id, verification, created_at, tags in rows:
        system = systems.get(notes.get(record_id))
        if system is not None:
            name, aliases = system
        elif scope == "project" and projects.get(project_id):
            name, aliases = projects[project_id], []
        else:
            name, aliases = GENERAL, []
        group = groups.setdefault(file_name(name), {"names": [], "aliases": [], "entries": [], "elsewhere": [],
                                                    "sections": {key: [] for key, _ in SECTIONS}})
        if name not in group["names"]:
            group["names"].append(name)
        group["aliases"].extend(alias for alias in aliases if alias not in group["aliases"] and alias != name)
        where = f", project {projects[project_id]}" if scope == "project" and projects.get(project_id) else ""
        unverified = " (unverified: check before relying on it)" if verification == "unverified" else ""
        if f'"{PROVISIONAL_TAG}"' in str(tags or ""):
            unverified += f" ({PROVISIONAL_LABEL})"
        body = secrets.redact(" ".join(str(text).split()))
        saved = f"[{record_id}] {kind}, saved {str(created_at)[:10]}{where}{unverified}"
        group["entries"].append(_wrap(f"- {saved}: {body}"))
        group["sections"][_section(facets.get(record_id), kind)].append(group["entries"][-1])
        home = file_name(name)
        by_names: dict[str, set[tuple[str, ...]]] = {}
        for key, target in _named_keys(str(text), table):
            by_names.setdefault(target, set()).add(key)
        for key in {key for keys in by_names.values() for key in keys}:
            using[key] = using.get(key, 0) + 1
            if table[key] == home:
                filed_there[key] = filed_there.get(key, 0) + 1
        others = {target: keys for target, keys in by_names.items() if target != home}
        if others:
            elsewhere.append((others, _wrap(f"- {saved}, filed in {home}: {body}")))
        if scope == "project" and project_id:
            uses.setdefault(project_id, set()).update([home, *(target for target in others if target in system_files)])
        elif system is not None and home in as_system and others:
            shared_names.append((as_system[home], others))
    generic = {key for key, count in using.items()
               if count >= GENERIC_MINIMUM_USES and filed_there.get(key, 0) < GENERIC_SHARE * count}
    for others, entry in elsewhere:   # only into files that exist: a name alone makes no file
        for target, keys in others.items():
            if target in groups and not keys <= generic:   # not when only a name used in passing names it
                groups[target]["elsewhere"].append(entry)
    for project_ids, others in shared_names:
        for target, keys in others.items():
            if target in system_files and target not in tools:
                for project_id in project_ids:
                    uses.setdefault(project_id, set()).add(target)
    files: dict[str, str] = {}
    index_lines: dict[str, str] = {}
    for name in sorted(groups, key=lambda item: (" / ".join(groups[item]["names"]).casefold(), item)):
        group = groups[name]
        title = " / ".join(group["names"])
        head = [f"# {title}"]
        if group["aliases"]:
            head.append(f"Also called: {', '.join(group['aliases'])}")
        head.append(f"{len(group['entries'])} memories, newest first.")
        if group["elsewhere"]:
            head.append(f"{len(group['elsewhere'])} more, filed in other files, name it; they are listed last.")
        filled = [(title, group["sections"][key]) for key, title in SECTIONS if group["sections"][key]]
        if group["elsewhere"]:
            filled.append((ELSEWHERE, group["elsewhere"]))
        if len(filled) == 1:
            body = "\n".join(filled[0][1])
        else:   # how to reach it and the how-tos first, so even a partial read gets them
            body = "\n\n".join(f"## {title}\n\n" + "\n".join(entries) for title, entries in filled)
        files[name] = "\n".join(head) + "\n\n" + body + "\n"
        called = f" (also {', '.join(group['aliases'][:4])})" if group["aliases"] else ""
        more = f", and {len(group['elsewhere'])} filed elsewhere that name it" if group["elsewhere"] else ""
        index_lines[name] = f"- {name}: {title}{called}, {len(group['entries'])} memories{more}"
    counts = {name: len(group["entries"]) for name, group in groups.items()}
    return Rendered(files, index_lines, uses, generic, tools, system_files, counts)


def _index(lines: Sequence[str]) -> str:
    return "\n".join([
        MARKER,
        "# What KnowItAll2 knows",
        "",
        "What KnowItAll2 remembers about this project and the systems it uses, from coding sessions on this",
        "computer and the computers it shares memory with: one file per system or project, newest first.",
        "Search it like any project file; KnowItAll2's recall finds what is known about other projects. A",
        "memory is filed under what it is mainly about and listed again at the end of the file of each other",
        "system it names, so one system's file has everything that names it. A memory marked unverified is",
        "a lead to check. These files are rewritten whenever memories change; to correct one, use",
        "KnowItAll2's remember or forget, not an edit here.",
        "",
        *lines,
    ]) + "\n"


SECTIONS = (
    ("reach", "How to reach it, and where its sign-in is kept"),
    ("howto", "How-tos"),
    ("rest", "Everything else"),
)
_REACH_FACETS = {"access", "signin", "where"}
ELSEWHERE = "Named in memories filed elsewhere"


def _wrap(entry: str) -> str:
    return "\n  ".join(textwrap.wrap(entry, LINE_WIDTH, break_long_words=False, break_on_hyphens=False))


def _section(facet: str | None, kind: str) -> str:
    if facet in _REACH_FACETS:
        return "reach"
    if facet == "howto" or kind == "procedure":
        return "howto"
    return "rest"


def change_marker(connection: sqlite3.Connection) -> str:
    """Changes whenever anything the files show changes; cheap enough to check at every hook.

    The store counts each such change (``KNOWN_GENERATION``): counts and latest times alone missed an
    update from the server older than the newest memory here, and a second change within the same second.
    """

    from .store import KNOWN_GENERATION

    row = connection.execute(
        "SELECT (SELECT value FROM meta WHERE key = ?), "
        "(SELECT COUNT(*) FROM records WHERE status = 'active'), (SELECT MAX(updated_at) FROM records), "
        "(SELECT MAX(seq) FROM records), (SELECT COUNT(*) FROM record_notes), (SELECT MAX(written_at) FROM record_notes), "
        "(SELECT MAX(updated_at) FROM systems), (SELECT COUNT(*) FROM systems), (SELECT COUNT(*) FROM projects), "
        "(SELECT COUNT(*) FROM project_paths), "
        # What ``commands.json`` holds: the lessons tagged with a command (a tag alone changes nothing above).
        "(SELECT COUNT(*) || ':' || IFNULL(MAX(updated_at), '') FROM records WHERE status = 'active' AND tags LIKE ?)",
        (KNOWN_GENERATION, f"%{commands.COMMAND_TAG}%"),
    ).fetchone()
    return "|".join(str(value) for value in row)


def _aliases(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []


# --- One project folder ---------------------------------------------------------------------------

@dataclass
class FolderResult:
    root: str
    written: int = 0
    removed: int = 0
    skipped: str | None = None      # why nothing was written
    notes: list[str] = field(default_factory=list)
    refused: str | None = None      # when KnowItAll2 must not write there: "git", "tracked", "link", or "foreign"


def git_common_dir(root: Path) -> Path | None:
    """The folder Git keeps a repository's shared files in (``info/exclude``), for a checkout or a worktree.

    None unless Git could use it (``identity.repository_dirs``): a stub ``.git`` or a worktree whose
    repository moved is no repository, and no Git folder is ever made for it.
    """

    found = repository_dirs(root)
    return found[1].resolve() if found else None


def ours(folder: Path) -> bool:
    try:
        return read_text(folder / INDEX).splitlines()[:1] == [MARKER]
    except (OSError, IndexError):
        return False


def write_folder(root: Path, files: dict[str, str], *, recorded: bool = False,
                 claim: Callable[[], object] | None = None) -> FolderResult:
    """Write the files into ``root``'s folder, keep it out of Git, and let search tools see it.

    Nothing in a repository is followed (review 2026-10-11): a folder that is a link or holds anything but
    plain files is left alone, a linked ``.ignore`` is never written, and each file replaces what is at its
    path instead of writing into it. What Git tracks is the repository's own and is never changed.

    ``recorded``: KnowItAll2's record says it wrote this folder before, so it is KnowItAll2's even when a
    crash emptied its index (``_refused``). ``claim`` is called when the folder is made, before anything is
    written into it, to record it so. The index is written last, once the files it lists are in place.
    """

    result = FolderResult(str(root))
    if _in_data_home(root.resolve()):
        result.skipped = "inside KnowItAll2's own data folder, which never gets knowledge files"
        return result
    found = repository_dirs(root)
    if found is None:
        result.skipped = "not a Git working tree"
        return result
    git_dir, common = found
    folder = root / FOLDER
    tracked = _tracked(root, git_dir)
    refusal = _refused(root, folder, tracked, recorded=recorded)
    if refusal is not None or tracked is None:
        result.refused, result.skipped = refusal or ("git", "Git could not be run")
        return result
    ignore = root / IGNORE_FILE
    linked = _linked(ignore)
    create = not linked and not os.path.lexists(ignore) and IGNORE_FILE not in tracked
    own = create or (not linked and IGNORE_FILE not in tracked and ignore.is_file() and read_text(ignore) == IGNORE_TEXT)
    _ensure_excluded(common.resolve() / "info" / "exclude", ignore=own)
    if linked:
        result.notes.append(f"{ignore} is a link, which KnowItAll2 never writes through, so search tools that follow "
                            f"Git's ignore rules may skip {FOLDER}/")
    elif create:
        write_text_atomic(ignore, IGNORE_TEXT, follow_links=False)
    elif not ignore.is_file() or IGNORE_LINE not in read_text(ignore).splitlines():
        result.notes.append(f"{ignore} is not KnowItAll2's, so search tools that follow Git's ignore rules may skip "
                            f"{FOLDER}/; add the line {IGNORE_LINE} to it to let them search it")
    if not os.path.lexists(folder):
        folder.mkdir()
        if claim is not None:
            claim()
    for name, text in files.items():
        if name != INDEX:
            _write_file(folder, name, text, result)
    for path in folder.glob("*.md"):
        if path.name not in files:
            path.unlink(missing_ok=True)
            result.removed += 1
    if INDEX in files:
        _write_file(folder, INDEX, files[INDEX], result)
    return result


def _write_file(folder: Path, name: str, text: str, result: FolderResult) -> None:
    path = folder / name
    try:
        if read_text(path) == text:
            return
    except OSError:
        pass
    try:   # one file Windows refuses must not stop the rest, or the index written last
        write_text_atomic(path, text, follow_links=False)
    except OSError as exc:
        result.notes.append(f"{name} could not be written: {exc}")
        return
    result.written += 1


def _left_by_a_crash(root: Path, folder: Path) -> bool:
    """Whether a crash emptied the index of a folder KnowItAll2 wrote: the index is nothing but zeros (or empty),
    and the ``.ignore`` beside the folder is KnowItAll2's own, word for word. The six live folders of 2026-10-05
    had dropped out of KnowItAll2's record before it kept one."""

    ignore = root / IGNORE_FILE
    try:
        if _linked(ignore) or not ignore.is_file() or read_text(ignore) != IGNORE_TEXT:
            return False
        return not (folder / INDEX).read_bytes().strip(b"\0")
    except OSError:
        return False


def _refused(root: Path, folder: Path, tracked: list[str] | None, *,
             recorded: bool = False) -> tuple[str, str] | None:
    """Why KnowItAll2 must not write ``folder``, as the kind of reason and the reason, or None when it may.

    A folder holding files is KnowItAll2's when its index starts with the marker, or when KnowItAll2's
    record in the data home says it wrote the folder (``recorded``) and it holds nothing but plain ``.md``
    files: a crash on 2026-10-05 left six live indexes as zeros, and those folders were refused from then on
    (release-candidate review, R-M3). Links, and what Git tracks, are refused either way.
    """

    if tracked is None:
        return "git", f"Git could not be run to check that it does not track {FOLDER}/, so nothing was written"
    if any(name.startswith(f"{FOLDER}/") for name in tracked):
        return "tracked", f"Git tracks {FOLDER}/ in this repository, so it is the repository's own; left alone"
    if _linked(folder):
        return "link", f"{folder} is a link, which KnowItAll2 never writes through; left alone"
    if os.path.normcase(folder.resolve().parent) != os.path.normcase(root.resolve()):
        return "link", f"{folder} is not inside the project; left alone"
    if not os.path.lexists(folder):
        return None
    if not folder.is_dir():
        return "foreign", f"{folder} is not a folder; left alone"
    with os.scandir(folder) as scan:
        entries = list(scan)
    if any(entry.is_symlink() or entry.is_junction() for entry in entries):
        return "link", f"{folder} holds a link, which KnowItAll2 never writes through; left alone"
    if any(not entry.is_file(follow_symlinks=False) for entry in entries):
        return "foreign", f"{folder} holds a folder, which KnowItAll2 never writes; left alone"
    markdown = all(os.path.normcase(entry.name).endswith(".md") for entry in entries)
    if entries and not ours(folder) and not ((recorded or _left_by_a_crash(root, folder)) and markdown):
        return "foreign", f"{folder} was not written by KnowItAll2; left alone"
    return None


def _linked(path: Path) -> bool:
    """Whether ``path`` is a symbolic link or a junction, which KnowItAll2 never follows in a repository."""

    return path.is_symlink() or path.is_junction()


def _tracked(root: Path, git_dir: Path) -> list[str] | None:
    """Which paths of the folder and ``.ignore`` Git's index lists in ``root``, or None when Git could not say."""

    if not (git_dir / "index").exists():
        return []   # nothing is tracked before the first commit
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    try:
        done = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", FOLDER, IGNORE_FILE],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60, env=environment,
                              creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return [name for name in done.stdout.decode("utf-8", "replace").split("\0") if name]


def remove_folder(root: Path) -> list[str]:
    """Take out everything KnowItAll2 added to ``root``: its folder, its ``.ignore``, its exclude lines.

    As in ``write_folder``, no link is followed and nothing Git tracks is touched.
    """

    return _remove(root)[0]


def _remove(root: Path, *, recorded: bool = False) -> tuple[list[str], bool]:
    """``remove_folder``'s changes, and whether it is done: the folder and KnowItAll2's ``.ignore`` are gone,
    or were never there, and so are KnowItAll2's exclude lines.

    The exclude lines go last, and only then (release-candidate review, R-M2): while the folder or the
    ``.ignore`` has to stay (Git cannot run, the folder holds a file KnowItAll2 did not write, a removal
    fails), they keep Git from showing what is left, and the cleanup is to be tried again later.
    """

    changes = []
    folder = root / FOLDER
    found = repository_dirs(root)
    tracked = _tracked(root, found[0]) if found else []
    if _refused(root, folder, tracked, recorded=recorded) is None and folder.is_dir():
        for path in sorted(folder.glob("*.md"), key=lambda item: item.name == INDEX):   # its marker last
            path.unlink(missing_ok=True)
        try:
            folder.rmdir()
            changes.append(f"removed {folder}")
        except OSError:
            changes.append(f"emptied {folder} (it holds files KnowItAll2 did not write)")
    ignore = root / IGNORE_FILE
    own = _own_ignore(ignore, tracked)
    if own and not os.path.lexists(folder):
        try:
            ignore.unlink()
            changes.append(f"removed {ignore}")
            own = False
        except OSError:
            pass
    left = [str(path) for path, there in ((folder, os.path.lexists(folder)), (ignore, own is not False)) if there]
    if left:
        changes.append(f"{' and '.join(left)} stayed, so KnowItAll2's exclude lines stay too, to be tried again "
                       "at the next refresh")
        return changes, False
    common = git_common_dir(root)
    if common is not None:
        exclude = common / "info" / "exclude"
    else:   # a .git folder Git cannot use, which KnowItAll2 wrote into before 0.10.6 required a real one
        exclude = root / ".git" / "info" / "exclude"
        if _linked(root / ".git") or not exclude.is_file():
            return changes, True
    if _remove_excluded(exclude):
        changes.append(f"removed KnowItAll2's lines from {exclude}")
    return changes, True


def _own_ignore(ignore: Path, tracked: list[str] | None) -> bool | None:
    """Whether ``ignore`` is KnowItAll2's: a plain file with exactly its text, which Git does not track.
    None when that cannot be told: Git could not say what it tracks, or the file could not be read."""

    if (tracked and IGNORE_FILE in tracked) or _linked(ignore) or not ignore.is_file():
        return False
    try:
        if read_text(ignore) != IGNORE_TEXT:
            return False
    except OSError:
        return None
    return True if tracked is not None else None


def _ensure_excluded(path: Path, *, ignore: bool) -> None:
    """Keep the folder out of Git, and ``.ignore`` only when it is KnowItAll2's: a user's own, not yet
    committed, must still show in ``git status``."""

    try:
        text = read_text(path)
    except FileNotFoundError:
        text = ""
    block = "\n".join([EXCLUDE_BEGIN, *(EXCLUDE_LINES if ignore else EXCLUDE_LINES[:1]), EXCLUDE_END])
    start = text.find(EXCLUDE_BEGIN)
    if start < 0:
        write_text_atomic(path, text + ("" if not text or text.endswith("\n") else "\n") + block + "\n")
        return
    end = text.find(EXCLUDE_END, start)
    end = len(text) if end < 0 else end + len(EXCLUDE_END)
    if text[start:end] != block:
        write_text_atomic(path, text[:start] + block + (text[end:] or "\n"))


def _remove_excluded(path: Path) -> bool:
    try:
        text = read_text(path)
    except OSError:
        return False
    start = text.find(EXCLUDE_BEGIN)
    if start < 0:
        return False
    end = text.find(EXCLUDE_END, start)
    end = len(text) if end < 0 else end + len(EXCLUDE_END)
    write_text_atomic(path, text[:start] + text[end:].lstrip("\n"))
    return True


# --- Every project, kept current ---------------------------------------------------------------------

@dataclass
class RefreshReport:
    marker: str
    folders: list[FolderResult] = field(default_factory=list)
    unchanged: bool = False
    retired: list[tuple[str, list[str]]] = field(default_factory=list)   # folders no longer written, and changes

    def describe(self) -> str:
        if self.unchanged:
            return "The knowledge files are up to date."
        written = [item for item in self.folders if item.skipped is None]
        lines = [f"Wrote the knowledge files in {len(written)} project folders "
                 f"({sum(item.written for item in written)} files changed, {sum(item.removed for item in written)} removed)."]
        for item in self.folders:
            if item.skipped and item.skipped != "not a Git working tree":
                lines.append(f"- {item.root}: {item.skipped}")
            for note in item.notes:
                lines.append(f"- {item.root}: {note}")
        for root, changes in self.retired:
            if changes:
                lines.append(f"- {root}: no longer written here (no agent worked here in {WORKED_DAYS} days, or the "
                             f"project is in use in another copy); {'; '.join(changes)}")
        return "\n".join(lines)


def enabled() -> bool:
    """On unless turned off: ``"known_files": false`` in config.json, or ``KNOWITALL2_KNOWN_FILES=0``."""

    if os.environ.get(ENVIRONMENT_SWITCH, "").strip() in ("0", "false", "off"):
        return False
    try:
        data = json.loads(read_text(data_home() / "config.json"))
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get("known_files") is False)


# --- Where knowledge files could travel further than this computer (the user, 2026-10-12) ----------------

# In config.json: true lets knowledge files be written in folders a cloud service keeps in sync.
SYNCED_SETTING = "known_files_in_synced_folders"


def synced_places(home: Path | None = None) -> list[str]:
    """The folders OneDrive, Dropbox, Google Drive, or iCloud keep in sync on this computer, as keys."""

    places = [os.environ.get(name) for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    places += [str((home or Path.home()) / name) for name in ("Dropbox", "Google Drive", "iCloudDrive")]
    return [_key(Path(place).resolve()) for place in places if place]


def held_back(root: Path, *, places: Sequence[str] | None = None, allow_synced: bool | None = None) -> str | None:
    """Why no knowledge files go into ``root`` although an agent works there, or None when they may.

    - A folder a cloud service keeps in sync copies them to other devices and to the service, unless the user
      allows it (``SYNCED_SETTING``).
    - A project that publishes an npm package, or builds a Docker image from its folder, would carry them in
      the package or image, unless they are excluded there: Git's exclude file, where KnowItAll2 keeps them out
      of Git, means nothing to npm or Docker.
    """

    key = _key(root.resolve())
    if not (_synced_allowed() if allow_synced is None else allow_synced):
        for place in synced_places() if places is None else places:
            if key == place or key.startswith(place.rstrip("\\/") + os.sep):
                return (f"a cloud service keeps this folder in sync, so knowledge files would be copied off this "
                        f"computer; to allow it, set \"{SYNCED_SETTING}\": true in KnowItAll2's config.json")
    npm = _npm_publishes(root)
    if npm:
        return npm
    if _copies_the_folder(root) and not _names_folder(root / ".dockerignore"):
        return f"a Docker build copies this folder into its image; add {FOLDER}/ to .dockerignore to allow it"
    return None


_COPIES_ALL = re.compile(r"^\s*(?:COPY|ADD)\b(?:\s+--(?!from)\S+)*\s+\.(?:/)?\s", re.IGNORECASE | re.MULTILINE)


def _copies_the_folder(root: Path) -> bool:
    """Whether a Dockerfile or Containerfile in ``root`` copies the whole folder into its image (``COPY . ...``)."""

    try:
        with os.scandir(root) as scan:
            files = [entry.path for entry in scan if entry.is_file() and _a_container_file(entry.name)]
    except OSError:
        return False
    for path in files:
        try:
            if _COPIES_ALL.search(read_text(Path(path))):
                return True
        except OSError:
            continue
    return False


def _npm_publishes(root: Path) -> str | None:
    package = root / "package.json"
    if not package.is_file():
        return None
    try:
        data = json.loads(read_text(package))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("private") is True or not (data.get("name") and data.get("version")):
        return None   # not published (npm publishes only a package with a name and a version)
    files = data.get("files")
    if isinstance(files, list) and not any(str(item).strip("./*") == "" for item in files):
        return None   # only the files it lists are published
    ignore = root / ".npmignore"
    if _names_folder(ignore if ignore.is_file() else root / ".gitignore"):
        return None
    return f"npm would publish this folder with the package; add {FOLDER}/ to .npmignore to allow it"


def _names_folder(path: Path) -> bool:
    try:
        lines = read_text(path).splitlines()
    except OSError:
        return False
    def bare(line: str) -> str:
        line = line.strip().removeprefix("**/").strip("/")
        for ending in ("/**", "/*"):
            line = line.removesuffix(ending)
        return line.strip("/")

    return any(bare(line) == FOLDER for line in lines)


def _synced_allowed() -> bool:
    try:
        data = json.loads(read_text(data_home() / "config.json"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get(SYNCED_SETTING) is True


def project_roots(connection: sqlite3.Connection) -> list[tuple[Path, list[str]]]:
    """The folders to write, each with the projects whose files it gets.

    Per project, the clone an agent worked in last and that clone's worktrees, which share its Git folder; a
    copy with its own Git folder elsewhere is an old copy and is left alone. Only folders an agent session ran
    in during the last 30 days count (``worked_in``), never one a tool call merely named.
    """

    from .identity import identify

    found = []   # (folder, when worked in, its shared Git folder, its project)
    for key, item in worked_folders(connection).items():
        root = Path(key)
        dirs = repository_dirs(root) if root.is_dir() else None
        project = identify(root) if dirs else None
        when = _when(item.get("at"))
        if dirs and project and when:
            found.append((root, when, _key(dirs[1].resolve()), project.id))
    return _choose(found)


def _choose(found: Sequence[tuple[Any, datetime, str, str]]) -> list[tuple[Any, list[str]]]:
    """Of the folders found (each with when it was worked in, its shared Git folder, and its project), those
    to write, each with the projects whose files it gets: per project, the copy worked in last and every
    folder sharing its Git folder."""

    # Folders of one project, or sharing one Git folder (the worktrees of a repository without a remote are
    # projects of their own), are copies of one another.
    parent: dict[str, str] = {}

    def group(name: str) -> str:
        while parent.setdefault(name, name) != name:
            name = parent[name]
        return name

    for _, _, common, project_id in found:
        parent[group(f"git:{common}")] = group(f"project:{project_id}")
    copies: dict[str, list[tuple[Any, datetime, str, str]]] = {}
    for item in found:
        copies.setdefault(group(f"project:{item[3]}"), []).append(item)
    roots = []
    for members in copies.values():
        newest = max(members, key=lambda item: item[1])
        chosen = [item for item in members if item[2] == newest[2]]
        projects = sorted({item[3] for item in chosen})
        roots += [(item[0], projects) for item in chosen]
    return roots


def _seen_roots(connection: sqlite3.Connection) -> list[Path]:
    """Every folder KnowItAll2 has seen a project in, or noted an agent working in, that still exists."""

    paths = [path for (path,) in connection.execute("SELECT DISTINCT path FROM project_paths")]
    seen, roots = set(), []
    for path in [*paths, *_read_worked()["folders"]]:
        root = Path(path)
        if _key(root) not in seen and root.is_dir():
            seen.add(_key(root))
            roots.append(root)
    return roots


# --- Where agents work ------------------------------------------------------------------------------------

# The folders agent sessions ran in, and when last: knowledge files are written only there (review
# 2026-10-11). The session-start and message hooks note the folder each agent reports.
WORKED_FILE = "worked_in.json"
WORKED_DAYS = 30
WORKED_NOTED_AGAIN_SECONDS = 3600


def worked_in(folder: str | os.PathLike[str] | None) -> None:
    """Note that an agent session runs in ``folder``'s repository. Cheap, and never raises.

    Only the hooks call this, with the folder the agent reports, never for a folder a tool call names.
    """

    if not folder:
        return
    try:
        record, now = _read_worked(), _now()
        if _noted_lately(record, _key(folder), now):   # most often the agent works at the repository's root
            return
        from .identity import identify

        project = identify(Path(folder))
        if project is None or _in_data_home(project.root):
            return
        key = _key(project.root)
        if record["folders"].get(key, {}).get("project") == project.id and _noted_lately(record, key, now):
            return
        record["folders"][key] = _entry(project.root, now, project.id)
        _write_worked(record, now)
    except Exception as exc:
        journal.problem("knowledge files", f"the folder an agent works in was not noted: {type(exc).__name__}: {exc}")


def _entry(root: Path, at: str, project_id: str) -> dict[str, str]:
    """A folder's line in the record: when it was worked in, its project, and the Git folder it shares with the
    other worktrees of its repository, which decides where the files go (``_choose``)."""

    entry = {"at": at, "project": project_id}
    dirs = repository_dirs(root)
    if dirs:
        entry["common"] = _key(dirs[1].resolve())
    return entry


def _noted_lately(record: dict[str, Any], key: str, now: str) -> bool:
    """Whether ``key`` was noted within the hour, and no copy of its project in another Git folder was worked
    in since: a worktree used in turn with another of the same repository is not noted again each time."""

    entry = record["folders"].get(key)
    when, moment = _when((entry or {}).get("at")), _when(now)
    if when is None or moment is None or (moment - when).total_seconds() >= WORKED_NOTED_AGAIN_SECONDS:
        return False
    common = entry.get("common")
    return all((_when(item.get("at")) or when) <= when or (common and item.get("common") == common)
               for item in record["folders"].values() if item.get("project") == entry.get("project"))


def worked_folders(connection: sqlite3.Connection | None = None) -> dict[str, dict[str, str]]:
    """The folders agents worked in during the last 30 days, with when and the project there.

    With ``connection``, the record is seeded once, at the first refresh after the update that began it:
    where the store saw each project in the last 30 days stands in for where agents worked.
    """

    record = _read_worked()
    now = _now()
    if connection is not None and not record.get("seeded"):
        folders = record["folders"]
        for project_id, path, seen in connection.execute(
                "SELECT project_id, path, last_seen_at FROM project_paths ORDER BY last_seen_at"):
            if _in_data_home(path):
                continue
            earlier = _when(folders.get(_key(path), {}).get("at"))
            if _when(seen) is not None and (earlier is None or _when(seen) > earlier):
                folders[_key(path)] = _entry(Path(path), seen, project_id)
        record["seeded"] = now
        _write_worked(record, now)
    return _recent(record["folders"], now)


def _worked_marker() -> str:
    """Changes when the folders to write may have: the folders chosen, and the projects each gets, as
    ``project_roots`` chooses them from the record alone, cheaply enough for every hook.

    Not the newest folder of each project (release-candidate review, R-M1): worktrees of one repository
    used in turn made each the newest by turns, and a full refresh started after nearly every prompt,
    although the folders chosen, which share one Git folder, stayed the same.
    """

    record = _read_worked()
    if not record.get("seeded"):
        return "unseeded"
    found = []
    for key, item in _recent(record["folders"], _now()).items():
        when = _when(item.get("at"))
        if when is not None:   # a line noted before the shared Git folder was, stands for itself
            found.append((key, when, str(item.get("common") or f"folder:{key}"), str(item.get("project"))))
    state = json.dumps(sorted(_choose(found)))
    return hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]


def _recent(folders: dict[str, dict[str, str]], now: str) -> dict[str, dict[str, str]]:
    moment = _when(now)
    if moment is None:
        return {}
    cutoff = moment - timedelta(days=WORKED_DAYS)
    return {key: item for key, item in folders.items() if (_when(item.get("at")) or cutoff) > cutoff}


def _read_worked() -> dict[str, Any]:
    try:
        data = json.loads(read_text(data_home() / WORKED_FILE))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    folders = data.get("folders")
    homes = _data_home_keys()
    data["folders"] = ({str(key): item for key, item in folders.items()
                        if isinstance(item, dict) and not _in_data_home(str(key), homes)}
                       if isinstance(folders, dict) else {})
    return data


def _in_data_home(path: str | os.PathLike[str], homes: Sequence[str] | None = None) -> bool:
    """Whether ``path`` is in KnowItAll2's data folder, such as the clone the agents run (``app``): it never
    gets knowledge files and never counts as a copy of a project (release-candidate review, R-L5)."""

    key = _key(path)
    return any(key == home or key.startswith(home.rstrip(os.sep) + os.sep) for home in homes or _data_home_keys())


def _data_home_keys() -> tuple[str, ...]:
    home = Path(data_home())
    return tuple({_key(home), _key(home.resolve())})


def _write_worked(record: dict[str, Any], now: str) -> None:
    record["folders"] = _recent(record["folders"], now)
    try:
        write_text_atomic(data_home() / WORKED_FILE, json.dumps(record, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def _now() -> str:
    return journal.utc_now()


def _when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def _marker(connection: sqlite3.Connection) -> str:
    return f"{change_marker(connection)}|{_worked_marker()}|{_held_marker()}|{_needed_marker()}"


# The files that decide whether a folder is held back (``held_back``): a change to one is a change to refresh for.
_HOLD_FILES = ("package.json", ".npmignore", ".gitignore", ".dockerignore")


def _held_marker() -> str:
    """What the holds depend on: the setting, and the files that decide in each folder worked in (about 10 ms for
    60 folders, measured 2026-10-13)."""

    parts = [str(_synced_allowed())]
    for key in sorted(_recent(_read_worked()["folders"], _now())):
        if key.startswith(("\\\\", "//")):
            continue   # a network share that is away would hold up every hook (second candidate review, D-L6)
        try:
            with os.scandir(key) as scan:
                for entry in scan:
                    name = entry.name.casefold()
                    if name in _HOLD_FILES or _a_container_file(name):
                        stat = entry.stat()
                        parts.append(f"{key}/{name}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            continue
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _a_container_file(name: str) -> bool:
    name = name.casefold()
    return name.startswith(("dockerfile", "containerfile")) or name.endswith((".dockerfile", ".containerfile"))


def refresh(connection: sqlite3.Connection, *, force: bool = False) -> RefreshReport:
    """Bring every project's folder up to date with the memory; a no-op when nothing changed."""

    marker = _marker(connection)
    status = read_status()
    if not force and status.get("marker") == marker:
        return RefreshReport(marker, unchanged=True)
    roots, held = [], []
    places, allow_synced = synced_places(), _synced_allowed()
    for root, projects in project_roots(connection):
        why = held_back(root, places=places, allow_synced=allow_synced)
        if why:
            held.append({"folder": str(root), "reason": why})
        else:
            roots.append((root, projects))
    marker = _marker(connection)   # the record of where agents work is seeded by now
    rendered = render_files(connection)
    _write_names(rendered.generic)
    table, documents = name_table(connection), _read_documents()
    try:
        commands.write_index(connection, data_home())
    except OSError as exc:
        journal.problem("knowledge files", f"the command index could not be written: {exc}")
    report = RefreshReport(marker)
    owned = _owned(status)
    for root, projects in roots:
        try:
            extra = dependencies(root, projects, rendered, table, cache=documents)
            report.folders.append(write_folder(root, rendered.folder(projects, extra), recorded=_key(root) in owned,
                                               claim=lambda root=root: _claim(root)))
        except OSError as exc:
            report.folders.append(FolderResult(str(root), skipped=f"could not be written: {exc}"))
    _write_documents(documents, [root for root, _ in roots])
    targets = {_key(root) for root, _ in roots}
    retiring = _retire(owned, targets, report)
    written = [item.root for item in report.folders if item.skipped is None]
    problems = [item for item in report.folders if item.skipped and item.skipped.startswith("could not")]
    # A folder KnowItAll2 wrote before stays its own while it is skipped, or still to be cleaned up.
    kept = [path for key, path in owned.items() if key in targets and key not in {_key(root) for root in written}]
    # Recorded even after a problem, so a folder that cannot be written is tried again at the next change,
    # not at every hook.
    _write_status({"marker": marker, "at": journal.utc_now(),
                   "folders": written,
                   "retiring": retiring,
                   "owned": list(_owned({"owned": [*written, *kept, *retiring]}).values()),
                   "refused": [{"folder": item.root, "reason": item.refused} for item in report.folders if item.refused],
                   "held": held,
                   "notes": [f"{item.root}: {note}" for item in report.folders for note in item.notes],
                   "problems": [f"{item.root}: {item.skipped}" for item in problems]})
    for item in problems:
        journal.problem("knowledge files", f"{item.root} {item.skipped}")
    return report


def _owned(status: dict[str, Any]) -> dict[str, str]:
    """The folders KnowItAll2 wrote its files into and has not taken them out of yet, by key: its own record
    of what is its own, kept in the data home (fix plan, N-H2; release-candidate review, R-M3).

    The status written before 0.10.6 lists only the folders written at the last refresh, and those still to
    be cleaned up; both count.
    """

    found: dict[str, str] = {}
    for name in ("owned", "folders", "retiring"):
        paths = status.get(name)
        for path in paths if isinstance(paths, list) else []:
            if isinstance(path, str):
                try:   # the form the folders to write are named in, whatever form the record holds
                    key = _key(Path(path).resolve())
                except (OSError, ValueError):
                    key = _key(path)
                found.setdefault(key, path)
    return found


def _claim(root: Path) -> None:
    """Record ``root`` as KnowItAll2's before anything is written into its new folder, so a crash before the
    index is written cannot leave files that neither the marker nor the record vouches for."""

    status = read_status()
    _write_status({**status, "owned": [*_owned(status).values(), str(root)]})


def _retire(owned: dict[str, str], targets: set[str], report: RefreshReport) -> list[str]:
    """Take KnowItAll2's files out, once, of each folder it wrote that is written no longer.

    Such a folder is then left alone for good, unless an agent works there again. Returns the folders that
    could not be cleaned, to try again at the next refresh.
    """

    retiring = []
    for key, path in owned.items():
        if key in targets:
            continue
        root = Path(path)
        try:
            if root.is_dir():
                changes, done = _remove(root, recorded=True)
                report.retired.append((path, changes))
                if not done:
                    retiring.append(path)
        except OSError as exc:
            retiring.append(path)
            journal.problem("knowledge files", f"KnowItAll2's files could not be taken out of {path}: {exc}")
    return retiring


def remove_all(connection: sqlite3.Connection | None) -> list[str]:
    """Take the folders out of every project KnowItAll2 has seen or wrote to."""

    written = {key: Path(path) for key, path in _owned(read_status()).items()}
    roots = dict(written)
    if connection is not None:
        roots.update({_key(root): root for root in _seen_roots(connection) if _key(root) not in roots})
    changes, retiring = [], []
    for key, root in roots.items():
        try:
            found, done = _remove(root, recorded=key in written)
            changes.extend(found)
        except OSError as exc:
            changes.append(f"could not clean {root}: {exc}")
            done = False
        if not done and key in written:   # tried again when they are turned on
            retiring.append(str(root))
    # Off means off: no reminders before commands either.
    (data_home() / commands.INDEX).unlink(missing_ok=True)
    _write_status({"marker": None, "at": journal.utc_now(), "folders": [], "retiring": retiring, "owned": retiring})
    return changes


def set_enabled(on: bool) -> None:
    """Turn the knowledge files on or off in config.json, keeping its other settings."""

    path = data_home() / "config.json"
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if on:
        data.pop("known_files", None)
    else:
        data["known_files"] = False
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")


def read_status() -> dict[str, Any]:
    try:
        data = json.loads(read_text(data_home() / STATUS_FILE))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(data: dict[str, Any]) -> None:
    try:
        write_text_atomic(data_home() / STATUS_FILE, json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


# --- Pointing an agent to the right file ----------------------------------------------------------------

# A chat is pointed to a file once, when the user's message or the agent's own words name a system or
# project the folder has a file about. The pointer says only where the knowledge is, never what it says.
POINTERS_PER_MESSAGE = 3
POINTER_TEXT_LIMIT = 200_000
POINTER_MINIMUM_CHARACTERS = 4
POINTER_MINIMUM_MEMORIES = 3
POINTER_LOG = "pointers.jsonl"
POINTER_LOG_KEPT = 2000
OWN_NAME = "knowitall2"   # the word that names the file about KnowItAll2 itself
_WORD = re.compile(r"[^\W_]+")
# A name fewer than a tenth of the memories using it are filed under is used in passing: of the memories that
# say "UniFi", 1% are in the network plane's file it points to, "Tailscale" 3%, "Codex" 7%, against 82% to 93%
# for "TacticalRMM", "taskboard", and "KnowItAll2" (docs/research/2026-10-13-root-causes/pointers.md). Such a
# name neither points from the agent's words nor lists a memory again in that file. Too few uses say nothing.
GENERIC_SHARE = 0.10
GENERIC_MINIMUM_USES = 5
NAMES_FILE = "names.json"   # in the data home: the generic names, as the last refresh measured them


class _Table(dict):
    """A name table (``name_table``): each name as words, and its target; ``starts`` holds the lengths of the
    names beginning with each word, so a text is matched in one pass instead of trying every length at each word
    (matching all 4,400 live memories took 2 seconds that way, measured 2026-10-14)."""

    starts: dict[str, tuple[int, ...]]


def name_table(connection: sqlite3.Connection) -> dict[tuple[str, ...], str]:
    """Each name an agent might use, as words, and the file about it.

    A system's own name always counts. An alias counts only when no other system or project uses it, so a
    name shared by several systems points nowhere rather than to the wrong file. Very short names
    never count.
    """

    systems = [(file_name(name), name, _aliases(raw)) for _, name, raw in connection.execute(
        "SELECT id, name, aliases FROM systems")]
    projects = [(file_name(str(name)), str(name), []) for (name,) in connection.execute("SELECT name FROM projects")]
    return _word_table(systems + projects)


def system_table(connection: sqlite3.Connection) -> dict[tuple[str, ...], str]:
    """Each name of a catalogued system, as words, and the system's id, by the rules of ``name_table``."""

    return _word_table([(system_id, name, _aliases(raw)) for system_id, name, raw in connection.execute(
        "SELECT id, name, aliases FROM systems")])


def tool_files(connection: sqlite3.Connection) -> set[str]:
    """The files of the agent's own tools on the workstation (``catalog.workstation_tool``)."""

    from .catalog import workstation_tool

    return {file_name(name) for name, raw, kind in connection.execute("SELECT name, aliases, kind FROM systems")
            if workstation_tool(name, kind, _aliases(raw))}


def named(text: str, table: dict[tuple[str, ...], str]) -> list[str]:
    """The targets of ``table`` that ``text`` names, in the order first named; the longest name wins."""

    return list(dict.fromkeys(target for _, target in _named_keys(text, table)))


def _named_keys(text: str, table: dict[tuple[str, ...], str]) -> list[tuple[tuple[str, ...], str]]:
    """Each name of ``table`` that ``text`` uses, with its target, in order; not a part of a path or a login."""

    folded, found = _matches(text, table)
    return [(key, target) for key, target, start, end in found if not _not_a_mention(folded, start, end)]


def _matches(text: str, table: dict[tuple[str, ...], str]) -> tuple[str, list[tuple[tuple[str, ...], str, int, int]]]:
    """``text`` folded, and each name of ``table`` in it, longest first: its words, target, start, and end."""

    folded = str(text).casefold()
    starts = table.starts if isinstance(table, _Table) else _indexed(table).starts
    words = [(match.group(0), match.start(), match.end()) for match in _WORD.finditer(folded)]
    found: list[tuple[tuple[str, ...], str, int, int]] = []
    index = 0
    while index < len(words):
        for size in starts.get(words[index][0], ()):
            if index + size <= len(words):
                key = tuple(word for word, _, _ in words[index:index + size])
                target = table.get(key)
                if target is not None:
                    break
        else:
            index += 1
            continue
        found.append((key, target, words[index][1], words[index + size - 1][2]))
        index += size
    return folded, found


def _indexed(table: dict[tuple[str, ...], str]) -> _Table:
    indexed = _Table(table)
    lengths: dict[str, set[int]] = {}
    for words in table:
        if words:
            lengths.setdefault(words[0], set()).add(len(words))
    indexed.starts = {word: tuple(sorted(sizes, reverse=True)) for word, sizes in lengths.items()}
    return indexed


def _word_table(entries: Sequence[tuple[str, str, Sequence[str]]]) -> dict[tuple[str, ...], str]:
    names: dict[tuple[str, ...], set[str]] = {}
    aliases: dict[tuple[str, ...], set[str]] = {}
    for target, name, others in entries:
        names.setdefault(tuple(_WORD.findall(name.casefold())), set()).add(target)
        for alias in others:
            aliases.setdefault(tuple(_WORD.findall(alias.casefold())), set()).add(target)
    table: dict[tuple[str, ...], str] = {}
    for words, targets in names.items():
        if len(targets) == 1 and len("".join(words)) >= POINTER_MINIMUM_CHARACTERS:
            table[words] = next(iter(targets))
    for words, targets in aliases.items():
        if words not in names and len(targets) == 1 and len("".join(words)) >= POINTER_MINIMUM_CHARACTERS:
            table[words] = next(iter(targets))
    return _indexed(table)


# --- Who wrote the words, and what part a name plays in them (docs/research/2026-10-13-root-causes/pointers.md)

# What a message hook's prompt can hold besides the user's own message. Claude Code delivers a background task's
# end as a message: a subagent's final result is another agent's words, but a monitor's event lines and a
# background command's notice are a tool's (5 pointers in one minute from a store-tagging monitor). A message
# another session sends begins with a header naming that session ("Codex audit findings review" pointed to
# codex.md). The apps add blocks of their own: the in-app browser's state, a list of plugins.
_NOTICE = re.compile(r"<task-notification>.*?(?:</task-notification>|\Z)", re.DOTALL)
_NOTICE_RESULT = re.compile(r"<result>(.*?)(?:</result>|\Z)", re.DOTALL)
_NOTICE_SUMMARY = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)
_TOOL_NOTICES = ("Monitor event", "Background command")
_HANDOFF = re.compile(r"<cross-session-message\b[^>]*>(.*?)(?:</cross-session-message>|\Z)", re.DOTALL)
_AMBIENT = re.compile(r"<(in-app-browser-context|recommended_plugins|external_codex_apps_open_page|system-reminder|"
                      r"environment_context)\b[^>]*>.*?(?:</\1>|\Z)", re.DOTALL)
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+")


def message_parts(prompt: str | None) -> tuple[list[str], list[str]]:
    """The words of a message hook's prompt, by who wrote them: the user's (their message, or an automation
    prompt they set up, such as a scheduled task or a heartbeat), and other agents' (a subagent's final result,
    the body of a message another session sent), which count as the agent's own words.

    Left out: monitor events and background-command notices, the header of a message from another session,
    and the blocks the apps add.
    """

    agents: list[str] = []

    def notice(match: re.Match[str]) -> str:
        summary = _NOTICE_SUMMARY.search(match.group(0))
        if not (summary and summary.group(1).startswith(_TOOL_NOTICES)):
            agents.extend(result.group(1) for result in _NOTICE_RESULT.finditer(match.group(0)))
        return ""

    def handoff(match: re.Match[str]) -> str:
        agents.append(match.group(1))
        return ""

    text = _AMBIENT.sub("", _HANDOFF.sub(handoff, _NOTICE.sub(notice, str(prompt or ""))))
    return ([text] if text.strip() else []), [item for item in agents if item.strip()]


def pointer_targets(table: dict[tuple[str, ...], str], texts: Sequence[str], own_words: Sequence[str] = (), *,
                    itself: str | None = None, tools: Collection[str] = (),
                    generic: Collection[tuple[str, ...]] = ()) -> list[str]:
    """The files ``texts`` (the user's words) and ``own_words`` (the agent's, and other agents') name as what the
    work is about, the user's first, each in the order first named.

    Never a name that starts a branch, ref, or path (``codex/visual-suite-repair``), one inside a URL, a login
    (``name@``), or a folder deeper in a path; never ``itself``, KnowItAll2's own file, which is given as None in
    the KnowItAll2 project. From the agent's words, never a sentence that passes on KnowItAll2's own output
    ("KnowItAll2 learned 12 things ... through the gateway host"), a file of the agent's own tools (``tools``), or a
    name used in passing (``generic``).
    """

    found: list[str] = []
    for agents, items in ((False, texts), (True, own_words)):
        folded, matches = _matches(_joined(items), table)
        urls = [match.span() for match in _URL.finditer(folded)] if matches else []
        for key, target, start, end in matches:
            if target in found or target == itself or any(low <= start < high for low, high in urls):
                continue
            pair = not agents and _two_names(folded, start, end, table)
            if folded[end:end + 1] == "/" and not pair:
                continue
            if _not_a_mention(folded, start, end) and not (pair and folded[start - 1:start] == "/"):
                continue
            if agents and (target in tools or key in generic or _relays_own_output(folded, start, end)):
                continue
            found.append(target)
    return found


def _two_names(folded: str, start: int, end: int, table: dict[tuple[str, ...], str]) -> bool:
    """Whether the name sits in a word of two parts joined by one slash that are two names, not a branch or a
    path: both parts letters only ("Sonarr/Docker"), or both known names ("Sonarr/Radarr", "nas01/nas02").
    ``sonarr/fix-imports``, ``sonarr/v4`` and ``sonarr/config/x.xml`` stay a branch or a path (candidate review
    of 0.11.1, E-L3)."""

    if folded[end:end + 1] != "/" and folded[start - 1:start] != "/":
        return False
    first, last = start, end
    while first > 0 and not folded[first - 1].isspace():
        first -= 1
    while last < len(folded) and not folded[last].isspace():
        last += 1
    word = folded[first:last].strip("\"'`()[]<>,;:.!?*")
    parts = word.split("/")
    if len(parts) != 2 or not all(parts):
        return False
    if all(part.isalpha() for part in parts):
        return True
    return all(tuple(_WORD.findall(part)) in table and "".join(_WORD.findall(part)) == part.replace("-", "")
               .replace(".", "").replace("_", "") for part in parts)


def _relays_own_output(folded: str, start: int, end: int) -> bool:
    """Whether the name sits in a sentence (to the end of its line) that names KnowItAll2: such a sentence passes
    on its news, a question it asks, or what it saved, not the work."""

    begin = max(folded.rfind(". ", 0, start), folded.rfind("\n", 0, start), 0)
    stop = folded.find("\n", end)
    return OWN_NAME in folded[begin:len(folded) if stop < 0 else stop]


def find_pointers(connection: sqlite3.Connection, texts: Sequence[str], folder: Path,
                  shown: Sequence[str], *, own_words: Sequence[str] = (),
                  commands: Sequence[str] = ()) -> list[tuple[str, str]]:
    """Files in ``folder`` that ``texts`` (the user's) or ``own_words`` (the agent's) name and this chat has not
    been pointed to, with the line to show (``pointer_targets``).

    KnowItAll2's own name points nowhere outside the KnowItAll2 project: agents pass on its news ("KnowItAll2
    learned ..."), which pointed them to its file in every project (2026-10-10: 24 of 165 pointers, one opened).

    What the folder's project depends on is noted for its next refresh (``dependencies``): the systems
    ``commands`` (the chat's shell commands since the last message) reach, and a file named here that the folder
    lacks, so the next mention finds it instead of pointing nowhere.
    """

    if not ours(folder):
        return []
    table = name_table(connection)
    if not table:
        return []
    from .identity import identify

    project = identify(folder.parent)
    tools = tool_files(connection)
    generic = generic_names()
    itself = table.get((OWN_NAME,))
    if project is not None and file_name(project.name) == itself:
        itself = None
    if project is not None and commands:
        note_reached(project.id, commands, table, tools=tools)
    targets = pointer_targets(table, texts, own_words, itself=itself, tools=tools, generic=generic)
    found: list[tuple[str, str, int]] = []
    rejected: set[str] = set()
    lacking: list[str] = []
    for target in targets:
        if target in shown or target in rejected:
            continue
        try:
            head = read_text(folder / target).splitlines()[:3]
        except OSError:
            rejected.add(target)
            lacking.append(target)
            continue
        count = next((line.split(" ", 1)[0] for line in head if line.endswith("memories, newest first.")), "0")
        if not count.isdigit() or int(count) < POINTER_MINIMUM_MEMORIES:
            rejected.add(target)   # a file this small rarely holds what the work needs
            continue
        title = head[0][2:] if head and head[0].startswith("# ") else target
        found.append((target, title, int(count)))
        if len(found) == POINTERS_PER_MESSAGE:
            break
    if project is not None and lacking:
        note_needed(project.id, "named", {target: 1 for target in lacking})
    return [(target, f"KnowItAll2: what is known about {title} is in {FOLDER}/{target} ({count} memories); "
                     "read or search it before working this out again.") for target, title, count in found]


def generic_names() -> set[tuple[str, ...]]:
    """The names used in passing (``GENERIC_SHARE``), as the last refresh measured them from the store."""

    try:
        data = json.loads(read_text(data_home() / NAMES_FILE))
    except (OSError, ValueError):
        return set()
    names = data.get("generic") if isinstance(data, dict) else None
    return {tuple(str(name).split(" ")) for name in names} if isinstance(names, list) else set()


def _write_names(generic: set[tuple[str, ...]]) -> None:
    try:
        write_text_atomic(data_home() / NAMES_FILE,
                          json.dumps({"generic": sorted(" ".join(key) for key in generic)}, indent=0) + "\n")
    except OSError as exc:
        journal.problem("knowledge files", f"the names used in passing could not be written: {exc}")


def _joined(texts: Sequence[str]) -> str:
    return "\n".join(str(item)[:POINTER_TEXT_LIMIT] for item in texts if item)


def _not_a_mention(text: str, start: int, end: int) -> bool:
    """A name that is part of something else: a login (``codex@host``), or a folder deeper in a path or a
    URL (``C:\\Projects\\name\\``, ``.codex``, ``https://name...``). A host at the start of a path counts."""

    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    return after == "@" or before in ("\\", "/", ".")


def log_pointers(agent: str, chat: str, files: Sequence[str], *, at: str) -> None:
    """Keep a local record of each pointer, so a later measurement can see whether agents opened the file."""

    path = data_home() / POINTER_LOG
    try:
        with path.open("a", encoding="utf-8") as stream:
            for target in files:
                stream.write(json.dumps({"at": at, "agent": agent, "chat": chat, "file": target}) + "\n")
        if path.stat().st_size > POINTER_LOG_KEPT * 200:
            lines = read_text(path).splitlines()[-POINTER_LOG_KEPT:]
            write_text_atomic(path, "\n".join(lines) + "\n")
    except OSError:
        pass


def folder_line(project_path: str | os.PathLike[str] | None) -> str | None:
    """The briefing's line saying where everything is, when this project's folder was written."""

    if not project_path or not enabled():
        return None
    from .identity import find_git_root

    root = find_git_root(Path(project_path))
    if root is None or not ours(root / FOLDER):
        return None
    return (f"What KnowItAll2 knows about this project and the systems it uses is also in {FOLDER}/ in this project "
            f"({INDEX} lists the files): search it like any project file.")


# --- What a project depends on ---------------------------------------------------------------------------

# A folder chosen only from where memories were filed missed half of what projects use: 119 of the 238
# systems their own documents name, 62% of those their chats reached by command, and every system of a new
# project (docs/research/2026-10-13-root-causes/pointers.md, H4). So a folder also gets, besides its projects'
# own files (``Rendered.uses``):
# - the systems its documents and scripts name, each in at least ``DOCUMENTED_FILES`` files;
# - the systems its chats' shell commands reached, in at least ``REACHED_COMMANDS`` commands, in the last
#   ``NEEDED_DAYS`` days;
# - a system's file a chat there named, as a pointer would have, while the folder lacked it.
# Never the files of the agent's own tools, and never by a name used in passing.
NEEDED_FILE = "needed.json"     # in the data home: what the hooks noted, by project
NEEDED_DAYS = 30
REACHED_COMMANDS = 2
COMMAND_TEXT_LIMIT = 20_000
DOCUMENTS_FILE = "documents.json"   # in the data home: what each folder's documents name, until they change
DOCUMENTED_FILES = 2
DOCUMENT_BYTES = 400_000
DOCUMENTS_PER_FOLDER = 600
# Where a repository says what it depends on: its top-level notes and scripts, its docs, its scripts folder.
DOCUMENT_PLACES = (("", (".md", ".ps1", ".sh")), ("scripts", None), ("docs", (".md",)))
# Folders of other people's packages and of what a build makes, whose READMEs name anything (E-L4).
DOCUMENT_SKIPPED = frozenset({"node_modules", "vendor", "build", "dist", "site", "__pycache__", "venv"})
DOCUMENTS_VERSION = 2   # what a document is read as changed: what the cache holds is read again


def note_reached(project_id: str, commands: Sequence[str], table: dict[tuple[str, ...], str], *,
                 tools: Collection[str] = ()) -> None:
    """Note the systems ``commands`` name for ``project_id``'s next refresh, each once a command
    (``command_names``).

    Not KnowItAll2's own commands, or a search of its folder, which name what they look up; never raises. A name
    used in passing counts here: a command reaching a host is no passing use, and a host many services run on is
    named in passing by each of their memories (candidate review of 0.11.1, E-M4).
    """

    counts: dict[str, int] = {}
    for command in commands:
        text = str(command)[:COMMAND_TEXT_LIMIT]
        if OWN_NAME in text.casefold():
            continue
        for target in set(command_names(text, table)):
            if target not in tools:
                counts[target] = counts.get(target, 0) + 1
    if counts:
        note_needed(project_id, "reached", counts)


def command_names(text: str, table: dict[tuple[str, ...], str]) -> list[str]:
    """The targets a command names, in order: as ``named``, and also a host a command reaches by URL or network
    path (``curl http://sonarr:8989/...``, ``\\\\nas01\\share``, ``//nas01/share``), which ``named`` takes for a
    folder deeper in a path (candidate review of 0.11.1, E-M3)."""

    folded, found = _matches(text, table)
    return list(dict.fromkeys(target for _, target, start, end in found
                              if not _not_a_mention(folded, start, end) or _host_start(folded, start)))


def _host_start(text: str, start: int) -> bool:
    """Whether the name at ``start`` begins the host of a URL (``scheme://name``) or of a network path
    (``\\\\name``, ``//name``): two slashes or more, all of one kind, at the start of a word, not inside a path such
    as ``C:\\\\work\\\\name`` (a Windows path with its backslashes escaped)."""

    first = start
    while first > 0 and text[first - 1] in "\\/":
        first -= 1
    slashes = text[first:start]
    if len(slashes) < 2 or len(set(slashes)) != 1:
        return False
    before = text[first - 1] if first else " "
    return before.isspace() or before in "\"'`(=,[<" or (before == ":" and slashes[0] == "/")


def note_needed(project_id: str, how: str, counts: dict[str, int]) -> None:
    """Add to what ``project_id``'s folder is to get (``how``: "reached" by commands, or "named" while missing)."""

    try:
        record, now = _read_needed(), _now()
        entries = record["projects"].setdefault(project_id, {}).setdefault(how, {})
        for target, count in counts.items():
            entry = entries.get(target) if isinstance(entries.get(target), dict) else {}
            entries[target] = {"at": now, "count": int(entry.get("count") or 0) + count}
        _write_needed(record, now)
    except Exception as exc:   # a help: never at the cost of the hook
        journal.problem("knowledge files", f"what a project uses was not noted: {type(exc).__name__}: {exc}")


def needed_files(project_ids: Sequence[str], record: dict[str, Any] | None = None) -> dict[str, str]:
    """The files the hooks noted for ``project_ids`` in the last ``NEEDED_DAYS`` days, and how: "reached" (by
    ``REACHED_COMMANDS`` commands or more) or "named". ``record``: what ``_read_needed`` returned, if read already."""

    record, found, now = record or _read_needed(), {}, _now()
    for project_id in project_ids:
        noted = _recent_needed(record["projects"].get(project_id) or {}, now)
        for target, entry in noted.get("reached", {}).items():
            if int(entry.get("count") or 0) >= REACHED_COMMANDS:
                found[target] = "reached"
        for target in noted.get("named", {}):
            found.setdefault(target, "named")
    return found


def _needed_marker() -> str:
    """Changes when the files the hooks noted would change a folder, not at every count; cheap for every hook:
    the record is read once (reading it again for each project took 13 to 179 ms, candidate review E-L2)."""

    record = _read_needed()
    chosen = {project_id: sorted(needed_files([project_id], record)) for project_id in sorted(record["projects"])}
    return hashlib.sha256(json.dumps(chosen, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _read_needed() -> dict[str, Any]:
    try:
        data = json.loads(read_text(data_home() / NEEDED_FILE))
    except (OSError, ValueError):
        data = {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return {"projects": {str(key): value for key, value in projects.items() if isinstance(value, dict)}
            if isinstance(projects, dict) else {}}


def _recent_needed(noted: dict[str, Any], now: str) -> dict[str, dict[str, dict[str, Any]]]:
    moment = _when(now)
    if moment is None:
        return {}
    cutoff = moment - timedelta(days=NEEDED_DAYS)
    return {how: {target: entry for target, entry in entries.items()
                  if isinstance(entry, dict) and (_when(entry.get("at")) or cutoff) > cutoff}
            for how, entries in noted.items() if isinstance(entries, dict)}


def _write_needed(record: dict[str, Any], now: str) -> None:
    kept = {}
    for project_id, noted in record["projects"].items():
        recent = {how: entries for how, entries in _recent_needed(noted, now).items() if entries}
        if recent:
            kept[project_id] = recent
    write_text_atomic(data_home() / NEEDED_FILE, json.dumps({"projects": kept}, indent=1, sort_keys=True) + "\n")


def documented(root: Path, table: dict[tuple[str, ...], str], *, cache: dict[str, Any] | None = None) -> set[str]:
    """The targets of ``table`` that ``root``'s documents and scripts (``DOCUMENT_PLACES``) name, each in at
    least ``DOCUMENTED_FILES`` of them; a name used in passing counts too, since a host many services run on is
    named in passing by each of their memories (candidate review of 0.11.1, E-M4). A binary is not read; a script
    Windows PowerShell saved as UTF-16 is (E-L4).

    ``cache`` (kept in the data home between refreshes, ``_read_documents``) spares the reading: a folder whose
    documents and the names are as they were is not read again, and a document whose text was read before, in
    this folder or in another worktree of its repository, is not searched again. Searching every document of
    the 54 project folders on the live computer took about 10 seconds (2026-10-14), most of it the same
    documents in several worktrees; checking that nothing changed takes about 0.2 seconds.
    """

    cache = {} if cache is None else cache
    names = hashlib.sha256(json.dumps([sorted(f"{' '.join(key)}={target}" for key, target in table.items()),
                                       DOCUMENTS_VERSION]).encode("utf-8")).hexdigest()
    if cache.get("names") != names or not isinstance(cache.get("folders"), dict) or not isinstance(
            cache.get("texts"), dict):
        cache.clear()
        cache.update({"names": names, "folders": {}, "texts": {}})
    listed = _documents(root)
    signature = hashlib.sha256(json.dumps([(str(path), stamp) for path, stamp in listed]).encode("utf-8")).hexdigest()
    held = cache["folders"].get(_key(root))
    if isinstance(held, dict) and held.get("signature") == signature and isinstance(held.get("named"), list):
        return {str(target) for target in held["named"]}
    files: dict[str, int] = {}
    texts = []
    for path, _ in listed:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha1(raw).hexdigest()
        texts.append(digest)
        named_here = cache["texts"].get(digest)
        if not isinstance(named_here, list):
            text = _document_text(raw)
            named_here = sorted({target for _, target in _named_keys(text, table)}) if text is not None else []
            cache["texts"][digest] = named_here
        for target in named_here:
            files[target] = files.get(target, 0) + 1
    found = {target for target, count in files.items() if count >= DOCUMENTED_FILES}
    cache["folders"][_key(root)] = {"signature": signature, "named": sorted(found), "texts": texts}
    return found

def _document_text(raw: bytes) -> str | None:
    """A document's text by its byte-order mark (UTF-16, UTF-8, else UTF-8 with replacements); None for a binary."""

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8-sig", "replace")


def _documents(root: Path) -> list[tuple[Path, list[int]]]:
    """The documents and scripts of ``root`` to read, with their sizes and times; none in a link, a hidden
    folder, or KnowItAll2's own folder, none too large, and at most ``DOCUMENTS_PER_FOLDER``."""

    found: list[tuple[Path, list[int]]] = []
    for place, suffixes in DOCUMENT_PLACES:
        base = root / place if place else root
        if _linked(base) or not base.is_dir():
            continue
        folders = [base]
        while folders and len(found) < DOCUMENTS_PER_FOLDER:
            folder = folders.pop(0)
            try:
                with os.scandir(folder) as scan:
                    entries = sorted(scan, key=lambda entry: entry.name)
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.name == FOLDER or entry.is_symlink() or entry.is_junction():
                    continue   # a junction is no symbolic link to is_symlink (candidate review E-L4)
                if entry.name.casefold() in DOCUMENT_SKIPPED:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if place == "docs":   # the docs folder whole; elsewhere only the top level
                        folders.append(Path(entry.path))
                    continue
                if suffixes is not None and not entry.name.casefold().endswith(suffixes):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.st_size <= DOCUMENT_BYTES and len(found) < DOCUMENTS_PER_FOLDER:
                    found.append((Path(entry.path), [stat.st_size, stat.st_mtime_ns]))
    return found


def dependencies(root: Path, projects: Sequence[str], rendered: Rendered, table: dict[tuple[str, ...], str], *,
                 cache: dict[str, Any] | None = None) -> set[str]:
    """The files of what the projects in ``root`` depend on besides their own memories (``Rendered.uses``): the
    systems its documents name and its chats' commands reached, never the agent's own tools; and any file a chat
    there named, as a pointer would have, while the folder lacked it, when it holds enough memories for a pointer,
    which is what that mention looked for."""

    found = documented(root, table, cache=cache)
    named_there = set()
    for target, how in needed_files(projects).items():
        (found if how == "reached" else named_there).add(target)
    # KnowItAll2's own file only in its own project: other projects' instructions name it ("KnowItAll2 memory").
    itself = table.get((OWN_NAME,))
    own = set().union(*(rendered.uses.get(project_id, set()) for project_id in projects))
    found = {target for target in found if target in rendered.systems and target not in rendered.tools
             and (target != itself or target in own)}
    # Never a tool's file, even one the user named (candidate review E-L7): its knowledge belongs at the command.
    return found | {target for target in named_there
                    if rendered.counts.get(target, 0) >= POINTER_MINIMUM_MEMORIES and target not in rendered.tools}


def _read_documents() -> dict[str, Any]:
    try:
        data = json.loads(read_text(data_home() / DOCUMENTS_FILE))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_documents(cache: dict[str, Any], roots: Sequence[Path]) -> None:
    """Keep what the folders written now name, and the texts they hold, for the next refresh."""

    wanted = {_key(root) for root in roots}
    folders = {key: value for key, value in (cache.get("folders") or {}).items() if key in wanted}
    held = {digest for value in folders.values() for digest in value.get("texts") or []}
    texts = {digest: named for digest, named in (cache.get("texts") or {}).items() if digest in held}
    try:
        write_text_atomic(data_home() / DOCUMENTS_FILE, json.dumps(
            {"names": cache.get("names"), "folders": folders, "texts": texts}, indent=0, sort_keys=True) + "\n")
    except OSError as exc:
        journal.problem("knowledge files", f"what the projects' documents name could not be kept: {exc}")


# --- Starting a refresh -------------------------------------------------------------------------------

def due(connection: sqlite3.Connection) -> bool:
    return read_status().get("marker") != _marker(connection)


def nudge(*, launcher: Callable[..., object] = subprocess.Popen) -> bool:
    """Start a refresh in the background when memories changed since the last one; never raises.

    Cheap when nothing changed: one read-only query on the store.
    """

    try:
        if not enabled() or not database_path().is_file():
            return False
        import sqlite3

        connection = sqlite3.connect(f"file:{database_path().as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            if not due(connection):
                return False
        finally:
            connection.close()
        lock = data_home() / LOCK_FILE
        try:
            if time.time() - lock.stat().st_mtime < LOCK_STALE_SECONDS:
                return False
        except OSError:
            pass
        from .hooks import _detached_options, _learner_environment

        lock.touch()
        launcher([sys.executable, "-B", "-P", "-m", "knowitall2", "known", "--quiet"], stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(data_home()),
                 env=_learner_environment(), close_fds=True, **_detached_options())
        return True
    except Exception as exc:
        journal.problem("knowledge files", f"a refresh could not start: {type(exc).__name__}: {exc}")
        return False


def run_command(arguments: Any) -> int:
    """``knowitall2 known``: write the knowledge files now; ``--off`` takes them out and keeps them out."""

    from .store import Store, StoreError

    try:
        store = Store.open(database_path())
    except StoreError as exc:
        print(f"knowitall2: {exc}", file=sys.stderr)
        return 1
    lock = data_home() / LOCK_FILE
    try:
        connection = store._connection
        if arguments.off:
            set_enabled(False)
            changes = remove_all(connection)
            print("\n".join([*changes, "The knowledge files are off; `knowitall2 known --on` brings them back."]))
            return 0
        if arguments.on:
            set_enabled(True)
        if not enabled():
            print("The knowledge files are off; `knowitall2 known --on` brings them back.")
            return 0
        report = refresh(connection, force=not arguments.quiet)
        if not arguments.quiet:
            print(report.describe())
        return 0
    finally:
        store.close()
        lock.unlink(missing_ok=True)
