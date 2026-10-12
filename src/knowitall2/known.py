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
from typing import TYPE_CHECKING, Any, Callable, Sequence

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

    def everything(self) -> dict[str, str]:
        return {**self.files, INDEX: _index(list(self.lines.values()))}

    def folder(self, projects: Sequence[str]) -> dict[str, str]:
        """The files of a folder of ``projects``: their own, those of the systems their memories are filed
        under or name, and the general file; the index lists only those (review 2026-10-11, the user's choice)."""

        chosen = {file_name(GENERAL)}.union(*(self.uses.get(project_id, set()) for project_id in projects))
        files = {name: text for name, text in self.files.items() if name in chosen}
        return {**files, INDEX: _index([self.lines[name] for name in files])}


def render_files(connection: sqlite3.Connection) -> Rendered:
    """Every file there is, with the files each project's folder gets."""

    from . import secrets

    systems = {row[0]: (row[1], _aliases(row[2]))
               for row in connection.execute("SELECT id, name, aliases FROM systems")}
    projects = dict(connection.execute("SELECT id, name FROM projects"))
    notes = dict(connection.execute("SELECT record_id, system_id FROM record_notes WHERE system_id IS NOT NULL"))
    facets = dict(connection.execute("SELECT record_id, facet FROM record_notes"))
    table = name_table(connection)
    system_files = {file_name(name) for name, _ in systems.values()}
    uses: dict[str, set[str]] = {project_id: {file_name(str(name))} for project_id, name in projects.items()}
    groups: dict[str, dict[str, Any]] = {}
    elsewhere: list[tuple[list[str], str]] = []   # the files a memory names besides its own, and its entry there
    rows = connection.execute(
        "SELECT id, kind, text, scope, project_id, verification, created_at FROM records "
        "WHERE status = 'active' ORDER BY created_at DESC, seq DESC"
    )
    for record_id, kind, text, scope, project_id, verification, created_at in rows:
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
        body = secrets.redact(" ".join(str(text).split()))
        saved = f"[{record_id}] {kind}, saved {str(created_at)[:10]}{where}{unverified}"
        group["entries"].append(_wrap(f"- {saved}: {body}"))
        group["sections"][_section(facets.get(record_id), kind)].append(group["entries"][-1])
        home = file_name(name)
        others = [target for target in named(str(text), table) if target != home]
        if others:
            elsewhere.append((others, _wrap(f"- {saved}, filed in {home}: {body}")))
        if scope == "project" and project_id:
            uses.setdefault(project_id, set()).update([home, *(target for target in others if target in system_files)])
    for others, entry in elsewhere:   # only into files that exist: a name alone makes no file
        for target in others:
            if target in groups:
                groups[target]["elsewhere"].append(entry)
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
    return Rendered(files, index_lines, uses)


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
    return f"{change_marker(connection)}|{_worked_marker()}"


def refresh(connection: sqlite3.Connection, *, force: bool = False) -> RefreshReport:
    """Bring every project's folder up to date with the memory; a no-op when nothing changed."""

    marker = _marker(connection)
    status = read_status()
    if not force and status.get("marker") == marker:
        return RefreshReport(marker, unchanged=True)
    roots = project_roots(connection)
    marker = _marker(connection)   # the record of where agents work is seeded by now
    rendered = render_files(connection)
    try:
        commands.write_index(connection, data_home())
    except OSError as exc:
        journal.problem("knowledge files", f"the command index could not be written: {exc}")
    report = RefreshReport(marker)
    owned = _owned(status)
    for root, projects in roots:
        try:
            report.folders.append(write_folder(root, rendered.folder(projects), recorded=_key(root) in owned,
                                               claim=lambda root=root: _claim(root)))
        except OSError as exc:
            report.folders.append(FolderResult(str(root), skipped=f"could not be written: {exc}"))
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


def named(text: str, table: dict[tuple[str, ...], str]) -> list[str]:
    """The targets of ``table`` that ``text`` names, in the order first named; the longest name wins."""

    folded = str(text).casefold()
    words = [(match.group(0), match.start(), match.end()) for match in _WORD.finditer(folded)]
    longest = max((len(key) for key in table), default=0)
    found: list[str] = []
    index = 0
    while index < len(words):
        for size in range(min(longest, len(words) - index), 0, -1):
            target = table.get(tuple(word for word, _, _ in words[index:index + size]))
            if target is not None:
                break
        else:
            index += 1
            continue
        if not _not_a_mention(folded, words[index][1], words[index + size - 1][2]) and target not in found:
            found.append(target)
        index += size
    return found


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
    return table


def find_pointers(connection: sqlite3.Connection, texts: Sequence[str], folder: Path,
                  shown: Sequence[str], *, own_words: Sequence[str] = ()) -> list[tuple[str, str]]:
    """Files in ``folder`` that ``texts`` (the user's) or ``own_words`` (the agent's) name and this chat has not
    been pointed to, with the line to show.

    KnowItAll2's own name in the agent's words points nowhere: agents pass on its news ("KnowItAll2 learned
    ..."), which pointed them to its file in every project (2026-10-10: 24 of 165 pointers, one opened).
    """

    if not ours(folder):
        return []
    table = name_table(connection)
    if not table:
        return []
    itself = table.get((OWN_NAME,))
    targets = named(_joined(texts), table)
    targets += [target for target in named(_joined(own_words), table) if target != itself and target not in targets]
    found: list[tuple[str, str, int]] = []
    rejected: set[str] = set()
    for target in targets:
        if target in shown or target in rejected:
            continue
        try:
            head = read_text(folder / target).splitlines()[:3]
        except OSError:
            rejected.add(target)
            continue
        count = next((line.split(" ", 1)[0] for line in head if line.endswith("memories, newest first.")), "0")
        if not count.isdigit() or int(count) < POINTER_MINIMUM_MEMORIES:
            rejected.add(target)   # a file this small rarely holds what the work needs
            continue
        title = head[0][2:] if head and head[0].startswith("# ") else target
        found.append((target, title, int(count)))
        if len(found) == POINTERS_PER_MESSAGE:
            break
    return [(target, f"KnowItAll2: what is known about {title} is in {FOLDER}/{target} ({count} memories); "
                     "read or search it before working this out again.") for target, title, count in found]


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
