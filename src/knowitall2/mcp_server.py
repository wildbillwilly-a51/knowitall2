"""MCP server over stdio that gives any coding agent access to KnowItAll2.

The server never lets a failure reach the agent's task: storage problems and
bad arguments come back as tool results the agent can read and act on, and the
process keeps serving.

Agents start ``knowitall2 serve``, which is the thin front in ``front``: it
lives as long as the agent's session and hands each request to a fresh
``knowitall2 serve-once`` process, which answers it with this module, so an
update reaches open sessions on their next request. ``run_stdio`` serves
directly from one process, for tests and tools that want that.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any, BinaryIO, Callable

from . import __version__, journal, review
from .memory import KINDS, MAX_RECALL_LIMIT, RECALL_SCOPES, SCOPES, SOURCES, Memory, MemoryInputError
from .paths import data_home, database_path
from .store import Store, StoreError

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
# Tools whose answer also gives what is new to the chat since its briefing (``hooks.tool_news``).
NEWS_TOOLS = ("recall", "remember")

INSTRUCTIONS = (
    "KnowItAll2 is the user's long-term memory for coding work: projects, systems, infrastructure, "
    "procedures, decisions, lessons, and the user's own rules. At the start of a session, call `briefing` "
    "with project_path set to the workspace root, unless a KnowItAll2 briefing is already in your context. "
    "Before rediscovering anything that may have been learned "
    "before (a host, service, route, credential location, procedure, or past decision), call `recall` with a "
    "few keywords. When you establish a durable fact that would help a future session, or the user tells you "
    "one (a system, a method, a rule), call `remember` right away and tell the user in one short line what was "
    "saved; set source to 'user' only for the user's own words. Never save secrets; save where a credential is "
    "kept instead. When the user asks KnowItAll2 to learn from the session, or once when a task the user gave you "
    "is complete, call `learn`. Treat unverified memories as leads to check. When the briefing mentions questions for the user, "
    "show them with `questions` at a convenient moment and record only the user's own choices with `answer`. "
    "A briefing or recall result may include an optional KnowItAll2 request with a task id such as t-1a2b3c4d; "
    "help only if it is quick, look-only, and not in the way of the user's task, and report with `settle`. "
    "When the user asks to update KnowItAll2, run the update script `update.py` in the `.knowitall2` folder of "
    "the user's home with Python 3.12 or later, and show the user its output. "
    "If KnowItAll2 is unavailable, continue normally."
)

_PROJECT_PATH = {
    "type": "string",
    "description": "Absolute path of the workspace root. Defaults to the server's working directory.",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "briefing",
        "title": "Session briefing",
        "description": (
            "Get the short KnowItAll2 briefing for the current project: the user's rules and what is known "
            "about this project. Call once at the start of a session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_path": _PROJECT_PATH},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "recall",
        "title": "Recall memories",
        "description": (
            "Search KnowItAll2 memory by keywords: systems, hosts, services, projects, tools, procedures, "
            "decisions, lessons, and the user's rules. Use it before rediscovering something that may have been "
            "learned before. Each result shows its source, date, and whether it is verified; treat unverified "
            "memories as leads to check."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A few keywords, for example 'homelab router dns' or 'vcenter credentials'.",
                },
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": MAX_RECALL_LIMIT,
                    "description": "Maximum number of results (default 8).",
                },
                "scope": {
                    "type": "string", "enum": list(RECALL_SCOPES),
                    "description": (
                        "'all' (default) searches global memories and this project; 'everywhere' also "
                        "includes other projects; 'project' and 'global' narrow the search."
                    ),
                },
                "project_path": _PROJECT_PATH,
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "remember",
        "title": "Remember",
        "description": (
            "Save one durable memory that would help a future session: a fact about a system or project, a "
            "procedure that worked, a decision, a lesson from a failure, or a rule the user stated. Keep it to "
            "one self-contained statement. Never include secrets; record where a credential is kept instead. "
            "Set source to 'user' only for the user's own words, 'observed' for something confirmed by tool "
            "output in this session, and otherwise 'inferred'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "maxLength": 2000, "description": "The memory, as one statement."},
                "kind": {
                    "type": "string", "enum": list(KINDS),
                    "description": "Default 'fact'. Only the user can set a 'rule'.",
                },
                "subjects": {
                    "type": "array", "items": {"type": "string"}, "maxItems": 8,
                    "description": "What it is about, for example ['OpenWrt router', 'DNS'].",
                },
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "scope": {
                    "type": "string", "enum": list(SCOPES),
                    "description": (
                        "'global' (default) for systems, infrastructure, tools, and user-wide rules; 'project' "
                        "for things that matter only inside this project. Decisions default to 'project'."
                    ),
                },
                "source": {"type": "string", "enum": list(SOURCES), "description": "Default 'inferred'."},
                "replaces": {
                    "type": "string",
                    "description": (
                        "Id of a memory this one corrects or updates. Weaker evidence never replaces a stronger "
                        "memory, and only the user's own words replace the user's statements: otherwise both are "
                        "kept and the user is asked which is right."
                    ),
                },
                "project_path": _PROJECT_PATH,
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False,
        },
    },
    {
        "name": "forget",
        "title": "Forget",
        "description": (
            "Retire a memory that is wrong or no longer true, by its id. It stops appearing in results. The user's "
            "own statements are not retired this way: KnowItAll2 asks the user, with your reason, and if the user "
            "just asked for it, the reply says how to record their choice with `answer`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The memory id, for example k-1a2b3c4d5e."},
                "reason": {"type": "string", "description": "Why it is being retired."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False,
        },
    },
    {
        "name": "questions",
        "title": "Questions for the user",
        "description": (
            "List KnowItAll2's open questions for the user, each with its choices. Call it when the briefing "
            "says there are questions and the user has a moment, then ask the user."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False,
        },
    },
    {
        "name": "answer",
        "title": "Answer a question",
        "description": (
            "Record the user's choice for one KnowItAll2 question. The answer counts as the user's own word, "
            "so only record a choice the user made; never choose on the user's behalf."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The question id, for example q-1a2b3c4d."},
                "choice": {"type": "string", "description": "The key of the option the user chose."},
            },
            "required": ["id", "choice"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False,
        },
    },
    {
        "name": "settle",
        "title": "Report on a KnowItAll2 request",
        "description": (
            "Report the result of an optional KnowItAll2 request shown in a briefing or recall result (a task id "
            "such as t-1a2b3c4d): which of two disagreeing memories is right, or information KnowItAll2 asked to "
            "find out. Answer only from what this session showed or one quick look-only check, and set certain to "
            "false when it is not settled. Never report a secret; say where it is kept instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task id, for example t-1a2b3c4d."},
                "choice": {
                    "type": "string", "enum": ["use_new", "keep_mine", "keep_both", "found"],
                    "description": (
                        "For two disagreeing memories: use_new (the second is right), keep_mine (the first is "
                        "right), or keep_both (both hold). For a request to find something out: found."
                    ),
                },
                "what_you_saw": {
                    "type": "string", "maxLength": 600,
                    "description": "What you saw that settles it, or the information you found.",
                },
                "certain": {"type": "boolean", "description": "True only if what you saw settles it."},
            },
            "required": ["task", "choice", "what_you_saw", "certain"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False,
        },
    },
    {
        "name": "learn",
        "title": "Learn from this session now",
        "description": (
            "Ask KnowItAll2 to learn from this session now. Call it when the user asks KnowItAll2 to learn or pick "
            "something up (reason asked), or once when a task the user gave you is complete (reason finished); not "
            "after every step. It returns at once: learning runs separately, and KnowItAll2 shows the user what it "
            "learned. For one fact, use remember instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "enum": ["asked", "finished"],
                           "description": "asked: the user asked; finished: a task is complete."},
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False,
        },
    },
]


class _InvalidParams(ValueError):
    """A protocol-level parameter error."""


def agent_name(client_name: object) -> str | None:
    """Map an MCP client name to the agent it belongs to."""

    if not isinstance(client_name, str) or not client_name.strip():
        return None
    lowered = client_name.strip().lower()
    if "claude" in lowered:
        return "claude-code"
    if "codex" in lowered:
        return "codex"
    return lowered[:40]


class McpServer:
    def __init__(self, *, memory_factory: Callable[[str | None], Memory], cwd: Path) -> None:
        self._memory_factory = memory_factory
        self._memory: Memory | None = None
        self._agent: str | None = None
        self._cwd = cwd

    def serve(self, reader: BinaryIO, writer: BinaryIO) -> None:
        """Serve newline-delimited JSON-RPC messages until the input closes."""

        for raw in iter(reader.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                response: Any = _error(None, -32700, "Parse error")
            else:
                if message == []:  # JSON-RPC answers an empty batch with one error
                    response = _error(None, -32600, "Invalid Request")
                elif isinstance(message, list):
                    response = [item for item in (self.handle(part) for part in message) if item is not None] or None
                else:
                    response = self.handle(message)
            if response is not None:
                writer.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                writer.flush()

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
        method = message.get("method")
        message_id = message.get("id")
        is_notification = "id" not in message
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return None
            return _error(message_id, -32600, "Invalid Request")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call_tool(params)
            elif method.startswith("notifications/"):
                return None
            else:
                return None if is_notification else _error(message_id, -32601, f"Method not found: {method}")
        except _InvalidParams as exc:
            return None if is_notification else _error(message_id, -32602, str(exc))
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            journal.problem(self._problem_source(), f"internal error in {method}: {type(exc).__name__}: {exc}")
            return None if is_notification else _error(message_id, -32603, "Internal error")
        return None if is_notification else {"jsonrpc": "2.0", "id": message_id, "result": result}

    def _initialize(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _InvalidParams("initialize params must be an object")
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        client = params.get("clientInfo")
        self._agent = agent_name(client.get("name") if isinstance(client, dict) else None)
        return {
            "protocolVersion": version,
            # The front announces a changed tool list after an update.
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "knowitall2", "title": "KnowItAll2", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _InvalidParams("tools/call params must be an object")
        name = params.get("name")
        handler = _HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            raise _InvalidParams(f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _InvalidParams("Tool arguments must be an object")
        try:
            memory = self._get_memory()
            text = handler(memory, arguments, self._cwd)
            if name in NEWS_TOOLS:
                text = self._with_news(memory, text)
            return _text_result(text)
        except MemoryInputError as exc:
            return _text_result(str(exc), is_error=True)
        except (StoreError, sqlite3.Error) as exc:
            journal.problem(self._problem_source(), f"{name} failed: {exc}")
            return _text_result(f"KnowItAll2 memory is unavailable right now ({exc}). Continue without it.", is_error=True)

    def _with_news(self, memory: Memory, text: str) -> str:
        """The answer, followed by memories added since the chat's briefing that it has not seen, if any."""

        from .chats import RECORD_ID
        from .hooks import tool_news

        try:
            news = tool_news(memory, cwd=self._cwd, agent=self._agent, shown=RECORD_ID.findall(text))
        except Exception as exc:
            journal.problem(self._problem_source(), f"what is new could not be added: {type(exc).__name__}: {exc}")
            return text
        return f"{text}\n\n{news}" if news else text

    def _problem_source(self) -> str:
        return f"MCP server ({self._agent or 'unknown agent'})"

    def _get_memory(self) -> Memory:
        if self._memory is None:
            self._memory = self._memory_factory(self._agent)
        return self._memory


def _briefing(memory: Memory, arguments: dict[str, Any], cwd: Path) -> str:
    return memory.briefing(project_path=_optional_string(arguments, "project_path") or cwd)


def _recall(memory: Memory, arguments: dict[str, Any], cwd: Path) -> str:
    return memory.recall(
        _required_string(arguments, "query"),
        limit=_optional_integer(arguments, "limit"),
        scope=_optional_string(arguments, "scope") or "all",
        project_path=_optional_string(arguments, "project_path") or cwd,
    )


def _remember(memory: Memory, arguments: dict[str, Any], cwd: Path) -> str:
    result = memory.remember(
        _required_string(arguments, "text"),
        kind=_optional_string(arguments, "kind") or "fact",
        subjects=_string_list(arguments, "subjects"),
        tags=_string_list(arguments, "tags"),
        scope=_optional_string(arguments, "scope"),
        source=_optional_string(arguments, "source") or "inferred",
        replaces=_optional_string(arguments, "replaces"),
        project_path=_optional_string(arguments, "project_path") or cwd,
    )
    return result.describe()


def _forget(memory: Memory, arguments: dict[str, Any], cwd: Path) -> str:
    return memory.forget(_required_string(arguments, "id"), reason=_optional_string(arguments, "reason"))


def _questions(memory: Memory, arguments: dict[str, Any], cwd: Path) -> str:
    return review.list_questions(memory)


def _answer(memory: Memory, arguments: dict[str, Any], cwd: Path) -> str:
    return review.answer(memory, _required_string(arguments, "id"), _required_string(arguments, "choice").strip())


def _settle(memory: Memory, arguments: dict[str, Any], cwd: Path) -> str:
    certain = arguments.get("certain")
    if not isinstance(certain, bool):
        raise MemoryInputError("'certain' must be true or false.")
    return review.settle_task(
        memory, _required_string(arguments, "task"), choice=_required_string(arguments, "choice").strip(),
        found=_optional_string(arguments, "what_you_saw") or "", certain=certain,
    )


def _learn(memory: Memory, arguments: dict[str, Any], cwd: Path) -> str:
    from .hooks import request_now

    reason = _optional_string(arguments, "reason") or "asked"
    if reason not in ("asked", "finished"):
        raise MemoryInputError("'reason' must be asked or finished.")
    return request_now(cwd=cwd, agent=memory.agent, reason=reason)


_HANDLERS: dict[str, Callable[[Memory, dict[str, Any], Path], str]] = {
    "briefing": _briefing,
    "recall": _recall,
    "remember": _remember,
    "forget": _forget,
    "questions": _questions,
    "answer": _answer,
    "settle": _settle,
    "learn": _learn,
}


def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = _optional_string(arguments, key)
    if value is None or not value.strip():
        raise MemoryInputError(f"'{key}' is required.")
    return value


def _optional_string(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryInputError(f"'{key}' must be a string.")
    return value


def _optional_integer(arguments: dict[str, Any], key: str) -> int | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    # JSON as Python reads it can hold Infinity and NaN, which no whole number equals.
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value)) or int(value) != value):
        raise MemoryInputError(f"'{key}' must be a whole number.")
    return int(value)


def _string_list(arguments: dict[str, Any], key: str) -> list[str]:
    value = arguments.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MemoryInputError(f"'{key}' must be a list of strings.")
    return value


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def tools_fingerprint() -> str:
    """Identifies the tool list, so the front can tell the agent when an update changed it."""

    return hashlib.sha256(json.dumps(TOOLS, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def handle_once(request: dict[str, Any]) -> dict[str, Any]:
    """Answer one request for the front: ``{"initialize": params, "message": message}``.

    The initialize parameters the agent sent when it connected are replayed
    first, so this process knows which agent it serves.
    """

    server = McpServer(memory_factory=lambda agent: Memory(Store.open(database_path()), agent=agent), cwd=Path.cwd())
    try:
        initialize = request.get("initialize")
        if isinstance(initialize, dict):
            server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": initialize})
        return {"response": server.handle(request.get("message")), "tools": tools_fingerprint()}
    finally:
        if server._memory is not None:
            journal.tidy(server._memory.store)
            if (data_home() / "server" / "connection.json").is_file():  # as in hooks.main: no network code otherwise
                from .connected import nudge

                nudge(server._agent, store=server._memory.store)
            server._memory.store.close()


def run_once() -> int:
    """``knowitall2 serve-once``: one request on stdin, its answer on stdout."""

    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        request = {}
    if not isinstance(request, dict):
        request = {}
    answer = handle_once(request)
    sys.stdout.buffer.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def run_stdio() -> int:
    server = McpServer(
        memory_factory=lambda agent: Memory(Store.open(database_path()), agent=agent),
        cwd=Path.cwd(),
    )
    server.serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0
