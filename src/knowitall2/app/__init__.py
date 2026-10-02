"""The KnowItAll2 app: a local window that shows what KnowItAll2 knows and does.

``knowitall2 app`` starts a private local server for this user, opens it as an
app window, and stops when the window closes. Opening the app again while it
is open brings up another window on the same server.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .. import journal
from ..files import write_text_atomic
from ..paths import data_home
from .server import KEY_HEADER, AppServer
from .window import open_window

INSTANCE_FILE = "app-window.json"


def run(*, show_window: bool = True, port: int = 0) -> int:
    """Open the app; returns when its window has closed."""

    existing = running_instance()
    if existing is not None:
        url = _window_url(existing["port"], existing["key"])
        if show_window and open_window(url):
            _finish_shortcut()
        elif sys.stdout is not None:
            print(f"KnowItAll2 is already open at {url}")
        return 0
    server = AppServer(secrets.token_urlsafe(24), port=port)
    url = _window_url(server.port, server.key)
    instance = instance_path()
    try:
        write_text_atomic(instance, json.dumps({"pid": os.getpid(), "port": server.port, "key": server.key}) + "\n")
        if os.name != "nt":
            instance.chmod(0o600)
    except OSError as exc:
        journal.problem("app", f"could not record the open window: {exc}")
    opened = show_window and open_window(url)
    if opened:
        threading.Thread(target=_finish_shortcut, daemon=True, name="knowitall2-shortcut").start()
    if sys.stdout is not None:
        where = "in its own window" if opened else f"at {url}"
        print(f"KnowItAll2 is open {where}\nIt stops when its window closes.", flush=True)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        _forget_instance(server.port)
    return 0


def _finish_shortcut() -> None:
    """Add the app to the Start menu if setup could only put it on the desktop.

    Opened from the desktop, the app runs outside any app package, so the
    Start menu shortcut it writes is real. It never slows the window.
    """

    from .shortcut import install

    try:
        changes = install(from_app=True)
    except Exception as exc:
        journal.problem("app", f"could not add KnowItAll2 to the Start menu: {exc}")
        return
    for change in changes:
        journal.record_standalone("settings", change[0].upper() + change[1:], outcome="shortcut", agent="app")


def instance_path() -> Path:
    return data_home() / INSTANCE_FILE


def running_instance() -> dict | None:
    """The app server that is already open for this user, if it answers."""

    try:
        details = json.loads(instance_path().read_text(encoding="utf-8"))
        port, key = int(details["port"]), str(details["key"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={KEY_HEADER: key})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            if json.loads(response.read()).get("ok"):
                return {"port": port, "key": key}
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return None


def _window_url(port: int, key: str) -> str:
    # The key travels in the fragment, which never leaves the browser.
    return f"http://127.0.0.1:{port}/#key={key}"


def _forget_instance(port: int) -> None:
    try:
        if json.loads(instance_path().read_text(encoding="utf-8")).get("port") == port:
            instance_path().unlink()
    except (OSError, ValueError):
        pass
