"""Shared pieces for agent adapters.

Adapters change an agent's configuration only inside entries, blocks, or
folders that KnowItAll2 owns. They validate every edited file before replacing
it and write atomically. Anything KnowItAll2 did not create is left alone, and
no copies of agent configuration are made, because it can hold other
servers' credentials.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .. import __version__
from ..files import write_text_atomic
from ..mcp_server import TOOLS
from ..paths import HOME_ENVIRONMENT_VARIABLE
from .skill import SKILL_MARKDOWN

SERVER_NAME = "knowitall2"
SKILL_NAME = "knowitall2"
MANAGED_MARKER = ".knowitall2-managed"


class AgentError(RuntimeError):
    """Setup or removal cannot proceed safely; the message says why and what to do."""


@dataclass(frozen=True)
class ServerLaunch:
    """How an agent should start the KnowItAll2 MCP server."""

    command: str
    args: tuple[str, ...]
    env: dict[str, str]


@dataclass(frozen=True)
class Check:
    """One line of ``doctor``. A check that is ``ok`` but has a ``fix`` is a note: nothing is broken, but
    something wants the user's look."""

    name: str
    ok: bool
    detail: str
    fix: str | None = None


def server_launch() -> ServerLaunch:
    """Launch the server with this Python, so agents use the same installation."""

    package_parent = Path(__file__).resolve().parents[2]
    env: dict[str, str] = {}
    if package_parent.name == "src" and (package_parent.parent / "pyproject.toml").is_file():
        # Running from a source checkout rather than an installed package.
        env["PYTHONPATH"] = str(package_parent)
    configured_home = os.environ.get(HOME_ENVIRONMENT_VARIABLE)
    if configured_home:
        env[HOME_ENVIRONMENT_VARIABLE] = str(Path(configured_home).expanduser().resolve())
    return ServerLaunch(command=console_python(), args=("-B", "-P", "-m", "knowitall2", "serve"), env=env)


def cli_command(*arguments: str) -> str:
    """How a person runs ``knowitall2 <arguments>`` in a terminal on this computer, with this installation.

    A clone has no ``knowitall2`` program on PATH, so this is the Python that
    agents are set up with, running the module, with the environment it needs
    on the same line: for PowerShell on Windows, and a POSIX shell elsewhere.
    """

    launch = server_launch()
    words = ["-P", "-m", "knowitall2", *arguments]
    if sys.platform == "win32":
        settings = "".join(f"$env:{name}={_powershell_literal(value)}; " for name, value in sorted(launch.env.items()))
        python = launch.command if _PLAIN_PATH.fullmatch(launch.command) else f"& {_powershell_literal(launch.command)}"
        return settings + " ".join([python, *words])
    settings = "".join(f"{name}={shlex.quote(value)} " for name, value in sorted(launch.env.items()))
    return settings + " ".join([shlex.quote(launch.command), *words])


_PLAIN_PATH = re.compile(r"[A-Za-z0-9._~:\\/-]+")


def _powershell_literal(value: str) -> str:
    # PowerShell also ends a single-quoted string at a typographic single quote; doubling keeps one.
    return "'" + re.sub("(['‘’‚‛])", r"\1\1", value) + "'"


def console_python() -> str:
    """This Python, as its console program: the app runs under ``pythonw.exe`` on Windows,
    but agents start the server with the ``python.exe`` beside it."""

    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").is_file():
        return str(executable.with_name("python.exe"))
    return sys.executable


class AgentAdapter:
    """One coding agent: register KnowItAll2, check the registration, remove it."""

    name = ""
    display_name = ""

    def installed(self) -> bool:
        raise NotImplementedError

    def preflight(self) -> None:
        """Raise :class:`AgentError` if setup could not finish, before setup (or a server connection) changes anything."""

    def setup(self, launch: ServerLaunch) -> list[str]:
        raise NotImplementedError

    def uninstall(self) -> list[str]:
        raise NotImplementedError

    def checks(self, launch: ServerLaunch) -> list[Check]:
        raise NotImplementedError

    def refresh_hook_launcher(self, launch: ServerLaunch) -> str | None:
        """Bring an existing, outdated hook launcher up to date; returns a change description or ``None``.

        Safe while the agent is open: see :func:`write_hook_launcher`.
        """

        return None

    def restart_hint(self) -> str:
        return f"Start a new {self.display_name} session to load KnowItAll2."

    def setup_command(self) -> str:
        """The command that sets this agent up again, as a person runs it here."""

        return cli_command("setup", self.name)


def was_set_up(adapter: AgentAdapter) -> bool:
    """Whether KnowItAll2 was set up for this agent here: the agent is installed and has the Skill folder."""

    return adapter.installed() and skill_state(adapter.skills_dir) != "missing"


class JsonFile:
    """A JSON settings file edited carefully: validated, written only if unchanged since read, and in its own style."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[str, dict[str, Any]]:
        try:
            text = self.path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return "", {}
        except (OSError, UnicodeDecodeError) as exc:
            raise AgentError(f"cannot read {self.path}: {exc}") from exc
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise AgentError(f"{self.path} is not valid JSON ({exc}); nothing was changed.") from exc
        if not isinstance(data, dict):
            raise AgentError(f"{self.path} does not contain a JSON object; nothing was changed.")
        return text, data

    def write_if_unchanged(self, original: str, data: dict[str, Any]) -> bool:
        current, _ = self.load()
        if current != original:
            return False
        if not data and self.path.is_file():
            # Nothing but KnowItAll2's entry was in the file; agents treat a
            # missing settings file the same as an empty one.
            self.path.unlink()
            return True
        write_text_atomic(self.path, render_json_like(original, data))
        return True


_INDENT = re.compile(r"\n([ \t]+)\S")
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_JSON_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.DOTALL)


def render_json_like(original: str, data: dict[str, Any]) -> str:
    """``data`` as JSON in the style of ``original``: its line endings, indentation, escaping of
    non-ASCII text, and final newline. A new or empty file gets a two-space indent."""

    newline = "\r\n" if "\r\n" in original else "\n"
    indent: int | str | None = 2
    separators = None
    found = _INDENT.search(original)
    if found:
        indent = found.group(1)
    elif original.strip() and "\n" not in original.strip() and json.loads(original):
        # A one-line file stays on one line, with or without spaces after its commas and colons.
        indent = None
        separators = (", ", ": ") if " " in _JSON_STRING.sub("", original.strip()) else (",", ":")
    escaped = any(match.group(1).startswith("u") and int(match.group(1)[1:], 16) > 0x7F
                  for match in _JSON_ESCAPE.finditer(original))
    ensure_ascii = escaped and original.isascii()
    rendered = json.dumps(data, indent=indent, separators=separators, ensure_ascii=ensure_ascii)
    if newline != "\n":
        # JSON escapes line breaks inside strings, so every one here is between values.
        rendered = rendered.replace("\n", newline)
    if original.endswith("\n") or not original:
        rendered += newline
    return rendered


def json_object(data: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentError(f"{path} has a {key} value that is not an object; nothing was changed.")
    return value


def render_hook_launcher(launch: ServerLaunch, agent: str) -> str:
    """A tiny script, kept in the data home, that runs a KnowItAll2 hook for ``agent``.

    The hook event is its argument (``stop``, ``session-end``); without one it
    is ``session-start``, as registered by earlier versions. It exits 0
    whatever happens, even when KnowItAll2 cannot be imported or the hook
    raises, so a hook can never stop or trouble the agent's session.
    """

    lines = [
        f'"""Generated by `knowitall2 setup {agent}`: runs the KnowItAll2 hooks."""',
        "import os",
        "import sys",
        "",
    ]
    if "PYTHONPATH" in launch.env:
        lines.append(f"sys.path.insert(0, {launch.env['PYTHONPATH']!r})")
    if HOME_ENVIRONMENT_VARIABLE in launch.env:
        lines.append(f"os.environ.setdefault({HOME_ENVIRONMENT_VARIABLE!r}, {launch.env[HOME_ENVIRONMENT_VARIABLE]!r})")
    lines += [
        "try:",
        "    from knowitall2.hooks import main",
        "",
        f'    main(["{agent}", sys.argv[1] if len(sys.argv) > 1 else "session-start"])',
        "except BaseException:",
        "    pass  # a hook never fails the agent's session",
        "raise SystemExit(0)",
        "",
    ]
    return "\n".join(lines)


def write_hook_launcher(path: Path, launch: ServerLaunch, agent: str, *, create: bool = True) -> bool:
    """Write the hook launcher unless it is current (or, without ``create``, missing); True if written.

    The launcher is KnowItAll2's own file in its data home, and the agent's hook
    command does not change with it, so it can be rewritten while the agent is
    open and nothing needs trusting again.
    """

    text = render_hook_launcher(launch, agent)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        if not create:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    write_text_atomic(path, text)
    return True


def ensure_skill_installable(skills_dir: Path) -> None:
    """Fail before any change if the Skill folder belongs to someone else."""

    if skill_state(skills_dir) == "foreign":
        raise AgentError(
            f"{skills_dir / SKILL_NAME} already exists and was not created by KnowItAll2. "
            "Rename or remove it, then run setup again."
        )


def install_skill(skills_dir: Path) -> str | None:
    """Install or refresh the KnowItAll2 Skill; returns a change description or ``None``."""

    ensure_skill_installable(skills_dir)
    target = skills_dir / SKILL_NAME
    marker = target / MANAGED_MARKER
    if skill_state(skills_dir) == "current":
        return None
    target.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target / "SKILL.md", SKILL_MARKDOWN)
    write_text_atomic(marker, _marker_text())
    return f"installed the knowitall2 Skill in {target}"


def remove_skill(skills_dir: Path) -> str | None:
    target = skills_dir / SKILL_NAME
    marker = target / MANAGED_MARKER
    if not target.exists():
        return None
    if not marker.is_file():
        return None
    for name in ("SKILL.md", MANAGED_MARKER):
        (target / name).unlink(missing_ok=True)
    try:
        target.rmdir()
    except OSError:
        return f"removed the knowitall2 Skill files; left {target} because it contains other files"
    return f"removed the knowitall2 Skill from {target}"


def skill_state(skills_dir: Path) -> str:
    """``current``, ``outdated``, ``missing``, or ``foreign`` (not created by KnowItAll2)."""

    target = skills_dir / SKILL_NAME
    if not target.exists():
        return "missing"
    if not (target / MANAGED_MARKER).is_file():
        return "foreign"
    try:
        current = (target / "SKILL.md").read_text(encoding="utf-8") == SKILL_MARKDOWN
    except OSError:
        current = False
    return "current" if current else "outdated"


def skill_check(adapter: "AgentAdapter", skills_dir: Path) -> Check:
    name = f"{adapter.display_name} Skill"
    state = skill_state(skills_dir)
    if state == "current":
        return Check(name, True, f"current in {skills_dir / SKILL_NAME}")
    if state == "foreign":
        return Check(
            name, False, f"{skills_dir / SKILL_NAME} exists but was not created by KnowItAll2",
            "Rename or remove that folder, then run setup again.",
        )
    return Check(name, False, state, f"Run: {adapter.setup_command()}")


def launch_matches(entry: object, launch: ServerLaunch) -> bool:
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("command") == launch.command
        and list(entry.get("args") or []) == list(launch.args)
        and dict(entry.get("env") or {}) == launch.env
    )


def probe_server(launch: ServerLaunch, *, timeout: float = 20.0) -> Check:
    """Start the server exactly as an agent would and complete an MCP handshake."""

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "knowitall2-doctor"}, "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    payload = "".join(json.dumps(message) + "\n" for message in messages).encode("utf-8")
    environment = dict(os.environ)
    environment.update(launch.env)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            [launch.command, *launch.args], input=payload, capture_output=True,
            timeout=timeout, env=environment, creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("server starts", False, f"the server did not run: {exc}", "Reinstall KnowItAll2, then run setup again.")
    responses = {}
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and "id" in message:
            responses[message["id"]] = message
    server = (responses.get(1) or {}).get("result", {}).get("serverInfo", {})
    tools = [tool.get("name") for tool in (responses.get(2) or {}).get("result", {}).get("tools", [])]
    if server.get("name") != SERVER_NAME or sorted(tools) != sorted(tool["name"] for tool in TOOLS):
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["no MCP handshake"]
        return Check("server starts", False, f"unexpected server response ({detail[0]})", "Reinstall KnowItAll2, then run setup again.")
    return Check("server starts", True, f"MCP handshake completed; version {server.get('version')}, {len(tools)} tools")


def describe_checks(checks: Sequence[Check]) -> str:
    lines = []
    for check in checks:
        status = "FAIL" if not check.ok else "NOTE" if check.fix else "OK  "
        lines.append(f"{status} {check.name}: {check.detail}")
        if check.fix:
            lines.append(f"     fix: {check.fix}")
    return "\n".join(lines)


def _marker_text() -> str:
    return f"schema_version=1\nsource=knowitall2\nversion={__version__}\n"
