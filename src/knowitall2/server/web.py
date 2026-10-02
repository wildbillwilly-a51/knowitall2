"""The server's web service: the agents' routes here, and the admin page (``admin_routes``).

Plain HTTP; when the server is reached through a reverse proxy, TLS is the
proxy's job. Every agent call needs that agent's key as a bearer token,
except the health check (which shows no memories) and spending a join code.
Each request opens the database on its own, like the local app does.

Routes, all JSON unless noted:

- ``GET /api/v1/health``: the server's version and API version.
- ``POST /api/v1/join``: spend a join code; returns the agent's key, once.
- ``GET /api/v1/hello``: the calling connection and the newest change number.
- ``GET /api/v1/changes?since=N``: rows changed after change N.
- ``POST /api/v1/push``: this agent's changes; one answer per change.
- ``POST /api/v1/usage``: briefings and recalls made on a computer.
- ``POST /api/v1/lease``: hold or release a job such as maintenance.
- ``GET /api/v1/copy``: a full copy of the shared memory (an SQLite file).

The admin page is ``/`` with its files from ``static/`` and its own routes
under ``/admin/api/`` (see ``admin_routes``).
"""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .. import __version__, journal
from ..journal import utc_now
from ..paths import DATABASE_NAME, data_home
from ..store import Store, StoreError
from ..sync import SyncError, changes_since
from . import API_VERSION, accounts, admin, backups, exchange, open_store

DEFAULT_PORT = 4191
MAX_BODY_BYTES = 8 * 1024 * 1024
HOUSEKEEPING_SECONDS = 60 * 60
# The first backup waits until the server has settled after starting.
HOUSEKEEPING_DELAY_SECONDS = 2 * 60
VERSION_HEADER = "X-KnowItAll2-Version"
API_HEADER = "X-KnowItAll2-API"
COMPUTER_HEADER = "X-KnowItAll2-Computer"
CHANGES_HEADER = "X-KnowItAll2-Changes"
# Spending join codes, signing in, and recovery codes: at most this many failures per address within the
# window. There is no cap across addresses, so one guesser cannot lock everyone else out.
JOIN_FAILURES_PER_ADDRESS = 5
JOIN_WINDOW_SECONDS = 10 * 60
# Besides the daily backup, one soon after a burst of changes (such as a computer sending its whole memory),
# once the last backup is at least an hour old.
BACKUP_AFTER_CHANGES = 500
BACKUP_AFTER_CHANGES_GAP = timedelta(hours=1)
BACKUP_CHANGES_KEY = "server.backup_changes"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": STATIC_DIR / "index.html", "/index.html": STATIC_DIR / "index.html",
                "/admin.css": STATIC_DIR / "admin.css", "/admin.js": STATIC_DIR / "admin.js",
                "/icon.svg": STATIC_DIR.parent.parent / "app" / "static" / "icon.svg"}
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml; charset=utf-8"}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
)


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def _networks(value: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """``KNOWITALL2_TRUSTED_PROXY``: addresses or networks, separated by commas; nonsense is ignored."""

    found = []
    for part in (value or "").split(","):
        try:
            found.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            continue
    return found


class Throttle:
    """Slows down guessing (join codes, passwords, recovery codes): too many failures and it pauses a while."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _trim(self, moments: deque[float], now: float) -> None:
        while moments and now - moments[0] > JOIN_WINDOW_SECONDS:
            moments.popleft()

    def allowed(self, address: str) -> bool:
        with self._lock:
            now = self._clock()
            mine = self._failures.get(address)
            if mine is None:
                return True
            self._trim(mine, now)
            if not mine:
                del self._failures[address]
                return True
            return len(mine) < JOIN_FAILURES_PER_ADDRESS

    def failed(self, address: str) -> None:
        with self._lock:
            now = self._clock()
            self._failures.setdefault(address, deque()).append(now)
            if len(self._failures) > 10_000:  # forget addresses whose failures have all aged out
                for key in [key for key, moments in self._failures.items() if now - moments[-1] > JOIN_WINDOW_SECONDS]:
                    del self._failures[key]


class KnowItAll2Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, home: Path, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT, clock: Callable[[], str] = utc_now,
        trusted_proxy: str | None = None, housekeeping_seconds: float = HOUSEKEEPING_SECONDS,
        housekeeping_delay: float = HOUSEKEEPING_DELAY_SECONDS, reset_value: str | None = None,
    ) -> None:
        self.home = home
        self.database = home / DATABASE_NAME
        store = open_store(self.database)  # create or upgrade before the first request
        try:
            self.admin_was_reset = admin.reset_from_setting(store, reset_value, now=clock())
        finally:
            store.close()
        # While the reset setting is present, the page reminds the user to delete it.
        self.reset_reminder = bool(reset_value and reset_value.strip())
        super().__init__((host, port), ServerHandler)
        self.port = self.server_address[1]
        self.clock = clock
        self.trusted_proxy = trusted_proxy
        self._proxies = _networks(trusted_proxy)
        self.join_guard = Throttle()
        self.signin_guard = Throttle()
        self.housekeeping_seconds = housekeeping_seconds
        self.housekeeping_delay = housekeeping_delay
        self._housekeeping_lock = threading.Lock()
        self._stopping = threading.Event()

    def open(self) -> Store:
        return open_store(self.database)

    def trusts(self, address: str) -> bool:
        """Whether a request came from the reverse proxy the user named (an address or a network)."""

        try:
            found = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(found in network for network in self._proxies)

    def housekeeping(self, *, now: datetime | None = None) -> None:
        """The daily backup and tidy-up, when due; and a backup soon after a burst of changes."""

        with self._housekeeping_lock:
            now = now or datetime.now(timezone.utc)
            folder = self.home / "backups"
            store = self.open()
            try:
                latest = exchange.summary(store)["latest"]
                backed_up = int(store.get_meta(BACKUP_CHANGES_KEY) or 0)
            finally:
                store.close()
            newest = backups.latest(folder)
            made = backups.made_at(newest) if newest else None
            burst = latest - backed_up >= BACKUP_AFTER_CHANGES and (made is None or now - made >= BACKUP_AFTER_CHANGES_GAP)
            if not (backups.due(folder, now=now) or burst):
                return
            backups.make(self.database, folder, now=now)
            store = self.open()
            try:
                store.set_meta(BACKUP_CHANGES_KEY, str(latest))
                exchange.tidy(store, now=self.clock())
            finally:
                store.close()

    def _housekeeping_loop(self) -> None:
        self._stopping.wait(self.housekeeping_delay)
        while not self._stopping.is_set():
            try:
                self.housekeeping()
            except Exception as exc:  # never let housekeeping stop the server
                journal.problem("server", f"the daily backup or tidy-up failed: {type(exc).__name__}: {exc}")
            self._stopping.wait(self.housekeeping_seconds)

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return
        journal.problem("server", f"the server failed on a request: {type(error).__name__}: {error}")

    def serve(self) -> None:
        threading.Thread(target=self._housekeeping_loop, daemon=True, name="knowitall2-housekeeping").start()
        try:
            self.serve_forever(poll_interval=0.5)
        finally:
            self._stopping.set()
            self.server_close()

    def stop(self) -> None:
        if not self._stopping.is_set():
            self._stopping.set()
            threading.Thread(target=self.shutdown, daemon=True).start()


class ServerHandler(BaseHTTPRequestHandler):
    server: KnowItAll2Server
    server_version = "KnowItAll2"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        # What a GET would answer, without the body (some uptime monitors only send HEAD).
        self._head_only = True
        self._dispatch("GET")

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        try:
            if method == "GET" and parts.path in STATIC_FILES:
                path = STATIC_FILES[parts.path]
                self._send(HTTPStatus.OK, path.read_bytes(), STATIC_TYPES[path.suffix])
                return
            route = ROUTES.get((method, parts.path))
            if route is None:
                known = any(path == parts.path for _, path in ROUTES)
                raise RequestError(HTTPStatus.METHOD_NOT_ALLOWED if known else HTTPStatus.NOT_FOUND,
                                   "use another method" if known else "not found")
            body = self._read_json() if method == "POST" else {}
            route(self, parse_qs(parts.query), body)
        except RequestError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except (SyncError, accounts.AccountError, admin.AdminError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except StoreError as exc:
            journal.problem("server", f"the server's store failed: {exc}")
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"the server's store failed: {exc}"})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            journal.problem("server", f"{method} {parts.path} failed: {type(exc).__name__}: {exc}",
                            details={"trace": traceback.format_exc(limit=4)})
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"the server could not do that: {exc}"})

    # --- Requests ---

    def address(self) -> str:
        """The caller's address; behind the user's named proxy, the address the proxy reports."""

        direct = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded and self.server.trusts(direct):
            return forwarded.split(",")[-1].strip() or direct
        return direct

    def _read_json(self) -> dict[str, Any]:
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise RequestError(HTTPStatus.BAD_REQUEST, "bad Content-Length") from None
        if length > MAX_BODY_BYTES:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large; send fewer changes at once")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise RequestError(HTTPStatus.BAD_REQUEST, "the request was not valid JSON") from None
        if not isinstance(body, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "the request must be a JSON object")
        return body

    def authenticated(self, store: Store) -> dict[str, Any]:
        header = self.headers.get("Authorization") or ""
        scheme, _, key = header.partition(" ")
        found = None
        if scheme.lower() == "bearer" and key.strip():
            found = accounts.authenticate(
                store, key.strip(), now=self.server.clock(), version=self.headers.get(VERSION_HEADER),
                computer=self.headers.get(COMPUTER_HEADER),
            )
        if found is None:
            raise RequestError(
                HTTPStatus.UNAUTHORIZED,
                "this agent's key is not accepted: it is missing, or the agent was removed on the server's page",
            )
        return found

    # --- Responses ---

    def _send_json(self, status: HTTPStatus, payload: Any, extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   extra)

    def _send(self, status: HTTPStatus, data: bytes, kind: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header(VERSION_HEADER, __version__)
        self.send_header(API_HEADER, str(API_VERSION))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)


# --- Routes ---


def _with_store(handler: ServerHandler, work: Callable[[Store], Any]) -> None:
    store = handler.server.open()
    try:
        result = work(store)
    finally:
        store.close()
    handler._send_json(HTTPStatus.OK, result)


def _health(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    handler._send_json(HTTPStatus.OK, {"ok": True, "product": "knowitall2", "version": __version__,
                                       "api": API_VERSION})


def _join(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    address = handler.address()
    if not handler.server.join_guard.allowed(address):
        raise RequestError(HTTPStatus.TOO_MANY_REQUESTS,
                           "too many join codes did not work; wait ten minutes and try again")

    def work(store: Store) -> dict[str, Any]:
        try:
            key, connection = accounts.redeem(
                store, body.get("code"), agent=body.get("agent"), computer=body.get("computer"),
                version=body.get("version"), now=handler.server.clock(),
            )
        except accounts.AccountError:
            handler.server.join_guard.failed(address)
            raise
        return {"key": key, "connection": connection, "version": __version__, "api": API_VERSION,
                "latest": exchange.summary(store)["latest"]}

    _with_store(handler, work)


def _hello(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> dict[str, Any]:
        connection = handler.authenticated(store)
        found = exchange.summary(store)
        return {"connection": connection, "version": __version__, "api": API_VERSION, "latest": found["latest"],
                "memories": found["memories"], "columns": found["columns"]}

    _with_store(handler, work)


def _number(query: dict[str, list[str]], name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int((query.get(name) or [str(default)])[0])
    except ValueError:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"{name} must be a whole number") from None
    return min(max(value, low), high)


def _changes(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    since = _number(query, "since", 0, low=0, high=2 ** 62)
    limit = _number(query, "limit", 500, low=1, high=exchange.MAX_OPERATIONS)

    def work(store: Store) -> dict[str, Any]:
        handler.authenticated(store)
        return changes_since(store, since, limit=limit)

    _with_store(handler, work)


def _push(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    operations = body.get("operations")
    if not isinstance(operations, list):
        raise RequestError(HTTPStatus.BAD_REQUEST, "send {\"operations\": [...]}")

    def work(store: Store) -> dict[str, Any]:
        connection = handler.authenticated(store)
        answers = exchange.apply_operations(store, connection["id"], operations, now=handler.server.clock())
        return {"results": answers, "latest": exchange.summary(store)["latest"]}

    _with_store(handler, work)


def _usage(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    rows = body.get("uses")
    if not isinstance(rows, list):
        raise RequestError(HTTPStatus.BAD_REQUEST, "send {\"uses\": [...]}")

    def work(store: Store) -> dict[str, Any]:
        connection = handler.authenticated(store)
        return {"added": exchange.add_usage(store, connection["id"], rows, now=handler.server.clock())}

    _with_store(handler, work)


def _lease(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> dict[str, Any]:
        connection = handler.authenticated(store)
        if body.get("release"):
            return {"released": exchange.release_lease(store, body.get("name"), connection["id"])}
        return exchange.take_lease(store, body.get("name"), connection["id"], seconds=body.get("seconds", 1800),
                                   now=handler.server.clock())

    _with_store(handler, work)


def _copy(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    store = handler.server.open()
    try:
        handler.authenticated(store)
    finally:
        store.close()
    folder = handler.server.home / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="copy-", suffix=".db", dir=folder)
    os.close(handle)
    target = Path(name)
    try:
        latest = exchange.make_copy(handler.server.database, target)
        data = target.read_bytes()
    finally:
        target.unlink(missing_ok=True)
    handler._send(HTTPStatus.OK, data, "application/vnd.sqlite3", {CHANGES_HEADER: str(latest)})


ROUTES: dict[tuple[str, str], Callable[[ServerHandler, dict[str, list[str]], dict[str, Any]], None]] = {
    ("GET", "/api/v1/health"): _health,
    ("POST", "/api/v1/join"): _join,
    ("GET", "/api/v1/hello"): _hello,
    ("GET", "/api/v1/changes"): _changes,
    ("POST", "/api/v1/push"): _push,
    ("POST", "/api/v1/usage"): _usage,
    ("POST", "/api/v1/lease"): _lease,
    ("GET", "/api/v1/copy"): _copy,
}

from . import admin_routes  # noqa: E402  (its routes use the handler above)

ROUTES.update(admin_routes.ROUTES)


def run(host: str, port: int) -> int:
    """``knowitall2 serve-http``: serve until stopped (Ctrl+C, or the container's stop signal)."""

    home = data_home()
    try:
        server = KnowItAll2Server(home, host=host, port=port,
                                  trusted_proxy=os.environ.get("KNOWITALL2_TRUSTED_PROXY") or None,
                                  reset_value=os.environ.get("KNOWITALL2_RESET_ADMIN"))
    except (OSError, StoreError) as exc:
        print(f"knowitall2: cannot start the server: {exc}", file=sys.stderr)
        return 1
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: server.stop())
    print(f"KnowItAll2 server {__version__} on http://{host}:{server.port}/ with data in {home}", flush=True)
    if server.admin_was_reset:
        print("The admin account was removed because of KNOWITALL2_RESET_ADMIN. Open the page to set it up "
              "again, then empty that setting in the compose file.", flush=True)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    return 0
