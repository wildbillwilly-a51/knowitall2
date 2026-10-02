"""Codex adapter: an MCP entry in ``config.toml``, the knowitall2 Skill, session hooks, and a block in AGENTS.md.

- In ``config.toml``, KnowItAll2 edits only its own marked block. Every other
  byte of the file is preserved, and the edited file is validated before it
  replaces the original. No copy of the file is made, because agent
  configuration can hold other servers' credentials.
- A short block in the user's global ``AGENTS.md`` (or ``AGENTS.override.md``
  when there is one, since Codex then reads only that) makes KnowItAll2 part
  of how every chat orients itself.
- The SessionStart, Stop, and UserPromptSubmit hooks are entries in ``hooks.json`` that run a
  small launcher in the KnowItAll2 data home. Codex hands hook commands to the
  session shell (PowerShell or cmd on Windows), so the Windows command avoids
  quoting: it uses the short form of folders whose names contain spaces.
  Codex runs a hook only after the user has trusted it once with ``/hooks``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tomllib
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from ..files import short_path as _short_path
from ..paths import data_home
from . import instructions
from .base import (
    SERVER_NAME,
    AgentAdapter,
    AgentError,
    Check,
    JsonFile,
    ServerLaunch,
    ensure_skill_installable,
    install_skill,
    json_object,
    launch_matches,
    remove_skill,
    render_hook_launcher,
    skill_check,
    write_text_atomic,
)

BEGIN = "# BEGIN knowitall2 (managed by `knowitall2 setup codex`; remove with `knowitall2 uninstall codex`)"
END = "# END knowitall2"
HOOK_LAUNCHER = "codex_session_start.py"
HOOK_MATCHER = "startup|resume|clear|compact"
HOOK_TIMEOUT_SECONDS = 30
HOOK_STATUS = "Loading KnowItAll2 memory"
# Session start adds the briefing; a turn's end learns after a commit and shows
# what was learned; the user's message adds what is new since the chat began.
# Codex has no hook for a session's end.
HOOK_EVENTS = ("SessionStart", "Stop", "UserPromptSubmit")
_EVENT_ARGUMENTS = {"SessionStart": [], "Stop": ["stop"], "UserPromptSubmit": ["prompt-submit"]}
_EVENT_WORDS = {"SessionStart": "session-start", "Stop": "end-of-turn", "UserPromptSubmit": "message"}
_TRUST_NAMES = {"SessionStart": "session_start", "Stop": "stop", "UserPromptSubmit": "user_prompt_submit"}
_EVENT_TIMEOUTS = {"SessionStart": HOOK_TIMEOUT_SECONDS, "Stop": 15, "UserPromptSubmit": 10}
_WRITE_ATTEMPTS = 3


class CodexAdapter(AgentAdapter):
    name = "codex"
    display_name = "Codex"

    def __init__(self, *, user_home: Path | None = None) -> None:
        if user_home is not None:
            self.home = user_home / ".codex"
        else:
            configured = os.environ.get("CODEX_HOME")
            self.home = Path(configured).expanduser() if configured else Path.home() / ".codex"
        self.config_path = self.home / "config.toml"
        self.skills_dir = self.home / "skills"
        self.hooks_path = self.home / "hooks.json"
        self._hooks = JsonFile(self.hooks_path)

    def installed(self) -> bool:
        return self.home.is_dir()

    @property
    def instructions_path(self) -> Path:
        override = self.home / "AGENTS.override.md"
        return override if override.is_file() else self.home / "AGENTS.md"

    def restart_hint(self) -> str:
        return (
            "Start Codex, open /hooks and trust the KnowItAll2 SessionStart, Stop, and UserPromptSubmit hooks once, "
            "then run `knowitall2 doctor` to confirm the registration."
        )

    def setup(self, launch: ServerLaunch) -> list[str]:
        if not self.installed():
            raise AgentError(f"Codex does not appear to be installed: {self.home} does not exist.")
        ensure_skill_installable(self.skills_dir)
        self._hooks.load()  # fail before any change if hooks.json is unreadable
        text = self._read_config()
        _parse(text, self.config_path)
        span = _block_span(text, self.config_path)
        newline = "\r\n" if "\r\n" in text else "\n"
        block = render_block(launch, newline)
        if span is None:
            if SERVER_NAME in (_parse(text, self.config_path).get("mcp_servers") or {}):
                raise AgentError(
                    f"{self.config_path} already defines mcp_servers.{SERVER_NAME} outside KnowItAll2's managed "
                    "block. Remove that entry, then run setup again."
                )
            if not text or text.endswith(newline * 2):
                separator = ""
            elif text.endswith(newline):
                separator = newline
            else:
                separator = newline * 2
            updated = text + separator + block + newline
        else:
            # Codex appends new tables (such as a hook's trust record) at the end of
            # the file, which can be inside this block; keep them, after the block.
            foreign = _foreign_lines(text[span[0]:span[1]], newline)
            kept = newline * 2 + newline.join(foreign) if foreign else ""
            updated = text[: span[0]] + block + kept + text[span[1]:]
        changes: list[str] = []
        if updated != text:
            parsed = _parse(updated, self.config_path, after_edit=True)
            if not launch_matches((parsed.get("mcp_servers") or {}).get(SERVER_NAME), launch):
                raise AgentError("The rendered Codex entry did not read back as expected; nothing was changed.")
            if _without_ours(parsed) != _without_ours(_parse(text, self.config_path)):
                raise AgentError("Editing the Codex settings would change more than KnowItAll2's entry; nothing was changed.")
            write_text_atomic(self.config_path, updated)
            changes.append(f"registered the knowitall2 MCP server in {self.config_path}")
        hook_change = self._install_hook(launch)
        if hook_change:
            changes.append(hook_change)
        skill_change = install_skill(self.skills_dir)
        if skill_change:
            changes.append(skill_change)
        instructions_change = instructions.install(self.instructions_path)
        if instructions_change:
            changes.append(instructions_change)
        return changes

    def uninstall(self) -> list[str]:
        changes: list[str] = []
        if self.config_path.is_file():
            text = self._read_config()
            span = _block_span(text, self.config_path)
            if span is not None:
                start, end = span
                newline = "\r\n" if "\r\n" in text else "\n"
                foreign = _foreign_lines(text[start:end], newline)
                if foreign:
                    updated = text[:start] + newline.join(foreign) + text[end:]
                else:
                    if text[end:].startswith(newline):
                        end += len(newline)
                    if text[:start].endswith(newline * 2):
                        start -= len(newline)
                    updated = text[:start] + text[end:]
                parsed = _parse(updated, self.config_path, after_edit=True)
                if _without_ours(parsed) != _without_ours(_parse(text, self.config_path)):
                    raise AgentError("Removing KnowItAll2 would change other Codex settings; nothing was changed.")
                if updated.strip():
                    write_text_atomic(self.config_path, updated)
                else:
                    # Only KnowItAll2's block was in the file; Codex treats a
                    # missing config the same as an empty one.
                    self.config_path.unlink()
                changes.append(f"removed the knowitall2 MCP server from {self.config_path}")
        hook_change = self._remove_hook()
        if hook_change:
            changes.append(hook_change)
        skill_change = remove_skill(self.skills_dir)
        if skill_change:
            changes.append(skill_change)
        instructions_change = instructions.remove(self.instructions_path)
        if instructions_change:
            changes.append(instructions_change)
        return changes

    def checks(self, launch: ServerLaunch) -> list[Check]:
        if not self.installed():
            return [Check("Codex", True, f"not installed ({self.home} not found); skipped")]
        try:
            text = self._read_config()
            parsed = _parse(text, self.config_path)
            span = _block_span(text, self.config_path)
        except AgentError as exc:
            return [Check("Codex config", False, str(exc), f"Fix {self.config_path}, then run: knowitall2 setup codex")]
        entry = (parsed.get("mcp_servers") or {}).get(SERVER_NAME)
        if span is None or entry is None:
            registration = Check("Codex registration", False, "KnowItAll2 is not registered", "Run: knowitall2 setup codex")
        elif not launch_matches(entry, launch):
            registration = Check(
                "Codex registration", False,
                "registered with a different Python or options than this installation",
                "Run: knowitall2 setup codex",
            )
        elif not Path(str(entry.get("command"))).is_file():
            registration = Check(
                "Codex registration", False, f"the registered Python is missing: {entry.get('command')}",
                "Run: knowitall2 setup codex",
            )
        else:
            registration = Check("Codex registration", True, f"registered in {self.config_path}")
        return [registration, self._hook_check(launch), skill_check(self, self.skills_dir),
                instructions.check(self.display_name, self.instructions_path, "Run: knowitall2 setup codex")]

    # Hooks ----------------------------------------------------------------

    def _install_hook(self, launch: ServerLaunch) -> str | None:
        launcher = hook_launcher_path()
        launcher_text = render_hook_launcher(launch, "codex")
        launcher_changed = not launcher.is_file() or launcher.read_text(encoding="utf-8") != launcher_text
        if launcher_changed:
            write_text_atomic(launcher, launcher_text)
        for _ in range(_WRITE_ATTEMPTS):
            text, data = self._hooks.load()
            if all(_find_hooks(_event_groups(data, event, self.hooks_path)) == [hook_entry(launch, event)]
                   for event in HOOK_EVENTS):
                return f"refreshed the KnowItAll2 session hook launcher at {launcher}" if launcher_changed else None
            json_object(data, "hooks", self.hooks_path)
            for event in HOOK_EVENTS:
                groups = _event_groups(data, event, self.hooks_path)
                _drop_our_hooks(groups)
                group: dict[str, Any] = {"matcher": HOOK_MATCHER} if event == "SessionStart" else {}
                groups.append({**group, "hooks": [hook_entry(launch, event)]})
                data.setdefault("hooks", {})[event] = groups
            if self._hooks.write_if_unchanged(text, data):
                persisted = self._hooks.load()[1]
                if any(_find_hooks(_event_groups(persisted, event, self.hooks_path)) != [hook_entry(launch, event)]
                       for event in HOOK_EVENTS):
                    raise AgentError(f"The KnowItAll2 session hooks did not persist in {self.hooks_path}; run setup again.")
                return f"added the KnowItAll2 session hooks to {self.hooks_path} (trust them once in Codex with /hooks)"
        raise AgentError(f"{self.hooks_path} kept changing while setup ran; close Codex and run setup again.")

    def _remove_hook(self) -> str | None:
        change = None
        if self.hooks_path.is_file():
            for _ in range(_WRITE_ATTEMPTS):
                text, data = self._hooks.load()
                if not any(_find_hooks(_event_groups(data, event, self.hooks_path)) for event in HOOK_EVENTS):
                    break
                hooks = data["hooks"]
                for event in HOOK_EVENTS:
                    groups = _event_groups(data, event, self.hooks_path)
                    _drop_our_hooks(groups)
                    if groups:
                        hooks[event] = groups
                    else:
                        hooks.pop(event, None)
                if not hooks:
                    data.pop("hooks")
                if self._hooks.write_if_unchanged(text, data):
                    change = f"removed the KnowItAll2 session hooks from {self.hooks_path}"
                    break
            else:
                raise AgentError(f"{self.hooks_path} kept changing; close Codex and run uninstall again.")
        hook_launcher_path().unlink(missing_ok=True)
        return change

    def _hook_check(self, launch: ServerLaunch) -> Check:
        name = "Codex session hook"
        try:
            data = self._hooks.load()[1]
            found = {event: _find_hooks(_event_groups(data, event, self.hooks_path)) for event in HOOK_EVENTS}
        except AgentError as exc:
            return Check(name, False, str(exc), "Fix or restore that file, then run setup again.")
        launcher = hook_launcher_path()
        for event in HOOK_EVENTS:
            if found[event] != [hook_entry(launch, event)]:
                what = _EVENT_WORDS[event]
                detail = f"the {what} hook is not installed" if not found[event] else (
                    f"the {what} hook is installed with different options than this installation")
                return Check(name, False, detail, "Run: knowitall2 setup codex")
        if not launcher.is_file() or launcher.read_text(encoding="utf-8") != render_hook_launcher(launch, "codex"):
            return Check(name, False, f"the hook launcher is missing or outdated: {launcher}", "Run: knowitall2 setup codex")
        untrusted = [_EVENT_WORDS[event] for event in HOOK_EVENTS if not self._hook_trusted(event)]
        if not untrusted:
            return Check(name, True, f"installed in {self.hooks_path} and trusted in Codex")
        noun = "hook once you trust it" if len(untrusted) == 1 else "hooks once you trust them"
        return Check(name, True, f"installed in {self.hooks_path}; Codex runs the {' and '.join(untrusted)} "
                                 f"{noun} with /hooks")

    def _hook_trusted(self, event: str = "SessionStart") -> bool:
        """Whether Codex recorded trust for the hook at KnowItAll2's position in hooks.json.

        Codex keys trust by file, event, group, and position, with a hash of the
        definition; a changed definition needs trusting again.
        """

        try:
            groups = _event_groups(self._hooks.load()[1], event, self.hooks_path)
            state = ((_parse(self._read_config(), self.config_path).get("hooks") or {}).get("state") or {})
        except AgentError:
            return False
        for group_index, group in enumerate(groups):
            hooks = group.get("hooks") if isinstance(group, dict) else None
            for hook_index, hook in enumerate(hooks if isinstance(hooks, list) else []):
                if _is_our_hook(hook):
                    position = f":{_TRUST_NAMES[event]}:{group_index}:{hook_index}"
                    return any(
                        isinstance(value, dict) and value.get("trusted_hash") and str(key).endswith(position)
                        and os.path.normcase(str(key)[: -len(position)]) == os.path.normcase(str(self.hooks_path))
                        for key, value in state.items()
                    )
        return False

    def _read_config(self) -> str:
        if not self.config_path.exists():
            return ""
        try:
            return self.config_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentError(f"cannot read {self.config_path}: {exc}") from exc


def hook_launcher_path() -> Path:
    return data_home() / "hooks" / HOOK_LAUNCHER


def hook_entry(launch: ServerLaunch, event: str = "SessionStart") -> dict[str, Any]:
    """The hook for this platform: a POSIX shell command, plus a Windows variant on Windows."""

    launcher = hook_launcher_path()
    extra = _EVENT_ARGUMENTS[event]
    entry: dict[str, Any] = {"type": "command", "command": shlex.join([launch.command, "-B", str(launcher), *extra])}
    if sys.platform == "win32":
        entry["commandWindows"] = " ".join([windows_command(launch.command, str(launcher)), *extra])
    if event == "SessionStart":
        entry.update({"timeout": HOOK_TIMEOUT_SECONDS, "statusMessage": HOOK_STATUS})
    else:
        # No status message: these hooks run on every turn and are quick.
        entry["timeout"] = _EVENT_TIMEOUTS[event]
    return entry


def windows_command(python: str, launcher: str | PurePath, *, shorten=None) -> str:
    """A command PowerShell and cmd both run as intended.

    Paths are written without quotes, using the short (8.3) form of any folder
    whose name has a space; file names keep their long form. Without short
    names, PowerShell's call operator is used, since Codex prefers PowerShell.
    """

    shorten = shorten or _short_path
    parts = [_without_spaces(PureWindowsPath(python), shorten), _without_spaces(PureWindowsPath(launcher), shorten)]
    if all(part is not None for part in parts):
        return f"{parts[0]} -B {parts[1]}"
    return f"& {_powershell_quote(python)} -B {_powershell_quote(str(launcher))}"


def _without_spaces(path: PureWindowsPath, shorten) -> str | None:
    if " " not in str(path):
        return str(path)
    if " " in path.name:
        return None
    folder = shorten(str(path.parent))
    if not folder or " " in folder:
        return None
    return str(PureWindowsPath(folder) / path.name)


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _is_our_hook(hook: object) -> bool:
    return isinstance(hook, dict) and any(HOOK_LAUNCHER in str(hook.get(key) or "") for key in ("command", "commandWindows"))


def _event_groups(data: dict[str, Any], event: str, path: Path) -> list[Any]:
    groups = json_object(data, "hooks", path).get(event)
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise AgentError(f"{path} has a {event} value that is not a list; nothing was changed.")
    return groups


def _find_hooks(groups: list[Any]) -> list[dict[str, Any]]:
    return [
        hook
        for group in groups if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for hook in group["hooks"] if _is_our_hook(hook)
    ]


def _drop_our_hooks(groups: list[Any]) -> None:
    for group in list(groups):
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            group["hooks"] = [hook for hook in group["hooks"] if not _is_our_hook(hook)]
            if not group["hooks"]:
                groups.remove(group)


def render_block(launch: ServerLaunch, newline: str = "\n") -> str:
    lines = [
        BEGIN,
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_toml_string(launch.command)}",
        "args = [" + ", ".join(_toml_string(argument) for argument in launch.args) + "]",
    ]
    if launch.env:
        lines.append(f"[mcp_servers.{SERVER_NAME}.env]")
        lines.extend(f"{key} = {_toml_string(value)}" for key, value in sorted(launch.env.items()))
    lines.append(END)
    return newline.join(lines)


_TABLE_HEADER = re.compile(r"^\s*\[\[?\s*(.+?)\s*\]\]?\s*(#.*)?$")


def _foreign_lines(block_text: str, newline: str) -> list[str]:
    """Lines inside KnowItAll2's markers that belong to other tables, without surrounding blank lines."""

    inner = block_text.split(newline)[1:-1]
    foreign: list[str] = []
    ours = True
    for line in inner:
        header = _TABLE_HEADER.match(line)
        if header:
            name = header.group(1)
            ours = name == f"mcp_servers.{SERVER_NAME}" or name.startswith(f"mcp_servers.{SERVER_NAME}.")
        if not ours:
            foreign.append(line)
    while foreign and not foreign[0].strip():
        foreign.pop(0)
    while foreign and not foreign[-1].strip():
        foreign.pop()
    return foreign


def _without_ours(parsed: dict) -> dict:
    """The parsed settings without KnowItAll2's entry, to prove an edit touched nothing else."""

    others = dict(parsed)
    servers = {name: value for name, value in (parsed.get("mcp_servers") or {}).items() if name != SERVER_NAME}
    if servers:
        others["mcp_servers"] = servers
    else:
        others.pop("mcp_servers", None)
    return others


def _toml_string(value: str) -> str:
    # A JSON string without ASCII escaping is a valid TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def _block_span(text: str, path: Path) -> tuple[int, int] | None:
    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        return None
    if begins != 1 or ends != 1 or text.index(END) < text.index(BEGIN):
        raise AgentError(f"{path} has a damaged KnowItAll2 block; restore it from a backup or remove the block by hand.")
    start = text.index(BEGIN)
    return start, text.index(END, start) + len(END)


def _parse(text: str, path: Path, *, after_edit: bool = False) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        if after_edit:
            raise AgentError(f"editing {path} would produce invalid TOML ({exc}); nothing was changed.") from exc
        raise AgentError(f"{path} is not valid TOML ({exc}); fix it before running setup.") from exc
