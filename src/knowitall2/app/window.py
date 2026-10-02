"""Open the app in a window of its own: a browser's app mode, without tabs or an address bar.

Windows has Microsoft Edge; Linux uses Chrome, Chromium, Edge, or Brave when
one is installed, and otherwise the default browser. Nothing is installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

WINDOW_SIZE = "1280,860"
_LINUX_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                   "microsoft-edge-stable", "brave-browser")


def app_browser() -> Path | None:
    """A Chromium-based browser that can open an app window, if one is installed."""

    if sys.platform == "win32":
        roots = [os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        candidates = [Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe" for root in roots if root]
        candidates += [Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe" for root in roots if root]
        return next((candidate for candidate in candidates if candidate.is_file()), None)
    for name in _LINUX_BROWSERS:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def open_window(url: str) -> bool:
    """Open ``url`` as an app window, else in the default browser; False if neither worked."""

    browser = app_browser()
    if browser is not None and _has_display():
        try:
            subprocess.Popen(
                [str(browser), f"--app={url}", f"--window-size={WINDOW_SIZE}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, **_detached(),
            )
            return True
        except OSError:
            pass
    if not _has_display():
        return False
    try:
        return webbrowser.open(url, new=1)
    except webbrowser.Error:
        return False


def _has_display() -> bool:
    if sys.platform == "win32":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _detached() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}
