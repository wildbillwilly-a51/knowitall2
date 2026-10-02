"""Shared test helpers: import the package from ``src`` and build fixtures."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

# No test may touch the real data home, even one that forgets to set its own:
# the journal writes problems there from deep inside the code.
_TEST_HOME = tempfile.mkdtemp(prefix="knowitall2-tests-")
os.environ["KNOWITALL2_HOME"] = _TEST_HOME
# Tests run the same wherever they are started, including inside an agent's session.
os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
atexit.register(shutil.rmtree, _TEST_HOME, True)


def make_repository(root: Path, remote: str | None = None, *, remote_name: str = "origin") -> Path:
    """Create the minimal Git layout identity reads; no Git executable is needed."""

    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    lines = ["[core]", "\trepositoryformatversion = 0"]
    if remote is not None:
        lines += [f'[remote "{remote_name}"]', f"\turl = {remote}", "\tfetch = +refs/heads/*:refs/remotes/origin/*"]
    (git_dir / "config").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


class Clock:
    """A settable clock for deterministic timestamps."""

    def __init__(self, value: str = "2026-09-27T12:00:00Z") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value
