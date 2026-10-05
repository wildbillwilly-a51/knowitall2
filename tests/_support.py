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
# No test starts a background rewrite of knowledge files; test_known turns them on where it tests them.
os.environ["KNOWITALL2_KNOWN_FILES"] = "0"
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


class SystemProxy:
    """A stand-in for a system proxy that answers every request with 502 and notes what it saw.

    Used as a context manager, it is the proxy in the environment, where
    urllib looks first (as it would look in the Windows settings).
    """

    def __init__(self) -> None:
        import http.server
        import threading

        self.seen: list[tuple[str, dict[str, str]]] = []
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._answer()

            def do_POST(self) -> None:
                self._answer()

            def _answer(self) -> None:
                proxy.seen.append((self.requestline, dict(self.headers.items())))
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "SystemProxy":
        from unittest import mock

        self.thread.start()
        address = f"http://127.0.0.1:{self.server.server_address[1]}"
        names = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")
        environment = {name: value for name, value in os.environ.items() if name not in names}
        self._environment = mock.patch.dict(os.environ, {**environment, "HTTP_PROXY": address,
                                                         "HTTPS_PROXY": address}, clear=True)
        self._environment.start()
        # urlopen keeps the proxies it found on its first use: let it look again.
        self._opener = mock.patch("urllib.request._opener", None)
        self._opener.start()
        return self

    def __exit__(self, *details: object) -> None:
        self._opener.stop()
        self._environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
