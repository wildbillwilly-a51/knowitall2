"""Claude Code adapter: the MCP server, the Skill, the session hooks, and a block in CLAUDE.md.

- The MCP server is a user-scope entry under ``mcpServers`` in ``.claude.json``,
  which the CLI and the desktop app's Code tab share.
- The session hooks (SessionStart, Stop, SessionEnd, UserPromptSubmit, and PostToolUseFailure
  for shell commands) are exec-form entries in
  ``settings.json`` that run a small launcher in the KnowItAll2 data home.
  Exec form (``command`` plus ``args``) involves no shell, so paths with
  spaces need no quoting whether Claude Code would use Git Bash or PowerShell.

- A short block in the user's global ``CLAUDE.md`` (``agents.instructions``)
  makes KnowItAll2 part of how every chat orients itself.

Both settings files may be written by a running Claude Code, so KnowItAll2 changes only
its own entries, re-reads each file immediately before writing, and verifies
the result; ``doctor`` detects a lost entry.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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
)

_WRITE_ATTEMPTS = 3
HOOK_LAUNCHER = "claude_session_start.py"
HOOK_TIMEOUT_SECONDS = 30
_JsonFile = JsonFile
_object = json_object


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude-code"
    display_name = "Claude Code"

    def __init__(self, *, user_home: Path | None = None) -> None:
        configured = os.environ.get("CLAUDE_CONFIG_DIR") if user_home is None else None
        if configured:
            self.config_dir = Path(configured).expanduser()
            self.config_path = self.config_dir / ".claude.json"
        else:
            home = user_home if user_home is not None else Path.home()
            self.config_dir = home / ".claude"
            self.config_path = home / ".claude.json"
        self.settings_path = self.config_dir / "settings.json"
        self.skills_dir = self.config_dir / "skills"
        self.instructions_path = self.config_dir / "CLAUDE.md"
        self._config = _JsonFile(self.config_path)
        self._settings = _JsonFile(self.settings_path)

    def installed(self) -> bool:
        return self.config_path.is_file() or self.config_dir.is_dir()

    def restart_hint(self) -> str:
        return f"Quit and reopen Claude Code, then run `{cli_command('doctor')}` to confirm it kept the registration."

    def preflight(self) -> None:
        if not self.config_path.is_file():
            raise AgentError(
                f"{self.config_path} was not found. Open Claude Code once so it creates its settings, "
                "then run setup again."
            )
        ensure_skill_installable(self.skills_dir)
        _foreign_server_check(_object(self._config.load()[1], "mcpServers", self.config_path), self.config_path)
        settings = self._settings.load()[1]
        for event in HOOK_EVENTS:
            _event_groups(settings, event, self.settings_path)
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
        instructions.ensure_editable(self.instructions_path)  # fail before any change, as setup does
        changes = [change for change in (self._unregister_server(), self._remove_hook()) if change]
        skill_change = remove_skill(self.skills_dir)
        if skill_change:
            changes.append(skill_change)
        instructions_change = instructions.remove(self.instructions_path)
        if instructions_change:
            changes.append(instructions_change)
        return changes

    def checks(self, launch: ServerLaunch) -> list[Check]:
        if not self.installed():
            return [Check("Claude Code", True, f"not installed ({self.config_path} not found); skipped")]
        return [self._server_check(launch), self._hook_check(launch), skill_check(self, self.skills_dir),
                instructions.check(self.display_name, self.instructions_path, f"Run: {self.setup_command()}")]

    # MCP server registration ------------------------------------------------

    def _register_server(self, launch: ServerLaunch) -> str | None:
        entry = {"type": "stdio", "command": launch.command, "args": list(launch.args), "env": dict(launch.env)}
        for _ in range(_WRITE_ATTEMPTS):
            text, data = self._config.load()
            servers = _object(data, "mcpServers", self.config_path)
            existing = _foreign_server_check(servers, self.config_path)
            if existing == entry:
                return None
            data.setdefault("mcpServers", servers)[SERVER_NAME] = entry
            if self._config.write_if_unchanged(text, data):
                persisted = _object(self._config.load()[1], "mcpServers", self.config_path).get(SERVER_NAME)
                if not launch_matches(persisted, launch):
                    raise AgentError(f"The KnowItAll2 entry did not persist in {self.config_path}; run setup again.")
                return f"registered the knowitall2 MCP server in {self.config_path}"
        raise AgentError(f"{self.config_path} kept changing while setup ran; close Claude Code and run setup again.")

    def _unregister_server(self) -> str | None:
        if not self.config_path.is_file():
            return None
        for _ in range(_WRITE_ATTEMPTS):
            text, data = self._config.load()
            servers = _object(data, "mcpServers", self.config_path)
            if not _is_knowitall2_server(servers.get(SERVER_NAME)):
                return None
            del servers[SERVER_NAME]
            if self._config.write_if_unchanged(text, data):
                return f"removed the knowitall2 MCP server from {self.config_path}"
        raise AgentError(f"{self.config_path} kept changing; close Claude Code and run uninstall again.")

    def _server_check(self, launch: ServerLaunch) -> Check:
        name = "Claude Code registration"
        try:
            entry = _object(self._config.load()[1], "mcpServers", self.config_path).get(SERVER_NAME)
        except AgentError as exc:
            return Check("Claude Code settings", False, str(exc), "Fix or restore that file, then run setup again.")
        if entry is None:
            return Check(name, False, "KnowItAll2 is not registered", f"Close Claude Code, then run: {self.setup_command()}")
        if not _is_knowitall2_server(entry):
            return Check(name, False, f"an MCP server named {SERVER_NAME} exists but is not KnowItAll2's",
                         f"Remove that entry, then run: {self.setup_command()}")
        if not launch_matches(entry, launch):
            return Check(name, False, "registered with a different Python or options than this installation",
                         f"Run: {self.setup_command()}")
        if not Path(str(entry.get("command"))).is_file():
            return Check(name, False, f"the registered Python is missing: {entry.get('command')}",
                         f"Run: {self.setup_command()}")
        return Check(name, True, f"registered in {self.config_path}")

    # Hooks ----------------------------------------------------------------

    def refresh_hook_launcher(self, launch: ServerLaunch) -> str | None:
        launcher = hook_launcher_path()
        if write_hook_launcher(launcher, launch, "claude-code", create=False):
            return f"refreshed the KnowItAll2 session hook launcher at {launcher}"
        return None

    def _install_hook(self, launch: ServerLaunch) -> str | None:
        launcher = hook_launcher_path()
        launcher_changed = write_hook_launcher(launcher, launch, "claude-code")
        for _ in range(_WRITE_ATTEMPTS):
            text, data = self._settings.load()
            if all(_find_hooks(_event_groups(data, event, self.settings_path)) == [hook_entry(launch, event)]
                   for event in HOOK_EVENTS):
                return f"refreshed the KnowItAll2 session hook launcher at {launcher}" if launcher_changed else None
            for event in HOOK_EVENTS:
                groups = _event_groups(data, event, self.settings_path)
                _drop_our_hooks(groups)
                groups.append({"matcher": _EVENT_MATCHERS.get(event, ""), "hooks": [hook_entry(launch, event)]})
                data.setdefault("hooks", {})[event] = groups
            if self._settings.write_if_unchanged(text, data):
                persisted = self._settings.load()[1]
                if any(_find_hooks(_event_groups(persisted, event, self.settings_path)) != [hook_entry(launch, event)]
                       for event in HOOK_EVENTS):
                    raise AgentError(f"The KnowItAll2 session hooks did not persist in {self.settings_path}; run setup again.")
                return f"added the KnowItAll2 session hooks to {self.settings_path}"
        raise AgentError(f"{self.settings_path} kept changing while setup ran; close Claude Code and run setup again.")

    def _remove_hook(self) -> str | None:
        change = None
        if self.settings_path.is_file():
            for _ in range(_WRITE_ATTEMPTS):
                text, data = self._settings.load()
                if not any(_find_hooks(_event_groups(data, event, self.settings_path)) for event in HOOK_EVENTS):
                    break
                hooks = data["hooks"]
                for event in HOOK_EVENTS:
                    groups = _event_groups(data, event, self.settings_path)
                    _drop_our_hooks(groups)
                    if groups:
                        hooks[event] = groups
                    else:
                        hooks.pop(event, None)
                if not hooks:
                    data.pop("hooks")
                if self._settings.write_if_unchanged(text, data):
                    change = f"removed the KnowItAll2 session hooks from {self.settings_path}"
                    break
            else:
                raise AgentError(f"{self.settings_path} kept changing; close Claude Code and run uninstall again.")
        hook_launcher_path().unlink(missing_ok=True)
        return change

    def _hook_check(self, launch: ServerLaunch) -> Check:
        name = "Claude Code session hook"
        try:
            data = self._settings.load()[1]
            found = {event: _find_hooks(_event_groups(data, event, self.settings_path)) for event in HOOK_EVENTS}
        except AgentError as exc:
            return Check(name, False, str(exc), "Fix or restore that file, then run setup again.")
        launcher = hook_launcher_path()
        for event in HOOK_EVENTS:
            if found[event] != [hook_entry(launch, event)]:
                what = _EVENT_WORDS[event]
                detail = f"the {what} hook is not installed" if not found[event] else (
                    f"the {what} hook is installed with different options than this installation")
                return Check(name, False, detail, f"Run: {self.setup_command()}")
        if not launcher.is_file() or launcher.read_text(encoding="utf-8") != render_launcher(launch):
            return Check(name, False, f"the hook launcher is missing or outdated: {launcher}",
                         f"Run: {self.setup_command()}")
        return Check(name, True, f"installed in {self.settings_path}")


# Session start adds the briefing; a turn's end learns after a commit and shows
# what was learned; the session's end learns the rest; the user's message
# carries news to the agent where the app does not show hook messages. After a
# Bash or PowerShell command fails, what is known about that command (``commands``).
HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd", "UserPromptSubmit", "PostToolUseFailure")
_EVENT_ARGUMENTS = {"SessionStart": [], "Stop": ["stop"], "SessionEnd": ["session-end"],
                    "UserPromptSubmit": ["prompt-submit"], "PostToolUseFailure": ["command"]}
_EVENT_WORDS = {"SessionStart": "session-start", "Stop": "end-of-turn", "SessionEnd": "session-end",
                "UserPromptSubmit": "message", "PostToolUseFailure": "failed-command"}
_EVENT_TIMEOUTS = {"SessionStart": HOOK_TIMEOUT_SECONDS, "Stop": 15, "SessionEnd": 15, "UserPromptSubmit": 10,
                   "PostToolUseFailure": 5}
_EVENT_MATCHERS = {"PostToolUseFailure": "Bash|PowerShell"}


def hook_launcher_path() -> Path:
    return data_home() / "hooks" / HOOK_LAUNCHER


def hook_entry(launch: ServerLaunch, event: str = "SessionStart") -> dict[str, Any]:
    """Exec form: the interpreter and its arguments, with no shell in between."""

    return {"type": "command", "command": launch.command,
            "args": ["-B", str(hook_launcher_path()), *_EVENT_ARGUMENTS[event]], "timeout": _EVENT_TIMEOUTS[event]}


def render_launcher(launch: ServerLaunch) -> str:
    return render_hook_launcher(launch, "claude-code")


def _event_groups(data: dict[str, Any], event: str, path: Path) -> list[Any]:
    groups = _object(data, "hooks", path).get(event)
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise AgentError(f"{path} has a {event} value that is not a list; nothing was changed.")
    return groups


def _is_our_hook(hook: object) -> bool:
    if not isinstance(hook, dict) or not isinstance(hook.get("args"), list):
        return False
    return any(
        isinstance(argument, str) and Path(argument).name == HOOK_LAUNCHER and Path(argument).parent.name == "hooks"
        for argument in hook["args"]
    )


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


def _foreign_server_check(servers: dict[str, Any], path: Path) -> object:
    """KnowItAll2's existing entry (or None); refuses an entry of that name that KnowItAll2 did not create."""

    existing = servers.get(SERVER_NAME)
    if existing is not None and not _is_knowitall2_server(existing):
        raise AgentError(
            f"{path} already has an MCP server named {SERVER_NAME} that KnowItAll2 did not "
            "create. Remove it, then run setup again."
        )
    return existing


def _is_knowitall2_server(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    args = entry.get("args")
    return isinstance(args, list) and "knowitall2" in args and "serve" in args
