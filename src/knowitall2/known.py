"""Everything KnowItAll2 knows, as files in each project, so agents reach it with their own tools.

Every active memory is written to ``knowitall2-known/`` at the root of each project folder KnowItAll2
has seen: one file per catalogued system or project, newest first, in short lines a search shows
whole. KnowItAll2 decides nothing about what an agent may see; the files hold everything the store
holds (docs/knowledge-files-design.md).

Git never sees the folder: it is listed in the repository's shared exclude file, which every worktree
reads and no commit carries. Search tools do: ``rg`` and Claude Code's Grep skip what Git ignores, so a
root ``.ignore`` file allows the folder again. A folder or ``.ignore`` that KnowItAll2 did not write is
left alone.

Each computer writes the files from its own copy of the memory, after a change, in a detached process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from . import journal
from .files import read_text, write_text_atomic
from .paths import data_home, database_path

if TYPE_CHECKING:  # hooks import this module; SQLite and the secret screen load only when needed
    import sqlite3

FOLDER = "knowitall2-known"
INDEX = "README.md"
GENERAL = "General"
LINE_WIDTH = 160
# The first line of the index: a folder without it was not written by KnowItAll2 and is never touched.
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
# Names Windows refuses as file names, whatever the extension.
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}


# --- What the files say --------------------------------------------------------------------------

def file_name(name: str) -> str:
    """The file for a system or project name: case-insensitive, safe on every platform, never the index."""

    slug = re.sub(r"[^\w.-]+", "-", name.casefold()).strip("-.") or "unnamed"
    if slug in _RESERVED or slug == Path(INDEX).stem.casefold():
        slug += "-"
    return slug + ".md"


def render(connection: sqlite3.Connection) -> dict[str, str]:
    """Every file of the folder, by name: one per system or project, and the index."""

    from . import secrets

    systems = {row[0]: (row[1], _aliases(row[2]))
               for row in connection.execute("SELECT id, name, aliases FROM systems")}
    projects = dict(connection.execute("SELECT id, name FROM projects"))
    notes = dict(connection.execute("SELECT record_id, system_id FROM record_notes WHERE system_id IS NOT NULL"))
    facets = dict(connection.execute("SELECT record_id, facet FROM record_notes"))
    groups: dict[str, dict[str, Any]] = {}
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
        group = groups.setdefault(file_name(name), {"names": [], "aliases": [], "entries": [],
                                                    "sections": {key: [] for key, _ in SECTIONS}})
        if name not in group["names"]:
            group["names"].append(name)
        group["aliases"].extend(alias for alias in aliases if alias not in group["aliases"] and alias != name)
        where = f", project {projects[project_id]}" if scope == "project" and projects.get(project_id) else ""
        unverified = " (unverified: check before relying on it)" if verification == "unverified" else ""
        body = secrets.redact(" ".join(str(text).split()))
        entry = f"- [{record_id}] {kind}, saved {str(created_at)[:10]}{where}{unverified}: {body}"
        wrapped = "\n  ".join(textwrap.wrap(entry, LINE_WIDTH, break_long_words=False, break_on_hyphens=False))
        group["entries"].append(wrapped)
        group["sections"][_section(facets.get(record_id), kind)].append(wrapped)
    files: dict[str, str] = {}
    index_lines = []
    for name in sorted(groups, key=lambda item: (" / ".join(groups[item]["names"]).casefold(), item)):
        group = groups[name]
        title = " / ".join(group["names"])
        head = [f"# {title}"]
        if group["aliases"]:
            head.append(f"Also called: {', '.join(group['aliases'])}")
        head.append(f"{len(group['entries'])} memories, newest first.")
        filled = [(title, group["sections"][key]) for key, title in SECTIONS if group["sections"][key]]
        if len(filled) == 1:
            body = "\n".join(filled[0][1])
        else:   # how to reach it and the how-tos first, so even a partial read gets them
            body = "\n\n".join(f"## {title}\n\n" + "\n".join(entries) for title, entries in filled)
        files[name] = "\n".join(head) + "\n\n" + body + "\n"
        called = f" (also {', '.join(group['aliases'][:4])})" if group["aliases"] else ""
        index_lines.append(f"- {name}: {title}{called}, {len(group['entries'])} memories")
    files[INDEX] = "\n".join([
        MARKER,
        "# What KnowItAll2 knows",
        "",
        "Everything KnowItAll2 remembers from coding sessions on this computer and the computers it shares",
        "memory with: one file per system or project, newest first. Search it like any project file. A",
        "memory marked unverified is a lead to check. These files are rewritten whenever memories change;",
        "to correct one, use KnowItAll2's remember or forget, not an edit here.",
        "",
        *index_lines,
    ]) + "\n"
    return files


SECTIONS = (
    ("reach", "How to reach it, and where its sign-in is kept"),
    ("howto", "How-tos"),
    ("rest", "Everything else"),
)
_REACH_FACETS = {"access", "signin", "where"}


def _section(facet: str | None, kind: str) -> str:
    if facet in _REACH_FACETS:
        return "reach"
    if facet == "howto" or kind == "procedure":
        return "howto"
    return "rest"


def change_marker(connection: sqlite3.Connection) -> str:
    """Changes whenever anything the files show changes; cheap enough to check at every hook."""

    row = connection.execute(
        "SELECT (SELECT COUNT(*) FROM records WHERE status = 'active'), (SELECT MAX(updated_at) FROM records), "
        "(SELECT MAX(seq) FROM records), (SELECT COUNT(*) FROM record_notes), (SELECT MAX(written_at) FROM record_notes), "
        "(SELECT MAX(updated_at) FROM systems), (SELECT COUNT(*) FROM systems), (SELECT COUNT(*) FROM projects), "
        "(SELECT COUNT(*) FROM project_paths)"
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


def git_common_dir(root: Path) -> Path | None:
    """The folder Git keeps a repository's shared files in (``info/exclude``), for a checkout or a worktree."""

    dot_git = root / ".git"
    if dot_git.is_dir():
        common = dot_git / "commondir"
        return (dot_git / read_text(common).strip()).resolve() if common.is_file() else dot_git
    if dot_git.is_file():
        match = re.match(r"gitdir:\s*(.+)", read_text(dot_git).strip())
        if not match:
            return None
        git_dir = Path(match.group(1).strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
        common = git_dir / "commondir"
        return (git_dir / read_text(common).strip()).resolve() if common.is_file() else git_dir
    return None


def ours(folder: Path) -> bool:
    try:
        return read_text(folder / INDEX).splitlines()[:1] == [MARKER]
    except (OSError, IndexError):
        return False


def write_folder(root: Path, files: dict[str, str]) -> FolderResult:
    """Write the files into ``root``'s folder, keep it out of Git, and let search tools see it."""

    result = FolderResult(str(root))
    common = git_common_dir(root)
    if common is None:
        result.skipped = "not a Git working tree"
        return result
    folder = root / FOLDER
    if folder.exists() and any(folder.iterdir()) and not ours(folder):
        result.skipped = f"{folder} was not written by KnowItAll2; left alone"
        return result
    _ensure_excluded(common / "info" / "exclude")
    ignore = root / IGNORE_FILE
    if not ignore.exists():
        write_text_atomic(ignore, IGNORE_TEXT)
    elif IGNORE_LINE not in read_text(ignore).splitlines():
        result.notes.append(f"{ignore} is not KnowItAll2's, so search tools that follow Git's ignore rules may skip "
                            f"{FOLDER}/; add the line {IGNORE_LINE} to it to let them search it")
    folder.mkdir(exist_ok=True)
    for name, text in files.items():
        path = folder / name
        try:
            if read_text(path) == text:
                continue
        except OSError:
            pass
        write_text_atomic(path, text)
        result.written += 1
    for path in folder.glob("*.md"):
        if path.name not in files:
            path.unlink(missing_ok=True)
            result.removed += 1
    return result


def remove_folder(root: Path) -> list[str]:
    """Take out everything KnowItAll2 added to ``root``: its folder, its ``.ignore``, its exclude lines."""

    changes = []
    folder = root / FOLDER
    if folder.is_dir() and ours(folder):
        for path in folder.glob("*.md"):
            path.unlink(missing_ok=True)
        try:
            folder.rmdir()
            changes.append(f"removed {folder}")
        except OSError:
            changes.append(f"emptied {folder} (it holds files KnowItAll2 did not write)")
    ignore = root / IGNORE_FILE
    try:
        if ignore.is_file() and read_text(ignore) == IGNORE_TEXT:
            ignore.unlink()
            changes.append(f"removed {ignore}")
    except OSError:
        pass
    common = git_common_dir(root)
    if common is not None and _remove_excluded(common / "info" / "exclude"):
        changes.append(f"removed KnowItAll2's lines from {common / 'info' / 'exclude'}")
    return changes


def _ensure_excluded(path: Path) -> None:
    try:
        text = read_text(path)
    except FileNotFoundError:
        text = ""
    if EXCLUDE_BEGIN in text:
        return
    block = "\n".join([EXCLUDE_BEGIN, *EXCLUDE_LINES, EXCLUDE_END]) + "\n"
    write_text_atomic(path, text + ("" if not text or text.endswith("\n") else "\n") + block)


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


def project_roots(connection: sqlite3.Connection) -> list[Path]:
    """The project folders KnowItAll2 has seen that still exist, each once."""

    seen, roots = set(), []
    for (path,) in connection.execute("SELECT DISTINCT path FROM project_paths"):
        root = Path(path)
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen and root.is_dir():
            seen.add(key)
            roots.append(root)
    return roots


def refresh(connection: sqlite3.Connection, *, force: bool = False) -> RefreshReport:
    """Bring every project's folder up to date with the memory; a no-op when nothing changed."""

    marker = change_marker(connection)
    status = read_status()
    if not force and status.get("marker") == marker:
        return RefreshReport(marker, unchanged=True)
    files = render(connection)
    report = RefreshReport(marker)
    for root in project_roots(connection):
        try:
            report.folders.append(write_folder(root, files))
        except OSError as exc:
            report.folders.append(FolderResult(str(root), skipped=f"could not be written: {exc}"))
    problems = [item for item in report.folders if item.skipped and item.skipped.startswith("could not")]
    # Recorded even after a problem, so a folder that cannot be written is tried again at the next change,
    # not at every hook.
    _write_status({"marker": marker, "at": journal.utc_now(),
                   "folders": [item.root for item in report.folders if item.skipped is None],
                   "notes": [f"{item.root}: {note}" for item in report.folders for note in item.notes],
                   "problems": [f"{item.root}: {item.skipped}" for item in problems]})
    for item in problems:
        journal.problem("knowledge files", f"{item.root} {item.skipped}")
    return report


def remove_all(connection: sqlite3.Connection | None) -> list[str]:
    """Take the folders out of every project KnowItAll2 has seen or wrote to."""

    roots = {os.path.normcase(os.path.abspath(path)): Path(path) for path in read_status().get("folders", [])}
    if connection is not None:
        roots.update({os.path.normcase(os.path.abspath(root)): root for root in project_roots(connection)})
    changes = []
    for root in roots.values():
        try:
            changes.extend(remove_folder(root))
        except OSError as exc:
            changes.append(f"could not clean {root}: {exc}")
    _write_status({"marker": None, "at": journal.utc_now(), "folders": []})
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

# A chat is pointed to a file once, when the user's message or the agent's own tool calls name a system or
# project the folder has a file about. The pointer says only where the knowledge is, never what it says.
POINTERS_PER_MESSAGE = 3
POINTER_TEXT_LIMIT = 200_000
POINTER_MINIMUM_CHARACTERS = 4
POINTER_MINIMUM_MEMORIES = 3
POINTER_LOG = "pointers.jsonl"
POINTER_LOG_KEPT = 2000
_WORD = re.compile(r"[^\W_]+")


def name_table(connection: sqlite3.Connection) -> dict[tuple[str, ...], str]:
    """Each name an agent might use, as words, and the file about it.

    A system's own name always counts. An alias counts only when no other system or project uses it, so a
    name shared by several systems points nowhere rather than to the wrong file. Very short names
    never count.
    """

    names: dict[tuple[str, ...], set[str]] = {}
    aliases: dict[tuple[str, ...], set[str]] = {}
    for _, name, raw in connection.execute("SELECT id, name, aliases FROM systems"):
        target = file_name(name)
        names.setdefault(tuple(_WORD.findall(name.casefold())), set()).add(target)
        for alias in _aliases(raw):
            aliases.setdefault(tuple(_WORD.findall(alias.casefold())), set()).add(target)
    for (name,) in connection.execute("SELECT name FROM projects"):
        names.setdefault(tuple(_WORD.findall(str(name).casefold())), set()).add(file_name(str(name)))
    table: dict[tuple[str, ...], str] = {}
    for words, targets in names.items():
        if len(targets) == 1 and len("".join(words)) >= POINTER_MINIMUM_CHARACTERS:
            table[words] = next(iter(targets))
    for words, targets in aliases.items():
        if words not in names and len(targets) == 1 and len("".join(words)) >= POINTER_MINIMUM_CHARACTERS:
            table[words] = next(iter(targets))
    return table


def find_pointers(connection: sqlite3.Connection, texts: Sequence[str], folder: Path,
                  shown: Sequence[str]) -> list[tuple[str, str]]:
    """Files in ``folder`` that ``texts`` name and this chat has not been pointed to, with the line to show."""

    if not ours(folder):
        return []
    table = name_table(connection)
    if not table:
        return []
    alternatives = sorted((r"[\W_]+".join(map(re.escape, words)) for words in table if words), key=len, reverse=True)
    pattern = re.compile(r"(?<![^\W_])(?:" + "|".join(alternatives) + r")(?![^\W_])")
    text = "\n".join(str(item)[:POINTER_TEXT_LIMIT] for item in texts if item).casefold()
    found: list[tuple[str, str, int]] = []
    rejected: set[str] = set()
    for match in pattern.finditer(text):
        if _not_a_mention(text, match.start(), match.end()):
            continue
        target = table.get(tuple(_WORD.findall(match.group(0))))
        if not target or target in shown or target in rejected or any(target == item[0] for item in found):
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
    return (f"Everything KnowItAll2 knows is also in {FOLDER}/ in this project ({INDEX} lists the files): "
            "search it like any project file.")


# --- Starting a refresh -------------------------------------------------------------------------------

def due(connection: sqlite3.Connection) -> bool:
    return read_status().get("marker") != change_marker(connection)


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
