"""The app's local web server: loopback only, one key per launch, and gone when the window closes.

Everything the page shows comes from the live database at the moment it asks;
nothing is precomputed, so nothing can go stale. The page is plain HTML,
CSS, and JavaScript shipped in the package, with no build step.

Security, in layers:
- The server binds to 127.0.0.1 only and answers only requests whose Host is
  that address and port, which defeats DNS rebinding.
- Every API request must carry the launch key in the ``X-KnowItAll2-Key``
  header. A custom header also means another web page cannot send the
  request without a CORS preflight, which is always refused.
- A request that changes something must be a JSON POST from this page's own
  origin.
- The page is served with a strict Content-Security-Policy and cannot be
  framed.

Lifetime: the page says goodbye when its window closes, and the server stops
a few seconds later unless the page comes back (a reload). A page that goes
silent (a crashed browser) stops the server after ``IDLE_SECONDS``; time the
computer spent asleep does not count.
"""

from __future__ import annotations

import hmac
import json
import re
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .. import journal
from ..memory import MemoryInputError
from . import api

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js",
                "/icon.svg": "icon.svg"}
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml; charset=utf-8"}
KEY_HEADER = "X-KnowItAll2-Key"
WINDOW_HEADER = "X-KnowItAll2-Window"
IDLE_SECONDS = 10 * 60
GOODBYE_GRACE_SECONDS = 8
# A window checks in at least once a minute, even minimized.
WINDOW_ALIVE_SECONDS = 90
WATCH_INTERVAL_SECONDS = 5
MAX_BODY_BYTES = 64 * 1024
# Of a body too large to take, this much is read and dropped before the connection closes, so a caller
# that sends the whole body before reading the answer still gets its 413.
DROP_AT_MOST_BYTES = 4 * MAX_BODY_BYTES
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
)


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


Route = Callable[[dict[str, list[str]], dict[str, Any], re.Match], Any]


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self, key: str, *, port: int = 0, idle_seconds: float = IDLE_SECONDS,
        watch_interval: float = WATCH_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(("127.0.0.1", port), AppHandler)
        self.key = key
        self.idle_seconds = idle_seconds
        self.watch_interval = watch_interval
        self.port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{host}" for host in self.hosts}
        self._last_seen = time.monotonic()
        self._windows: dict[str, float] = {}
        self._goodbye_at: float | None = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def seen(self, window: str | None = None) -> None:
        with self._lock:
            self._last_seen = time.monotonic()
            self._goodbye_at = None
            if window:
                self._windows[window[:64]] = self._last_seen

    def goodbye(self, window: str | None = None) -> None:
        """A window closed; stop soon unless another window is still checking in."""

        with self._lock:
            self._windows.pop((window or "")[:64], None)
            moment = time.monotonic()
            others = [seen for seen in self._windows.values() if moment - seen < WINDOW_ALIVE_SECONDS]
            if not others:
                self._goodbye_at = moment + GOODBYE_GRACE_SECONDS

    def watch(self) -> None:
        """Stop serving when the page said goodbye or went silent; runs in its own thread."""

        previous = time.monotonic()
        while not self._stopping.wait(self.watch_interval):
            moment = time.monotonic()
            with self._lock:
                if moment - previous > max(self.watch_interval * 6, 30):
                    # The computer was asleep: give the page time to wake up and check in.
                    self._last_seen = moment
                previous = moment
                done = (self._goodbye_at is not None and moment >= self._goodbye_at) or (
                    moment - self._last_seen > self.idle_seconds
                )
            if done:
                self.stop()

    def stop(self) -> None:
        if not self._stopping.is_set():
            self._stopping.set()
            threading.Thread(target=self.shutdown, daemon=True).start()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A browser closing a kept-alive connection is normal; anything else is a problem.
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return
        journal.problem("app", f"the app server failed on a request: {type(error).__name__}: {error}")

    def serve(self) -> None:
        threading.Thread(target=self.watch, daemon=True, name="knowitall2-app-watch").start()
        try:
            self.serve_forever(poll_interval=0.5)
        finally:
            self._stopping.set()
            self.server_close()


class AppHandler(BaseHTTPRequestHandler):
    server: AppServer
    server_version = "KnowItAll2"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet, and safe under pythonw, which has no stderr.
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        # For this request only: the handler lasts as long as the connection, which can carry more requests.
        self._body_read = False
        length = self._length()
        if length is None or length > MAX_BODY_BYTES:
            # This answer is the connection's last: where the body ends is unknown, or more comes than is read.
            self.close_connection = True
        try:
            self._answer(method)
        finally:
            if not self._body_read:
                self._drop_body(length)

    def _answer(self, method: str) -> None:
        try:
            if method == "OPTIONS":
                # No cross-origin access, ever.
                raise RequestError(HTTPStatus.FORBIDDEN, "cross-origin requests are not allowed")
            if self.headers.get("Host") not in self.server.hosts:
                raise RequestError(HTTPStatus.FORBIDDEN, "unexpected host")
            parts = urlsplit(self.path)
            if method == "GET" and parts.path in STATIC_FILES:
                self._send_static(STATIC_FILES[parts.path])
                return
            if not parts.path.startswith("/api/"):
                raise RequestError(HTTPStatus.NOT_FOUND, "not found")
            if parts.path == "/api/bye" and method == "POST":
                # A closing page can only send a plain beacon: "<key> <window id>" in the body.
                key, _, window = self._read_body().decode("utf-8", errors="replace").strip().partition(" ")
                self._check_key(key)
                self.server.goodbye(window)
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            self._check_key(self.headers.get(KEY_HEADER, ""))
            body: dict[str, Any] = {}
            if method == "POST":
                body = self._read_json()
            self.server.seen(self.headers.get(WINDOW_HEADER))
            route, match = _find_route(method, parts.path)
            result = route(parse_qs(parts.query), body, match)
            self._send_json(HTTPStatus.OK, result)
            if method == "POST":
                from ..connected import nudge
                from ..known import nudge as refresh_known_files

                nudge("app", pull=False)
                refresh_known_files()
        except RequestError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except api.NotFound as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except MemoryInputError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (ConnectionError, TimeoutError):
            # The window went away mid-answer (closed or reloaded): normal, and nothing to send.
            self.close_connection = True
        except Exception as exc:
            journal.problem("app", f"{method} {urlsplit(self.path).path} failed: {type(exc).__name__}: {exc}",
                            details={"trace": traceback.format_exc(limit=4)})
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"KnowItAll2 could not do that: {exc}"})

    def _check_key(self, offered: str) -> None:
        if not hmac.compare_digest(offered.encode("utf-8"), self.server.key.encode("utf-8")):
            raise RequestError(HTTPStatus.UNAUTHORIZED, "this window's key is missing or out of date")

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            # Where this request ends, and so where the next one on the connection starts, is unknown.
            self.close_connection = True
            raise RequestError(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if length > MAX_BODY_BYTES:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        self._body_read = True
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict[str, Any]:
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.origins:
            raise RequestError(HTTPStatus.FORBIDDEN, "unexpected origin")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
        raw = self._read_body()
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, RecursionError):  # RecursionError: nested deeper than the parser goes
            raise RequestError(HTTPStatus.BAD_REQUEST, "the request was not valid JSON") from None
        if not isinstance(body, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "the request must be a JSON object")
        return body

    def _length(self) -> int | None:
        """The length of this request's body (0 without one), or None when it cannot be known."""

        if self.headers.get("Transfer-Encoding"):  # such as chunked, which this server does not read
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        return length if length >= 0 else None

    def _drop_body(self, length: int | None) -> None:
        """Read and drop the body of a request answered without reading it (a refusal, such as a missing key).

        Left on a kept-alive connection, it would be read as the next request.
        A body larger than the server takes is read only so far, so a caller
        that sends it all before reading still gets the answer; that
        connection then closes (``_dispatch``).
        """

        if length is None:
            return
        left = min(length, DROP_AT_MOST_BYTES)
        try:
            while left > 0:
                chunk = self.rfile.read(min(left, 64 * 1024))
                if not chunk:
                    self.close_connection = True
                    return
                left -= len(chunk)
        except OSError:
            self.close_connection = True

    def _send_static(self, name: str) -> None:
        # Fixed types: Windows' registry can map .js to text/plain, which a
        # browser refuses to run under nosniff.
        kind = STATIC_TYPES[Path(name).suffix]
        self._send(HTTPStatus.OK, (STATIC_DIR / name).read_bytes(), kind)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, data: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


def _find_route(method: str, path: str) -> tuple[Route, re.Match]:
    """The handler for this method and path; a path known only for other methods is a 405 (one path, such as
    the catch-up list and its start, can have a GET and a POST)."""

    other = None
    for route_method, pattern, handler in api.ROUTES:
        match = re.fullmatch(pattern, path)
        if match:
            if route_method == method:
                return handler, match
            other = other or route_method
    if other:
        raise RequestError(HTTPStatus.METHOD_NOT_ALLOWED, f"use {other}")
    raise RequestError(HTTPStatus.NOT_FOUND, "not found")
