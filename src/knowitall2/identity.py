"""Identify the project a folder belongs to, using Git metadata alone.

Identity comes from the repository's remote when it has one, so moved,
renamed, and cloned working copies of one repository share a project. A
repository without a remote falls back to its root path; when it gains a
remote, what was saved under its folder's id follows it (``path_project_id``,
``Memory.rejoin``). Nothing is ever written into the repository.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

_SECTION = re.compile(r'^\s*\[\s*(?P<name>[A-Za-z0-9.\-]+)(?:\s+"(?P<sub>(?:[^"\\]|\\.)*)")?\s*\]')
_KEY_VALUE = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z0-9\-]*)\s*=\s*(?P<value>.*?)\s*$")
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>\S+)$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class ProjectIdentity:
    id: str
    name: str
    root: Path
    remote: str | None

    @property
    def path_key(self) -> str:
        """The root path in the platform's case-insensitive comparison form."""
        return os.path.normcase(str(self.root))


def identify(start: str | os.PathLike[str]) -> ProjectIdentity | None:
    """Return the project containing ``start``, or ``None`` outside Git."""

    root = find_git_root(Path(start))
    if root is None:
        return None
    raw_remote = read_remote_url(root)
    remote = normalize_remote(raw_remote)
    project_id = _project_id(f"git:{remote}") if remote else path_project_id(os.path.normcase(str(root)))
    name = (_display_name(raw_remote) if remote else None) or root.name
    return ProjectIdentity(project_id, name, root, remote)


def path_project_id(path_key: str) -> str:
    """The id of the repository in a folder (``ProjectIdentity.path_key``) while it has no remote."""

    return _project_id(f"path:{path_key}")


def _project_id(key: str) -> str:
    return "prj-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def identify_remote(url: str, name: str | None = None) -> ProjectIdentity | None:
    """The project for a Git remote without a local working copy, as ``identify`` would name it.

    The identity has no root path; it matches any working copy of that remote.
    """

    remote = normalize_remote(url)
    if remote is None or remote.startswith("file:"):
        return None
    return ProjectIdentity(_project_id(f"git:{remote}"), name or _display_name(url) or remote, Path(""), remote)


def find_git_root(start: Path) -> Path | None:
    """The repository holding ``start``: the nearest folder at or above it with a ``.git`` Git can use.

    An empty folder belongs to no repository, even inside one: it is most often
    what a project left behind when it moved, and the repository above is some
    other project (the live case: a chat opened in a project's folder that later
    moved away was filed under the parent folder's repository). A folder that
    does not exist yet still counts as part of the repository around it.
    """

    try:
        current = start.resolve()
    except OSError:
        return None
    if repository_dirs(current) is not None:
        return current
    if _vacant(current):
        return None
    for candidate in current.parents:
        if repository_dirs(candidate) is not None:
            return candidate
    return None


def repository_dirs(root: Path) -> tuple[Path, Path] | None:
    """The Git folder of the working tree at ``root``, and the one it shares with its other worktrees.

    None unless Git could use them: the Git folder holds ``HEAD`` and the shared one ``objects``. A ``.git``
    folder with nothing else in it (the live case: one holding only ``info/`` made a parent folder of
    projects a project), or a worktree whose repository moved away, is not a repository.
    """

    marker = root / ".git"
    try:
        mode = marker.stat().st_mode
        if stat.S_ISDIR(mode):
            git_dir = marker
        elif stat.S_ISREG(mode):
            content = marker.read_text(encoding="utf-8", errors="replace").strip()
            if not content.lower().startswith("gitdir:"):
                return None
            git_dir = Path(content[len("gitdir:"):].strip())
            if not git_dir.is_absolute():
                git_dir = root / git_dir
        else:
            return None
        common = git_dir
        pointer = git_dir / "commondir"
        if pointer.is_file():
            relative = pointer.read_text(encoding="utf-8", errors="replace").strip()
            if relative:
                candidate = Path(relative)
                common = candidate if candidate.is_absolute() else git_dir / candidate
        if (git_dir / "HEAD").is_file() and (common / "objects").is_dir():
            return git_dir, common
    except OSError:
        pass
    return None


def _vacant(folder: Path) -> bool:
    """Whether ``folder`` exists and holds nothing."""

    try:
        with os.scandir(folder) as entries:
            return next(entries, None) is None
    except OSError:  # gone, a file, or a folder that cannot be listed: judge by where it is
        return False


def read_remote_url(root: Path) -> str | None:
    """Return the ``origin`` URL, or the first remote's URL if there is no origin."""

    config = _config_path(root)
    if config is None:
        return None
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    remotes: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        section = _SECTION.match(line)
        if section:
            is_remote = section.group("name").lower() == "remote"
            current = section.group("sub") if is_remote else None
            continue
        if current is None or current in remotes:
            continue
        pair = _KEY_VALUE.match(line)
        if pair and pair.group("key").lower() == "url":
            value = pair.group("value")
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            if value:
                remotes[current] = value
    if "origin" in remotes:
        return remotes["origin"]
    return next(iter(remotes.values()), None)


def normalize_remote(url: str | None) -> str | None:
    """Reduce any remote URL form to ``host/path`` without credentials or ``.git``."""

    if not url or not url.strip():
        return None
    value = url.strip()
    if _WINDOWS_DRIVE.match(value) or value.startswith(("/", "\\", ".")):
        return "file:" + os.path.normcase(os.path.abspath(value))
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme.lower() == "file":
            return "file:" + os.path.normcase(os.path.abspath(unquote(parts.path)))
        host = (parts.hostname or "").lower()
        path = parts.path
    else:
        match = _SCP_LIKE.match(value)
        if match is None:
            return None
        host = match.group("host").lower()
        path = match.group("path")
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4].rstrip("/")
    if not host or not path:
        return None
    return f"{host}/{path.lower()}"


def _config_path(root: Path) -> Path | None:
    found = repository_dirs(root)
    return found[1] / "config" if found else None


def _display_name(raw_remote: str | None) -> str | None:
    if not raw_remote:
        return None
    tail = re.split(r"[/\\:]", raw_remote.strip().rstrip("/\\"))[-1]
    if tail.lower().endswith(".git"):
        tail = tail[:-4]
    return tail or None
