"""The process agents start for KnowItAll2's tools (``knowitall2 serve``).

An agent keeps the MCP server it started for the whole session, and desktop
apps keep sessions open for days, so the code in that process never updates.
This front is that process, kept small on purpose: it only passes messages.
Each request is answered by a fresh ``knowitall2 serve-once`` process running
the installed version, so an update reaches open sessions on their next
request. When an update changes the tool list, the front tells the agent
(``notifications/tools/list_changed``); Claude Code then shows the new tools,
Codex keeps the list it has.

This module uses only the standard library and imports nothing else from
KnowItAll2: whatever it imports stays at the version the session started
with. Change it rarely, and keep it compatible with every later version.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, BinaryIO, Callable

WORKER_TIMEOUT_SECONDS = 120
UNAVAILABLE = "KnowItAll2 is unavailable right now ({}). Continue without it."
_Worker = Callable[[dict[str, Any]], dict[str, Any]]


def run_worker(request: dict[str, Any]) -> dict[str, Any]:
    """Answer one request in a fresh process running the installed KnowItAll2."""

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        done = subprocess.run(
            [sys.executable, "-B", "-P", "-m", "knowitall2", "serve-once"],
            input=json.dumps(request).encode("utf-8"), capture_output=True, timeout=WORKER_TIMEOUT_SECONDS,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        raise WorkerError(f"no answer within {WORKER_TIMEOUT_SECONDS} seconds") from None
    except OSError as exc:
        raise WorkerError(f"it could not start: {exc}") from None
    if done.stderr:
        sys.stderr.write(done.stderr.decode("utf-8", errors="replace"))
        sys.stderr.flush()
    try:
        answer = json.loads(done.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise WorkerError(f"it stopped with exit code {done.returncode}") from None
    if not isinstance(answer, dict):
        raise WorkerError("it gave no answer")
    return answer


class WorkerError(RuntimeError):
    """The fresh process could not answer."""


class Front:
    def __init__(self, *, worker: _Worker = run_worker) -> None:
        self._worker = worker
        self._initialize: dict[str, Any] | None = None
        # The tool list the agent was last given, by fingerprint.
        self._told: str | None = None

    def serve(self, reader: BinaryIO, writer: BinaryIO) -> None:
        for raw in iter(reader.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                self._write(writer, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if message == []:  # JSON-RPC answers an empty batch with one error
                self._write(writer, {"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32600, "message": "Invalid Request"}})
                continue
            for part in message if isinstance(message, list) else [message]:
                for reply in self.handle(part):
                    self._write(writer, reply)

    def handle(self, message: Any) -> list[dict[str, Any]]:
        """The messages to send back for one message from the agent."""

        if not isinstance(message, dict):
            return [{"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}]
        method = message.get("method")
        if not isinstance(method, str) or method.startswith("notifications/"):
            return []  # responses and notifications need no answer
        ident = message.get("id")
        if method == "ping":
            return [{"jsonrpc": "2.0", "id": ident, "result": {}}]
        if method == "initialize" and isinstance(message.get("params"), dict):
            self._initialize = message["params"]
        try:
            answer = self._worker({"initialize": self._initialize, "message": message})
        except WorkerError as exc:
            return [] if "id" not in message else [self._unavailable(method, ident, str(exc))]
        replies = [answer["response"]] if isinstance(answer.get("response"), dict) else []
        tools = answer.get("tools")
        if method in ("initialize", "tools/list"):
            self._told = tools if isinstance(tools, str) else None
        elif isinstance(tools, str) and self._told is not None and tools != self._told:
            # An update changed the tools: say so once; the agent lists them again if it can.
            replies.append({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            self._told = tools
        return replies

    def _unavailable(self, method: str, ident: Any, why: str) -> dict[str, Any]:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": ident,
                    "result": {"content": [{"type": "text", "text": UNAVAILABLE.format(why)}], "isError": True}}
        if method == "initialize":
            # Still connect, so the session starts; tools work once KnowItAll2 does.
            requested = (self._initialize or {}).get("protocolVersion")
            return {"jsonrpc": "2.0", "id": ident, "result": {
                "protocolVersion": requested if isinstance(requested, str) else "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "knowitall2", "title": "KnowItAll2", "version": "unavailable"},
            }}
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": UNAVAILABLE.format(why)}}

    @staticmethod
    def _write(writer: BinaryIO, message: dict[str, Any]) -> None:
        writer.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        writer.flush()


def main() -> int:
    Front().serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0

