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
  quoting: it uses the short form of folders whose names contain spaces. When
  a path has a character that one of those shells would misread (such as
  ``'`` or ``&``), no command works in both, and the hooks are left out.
  Codex runs a hook only after the user has trusted it once with ``/hooks``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import string
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
    cli_command,
    ensure_skill_installable,
    install_skill,
    json_object,
    launch_matches,
    remove_skill,
    render_hook_launcher,
    skill_check,
    write_hook_launcher,
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
        self._hooks_skipped: str | None = None

    def installed(self) -> bool:
        return self.home.is_dir()

    @property
    def instructions_path(self) -> Path:
        override = self.home / "AGENTS.override.md"
        return override if override.is_file() else self.home / "AGENTS.md"

    def restart_hint(self) -> str:
        doctor = f"then run `{cli_command('doctor')}` to confirm the registration."
        if self._hooks_skipped:
            return f"Start a new Codex session to load KnowItAll2, {doctor}"
        return ("Start Codex, open /hooks and trust the KnowItAll2 SessionStart, Stop, and UserPromptSubmit hooks once, "
                + doctor)

    def preflight(self) -> None:
        if not self.installed():
            raise AgentError(f"Codex does not appear to be installed: {self.home} does not exist.")
        ensure_skill_installable(self.skills_dir)
        hooks = self._hooks.load()[1]
        for event in HOOK_EVENTS:
            _event_groups(hooks, event, self.hooks_path)
        text = self._read_config()
        parsed = _parse(text, self.config_path)
        if _block_span(text, self.config_path) is None:
            _foreign_server_check(parsed, self.config_path)
        instructions.ensure_editable(self.instructions_path)

    def setup(self, launch: ServerLaunch) -> list[str]:
        self.preflight()
        changes = [change for change in (self._register_server(launch), self._install_hook(launch)) if change]
        skill_change = install_skill(self.skills_dir)
        if skill_change:
            changes.append(skill_change)
        instructions_change = instructions.install(self.instructions_path)
        if instructions_change:
            changes.append(instructions_change)
        return changes

    def uninstall(self) -> list[str]:
        # Codex reads only AGENTS.override.md when there is one, but the block may also be
        # in AGENTS.md from before that file appeared: remove it from both.
        instruction_files = [self.home / "AGENTS.override.md", self.home / "AGENTS.md"]
        for path in instruction_files:
            instructions.ensure_editable(path)
        changes = [change for change in (self._unregister_server(), self._remove_hook()) if change]
        skill_change = remove_skill(self.skills_dir)
        if skill_change:
            changes.append(skill_change)
        for path in instruction_files:
            instructions_change = instructions.remove(path)
            if instructions_change:
                changes.append(instructions_change)
        return changes

    def _register_server(self, launch: ServerLaunch) -> str | None:
        for _ in range(_WRITE_ATTEMPTS):
            text = self._read_config()
            original = _parse(text, self.config_path)
            span = _block_span(text, self.config_path)
            newline = "\r\n" if "\r\n" in text else "\n"
            block = render_block(launch, newline)
            if span is None:
                _foreign_server_check(original, self.config_path)
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
            if updated == text:
                return None
            parsed = _parse(updated, self.config_path, after_edit=True)
            if not launch_matches((parsed.get("mcp_servers") or {}).get(SERVER_NAME), launch):
                raise AgentError("The rendered Codex entry did not read back as expected; nothing was changed.")
            if _without_ours(parsed) != _without_ours(original):
                raise AgentError("Editing the Codex settings would change more than KnowItAll2's entry; nothing was changed.")
            # Codex may have saved its settings meanwhile; then start again from what it wrote.
            if self._read_config() == text:
                write_text_atomic(self.config_path, updated)
                return f"registered the knowitall2 MCP server in {self.config_path}"
        raise AgentError(f"{self.config_path} kept changing while setup ran; close Codex and run setup again.")

    def _unregister_server(self) -> str | None:
        for _ in range(_WRITE_ATTEMPTS):
            if not self.config_path.is_file():
                return None
            text = self._read_config()
            span = _block_span(text, self.config_path)
            if span is None:
                return None
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
            if self._read_config() != text:
                continue
            if updated.strip():
                write_text_atomic(self.config_path, updated)
            else:
                # Only KnowItAll2's block was in the file; Codex treats a
                # missing config the same as an empty one.
                self.config_path.unlink()
            return f"removed the knowitall2 MCP server from {self.config_path}"
        raise AgentError(f"{self.config_path} kept changing; close Codex and run uninstall again.")

    def checks(self, launch: ServerLaunch) -> list[Check]:
        if not self.installed():
            return [Check("Codex", True, f"not installed ({self.home} not found); skipped")]
        try:
            text = self._read_config()
            parsed = _parse(text, self.config_path)
            span = _block_span(text, self.config_path)
        except AgentError as exc:
            return [Check("Codex config", False, str(exc), f"Fix {self.config_path}, then run: {self.setup_command()}")]
        entry = (parsed.get("mcp_servers") or {}).get(SERVER_NAME)
        if span is None or entry is None:
            registration = Check("Codex registration", False, "KnowItAll2 is not registered", f"Run: {self.setup_command()}")
        elif not launch_matches(entry, launch):
            registration = Check(
                "Codex registration", False,
                "registered with a different Python or options than this installation",
                f"Run: {self.setup_command()}",
            )
        elif not Path(str(entry.get("command"))).is_file():
            registration = Check(
                "Codex registration", False, f"the registered Python is missing: {entry.get('command')}",
                f"Run: {self.setup_command()}",
            )
        else:
            registration = Check("Codex registration", True, f"registered in {self.config_path}")
        return [registration, self._hook_check(launch), skill_check(self, self.skills_dir),
                instructions.check(self.display_name, self.instructions_path, f"Run: {self.setup_command()}")]

    # Hooks ----------------------------------------------------------------

    def refresh_hook_launcher(self, launch: ServerLaunch) -> str | None:
        launcher = hook_launcher_path()
        if write_hook_launcher(launcher, launch, "codex", create=False):
            return f"refreshed the KnowItAll2 session hook launcher at {launcher}"
        return None

    def _install_hook(self, launch: ServerLaunch) -> str | None:
        self._hooks_skipped = hooks_unavailable(launch)
        if self._hooks_skipped:
            # A hook command that one of Codex's shells would misread could run something else.
            removed = self._remove_hook()
            skipped = (f"did not add the KnowItAll2 session hooks: {self._hooks_skipped}. Codex still reaches "
                       "KnowItAll2 through its tools, but without the automatic briefing or learning after a commit")
            return f"{removed}; {skipped}" if removed else skipped
        launcher = hook_launcher_path()
        launcher_changed = write_hook_launcher(launcher, launch, "codex")
        for _ in range(_WRITE_ATTEMPTS):
            text, data = self._hooks.load()
            if all(_find_hooks(_event_groups(data, event, self.hooks_path)) == [hook_entry(launch, event)]
                   for event in HOOK_EVENTS):
                return f"refreshed the KnowItAll2 session hook launcher at {launcher}" if launcher_changed else None
            json_object(data, "hooks", self.hooks_path)
            for event in HOOK_EVENTS:
                groups = _event_groups(data, event, self.hooks_path)
                fields = {"matcher": HOOK_MATCHER} if event == "SessionStart" else {}
                _put_our_hook(groups, hook_entry(launch, event), fields)
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
        skipped = hooks_unavailable(launch)
        if skipped and any(found.values()):
            return Check(name, False, f"the hooks registered cannot run as intended: {skipped}",
                         f"Run: {self.setup_command()}")
        if skipped:
            return Check(name, True, f"not added: {skipped}. Codex reaches KnowItAll2 through its tools, without the "
                                     "automatic briefing or learning after a commit")
        launcher = hook_launcher_path()
        for event in HOOK_EVENTS:
            if found[event] != [hook_entry(launch, event)]:
                what = _EVENT_WORDS[event]
                detail = f"the {what} hook is not installed" if not found[event] else (
                    f"the {what} hook is installed with different options than this installation")
                return Check(name, False, detail, f"Run: {self.setup_command()}")
        if not launcher.is_file() or launcher.read_text(encoding="utf-8") != render_hook_launcher(launch, "codex"):
            return Check(name, False, f"the hook launcher is missing or outdated: {launcher}", f"Run: {self.setup_command()}")
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
        command = windows_command(launch.command, str(launcher))
        if command is None:
            raise AgentError(f"No Windows command runs the KnowItAll2 hook: {hooks_unavailable(launch)}.")
        entry["commandWindows"] = " ".join([command, *extra])
    if event == "SessionStart":
        entry.update({"timeout": HOOK_TIMEOUT_SECONDS, "statusMessage": HOOK_STATUS})
    else:
        # No status message: these hooks run on every turn and are quick.
        entry["timeout"] = _EVENT_TIMEOUTS[event]
    return entry


def windows_command(python: str, launcher: str | PurePath, *, shorten=None) -> str | None:
    """A command PowerShell and cmd both run as intended, or None when there is none.

    Paths are written without quotes, using the short (8.3) form of any folder
    whose name has a space; file names keep their long form. Without short
    names, PowerShell's call operator is used, since Codex prefers PowerShell.
    Any other character must be plain to both shells: short names keep
    characters such as ' & $ % ( ), which PowerShell or cmd reads as part of
    the command, and no quoting works in both.
    """

    shorten = shorten or _short_path
    parts = [_without_spaces(PureWindowsPath(python), shorten), _without_spaces(PureWindowsPath(launcher), shorten)]
    if all(part is not None for part in parts):
        # What counts is the command as written: "Program Files (x86)" is C:\PROGRA~2, which both shells run.
        return f"{parts[0]} -B {parts[1]}" if all(_plain(part) for part in parts) else None
    if not all(_plain(str(path).replace(" ", "")) for path in (python, launcher)):
        return None
    return f"& {_powershell_quote(python)} -B {_powershell_quote(str(launcher))}"


def hooks_unavailable(launch: ServerLaunch) -> str | None:
    """Why the session hooks cannot be registered on this computer, or None when they can."""

    if sys.platform != "win32":
        return None
    launcher = str(hook_launcher_path())
    if windows_command(launch.command, launcher) is not None:
        return None
    misread = [path for path in (launch.command, launcher) if not _plain(path.replace(" ", ""))] or [launch.command]
    return (f"{' and '.join(misread)} {'contains' if len(misread) == 1 else 'contain'} a character (such as ', &, $ "
            "or %) that PowerShell or cmd would read as part of the command, and no hook command works in both")


# What PowerShell and cmd both read as part of a plain word, besides letters and digits outside ASCII.
_PLAIN = frozenset(string.ascii_letters + string.digits + "._~:\\-")


def _plain(text: str) -> bool:
    return all(character in _PLAIN or (not character.isascii() and character.isalnum()) for character in text)


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


def _put_our_hook(groups: list[Any], hook: dict[str, Any], fields: dict[str, Any]) -> None:
    """Put KnowItAll2's hook where it already is, or in a new group at the end when there is none.

    Codex keys its trust in each hook by its group's and its own position, so
    replacing the hook in place keeps the user's hook groups after it where
    they were, and trusted. A second copy of KnowItAll2's hook is removed.
    """

    placed = False
    index = 0
    while index < len(groups):
        group = groups[index]
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(hooks, list) and any(_is_our_hook(item) for item in hooks):
            if placed:
                group["hooks"] = [item for item in hooks if not _is_our_hook(item)]
            elif all(_is_our_hook(item) for item in hooks):
                group = groups[index] = {**group, **fields, "hooks": [hook]}
            else:
                position = next(number for number, item in enumerate(hooks) if _is_our_hook(item))
                group["hooks"] = [item for item in hooks if not _is_our_hook(item)]
                group["hooks"].insert(position, hook)
            placed = True
            if not group["hooks"]:
                del groups[index]
                continue
        index += 1
    if not placed:
        groups.append({**fields, "hooks": [hook]})


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


def _foreign_server_check(parsed: dict, path: Path) -> None:
    """Refuse a knowitall2 server entry outside KnowItAll2's managed block."""

    if SERVER_NAME in (parsed.get("mcp_servers") or {}):
        raise AgentError(
            f"{path} already defines mcp_servers.{SERVER_NAME} outside KnowItAll2's managed "
            "block. Remove that entry, then run setup again."
        )


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
