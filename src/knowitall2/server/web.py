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
from urllib.parse import SplitResult, parse_qs, urlsplit

from .. import __version__, journal
from ..journal import utc_now
from ..paths import DATABASE_NAME, data_home
from ..store import Store, StoreError
from ..sync import SyncError, changes_since
from . import API_VERSION, accounts, admin, backups, exchange, open_store

DEFAULT_PORT = 4191
MAX_BODY_BYTES = 8 * 1024 * 1024
# What each route reads at most: only an agent that has shown its key sends a large body (its changes).
SMALL_BODY_BYTES = 256 * 1024
JOIN_BODY_BYTES = 16 * 1024
BODY_LIMITS = {"/api/v1/push": MAX_BODY_BYTES, "/api/v1/usage": MAX_BODY_BYTES, "/api/v1/join": JOIN_BODY_BYTES}
# Routes that need an agent's key: it is checked before the body is read.
KEY_ROUTES = frozenset({"/api/v1/push", "/api/v1/usage", "/api/v1/lease"})
# Of a body too large to take, this much is read and dropped before the connection closes, so a caller
# that sends the whole body before reading the answer still gets its 413.
DROP_AT_MOST_BYTES = 4 * MAX_BODY_BYTES
# A connection that sends nothing for this long is closed. Longer than a reverse proxy keeps an idle connection
# (Traefik: 90 s), so the proxy closes first and never sends a request on a connection the server is closing.
CONNECTION_TIMEOUT_SECONDS = 120
HOUSEKEEPING_SECONDS = 60 * 60
# The first backup waits until the server has settled after starting.
HOUSEKEEPING_DELAY_SECONDS = 2 * 60
VERSION_HEADER = "X-KnowItAll2-Version"
API_HEADER = "X-KnowItAll2-API"
COMPUTER_HEADER = "X-KnowItAll2-Computer"
CHANGES_HEADER = "X-KnowItAll2-Changes"
# Spending join codes, signing in, and recovery codes: at most this many failures per address within the
# window, tries still running included. There is no cap across addresses (nor per account: there is one
# admin), so one guesser cannot lock everyone else out.
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
    """Slows down guessing (join codes, passwords, recovery codes): too many failures and it pauses a while.

    Each try holds a place for its address from :meth:`begin` until :meth:`end`
    settles it, so tries sent all at once count before any of them has failed
    and a burst cannot slip past the limit. An IPv6 caller counts as its whole
    /64 network, which one computer usually has to itself.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._running: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _group(address: str) -> str:
        try:
            found = ipaddress.ip_address(address)
        except ValueError:
            return address
        if isinstance(found, ipaddress.IPv6Address):
            if found.ipv4_mapped is not None:
                return str(found.ipv4_mapped)
            return str(ipaddress.ip_network(f"{found}/64", strict=False))
        return address

    def begin(self, address: str) -> bool:
        """Hold a place for one try from ``address``; False when its failures and running tries reach the limit."""

        group = self._group(address)
        with self._lock:
            now = self._clock()
            mine = self._failures.get(group)
            if mine is not None:
                while mine and now - mine[0] > JOIN_WINDOW_SECONDS:
                    mine.popleft()
                if not mine:
                    del self._failures[group]
            if len(mine or ()) + self._running.get(group, 0) >= JOIN_FAILURES_PER_ADDRESS:
                return False
            self._running[group] = self._running.get(group, 0) + 1
            return True

    def end(self, address: str, *, failed: bool) -> None:
        """Settle a try :meth:`begin` let through: a failure stays counted for the window, anything else does not."""

        group = self._group(address)
        with self._lock:
            now = self._clock()
            left = self._running.get(group, 0) - 1
            if left > 0:
                self._running[group] = left
            else:
                self._running.pop(group, None)
            if not failed:
                return
            self._failures.setdefault(group, deque()).append(now)
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
            # With no admin, setting one up needs this code, which ``run`` prints to the server's log.
            self.setup_code = admin.new_setup_code() if admin.admin(store) is None else None
            self.epoch = exchange.begin_run(store, home)
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
        self._proxy_noted = False
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

    def note_untrusted_proxy(self, address: str) -> None:
        """Say once, in the problems journal, that a proxy the user has not named forwards callers' addresses."""

        if self._proxy_noted:
            return
        self._proxy_noted = True
        journal.problem("server", f"requests from {address} carry X-Forwarded-For, but KNOWITALL2_TRUSTED_PROXY "
                                  "does not name that address, so every caller behind it counts as one address for "
                                  "the sign-in limits; set KNOWITALL2_TRUSTED_PROXY to the proxy's address")

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
                journal.tidy(store, now=now)  # old events, and uses older than a year folded into totals
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
    timeout = CONNECTION_TIMEOUT_SECONDS
    _head_only = False

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        # What a GET would answer, without the body (some uptime monitors only send HEAD).
        self._dispatch("GET", head_only=True)

    def _dispatch(self, method: str, *, head_only: bool = False) -> None:
        # For this request only: the handler lasts as long as the connection, which can carry more requests.
        self._head_only = head_only
        self._body_read = False
        length = self._length()
        if length is None or length > MAX_BODY_BYTES:
            # This answer is the connection's last: where the body ends is unknown, or more comes than is read.
            self.close_connection = True
        parts = urlsplit(self.path)
        try:
            self._answer(method, parts)
        finally:
            if not self._body_read:
                self._drop_body(length)

    def _answer(self, method: str, parts: SplitResult) -> None:
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
            body = self._read_json(parts.path) if method == "POST" else {}
            route(self, parse_qs(parts.query), body)
        except RequestError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except (SyncError, accounts.AccountError, admin.AdminError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except admin.Busy as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)}, {"Retry-After": "5"})
        except StoreError as exc:
            journal.problem("server", f"the server's store failed: {exc}")
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"the server's store failed: {exc}"})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            # The detail goes only to the journal: it can show the server's paths, and the caller may have no key.
            journal.problem("server", f"{method} {parts.path} failed: {type(exc).__name__}: {exc}",
                            details={"trace": traceback.format_exc(limit=4)})
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": "the server could not do that; its data folder's logs/problems.jsonl says why"})

    # --- Requests ---

    def address(self) -> str:
        """The caller's address; behind the user's named proxy, the address the proxy reports."""

        direct = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded and self.server.trusts(direct):
            return forwarded.split(",")[-1].strip() or direct
        if forwarded:
            self.server.note_untrusted_proxy(direct)
        return direct

    def _read_json(self, path: str = "") -> dict[str, Any]:
        """The request's JSON body, read only when its route takes one that large and, if the route needs an
        agent's key, after the key is checked: no caller without a key makes the server read much."""

        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            # Where this request ends, and so where the next one on the connection starts, is unknown.
            self.close_connection = True
            raise RequestError(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if length > BODY_LIMITS.get(path, SMALL_BODY_BYTES):
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large; send fewer changes at once")
        if path in KEY_ROUTES:
            store = self.server.open()
            try:
                self.authenticated(store)
            finally:
                store.close()
        self._body_read = True  # from here on, even a read cut short by the timeout is not tried again
        raw = self.rfile.read(length) if length else b""
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
        """Read and drop the body of a request answered without reading it (a refusal, such as an unknown route).

        Left on a kept-alive connection, it would be read as the next request,
        and behind a proxy that pools connections, break someone else's
        request. A body larger than the server takes is read only so far, so
        a caller that sends it all before reading still gets the answer; that
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
        except OSError:  # including the handler's timeout
            self.close_connection = True

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
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not self._head_only:
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
    address, guard = handler.address(), handler.server.join_guard
    if not guard.begin(address):
        raise RequestError(HTTPStatus.TOO_MANY_REQUESTS,
                           "too many join codes did not work; wait ten minutes and try again")
    failed = False

    def work(store: Store) -> dict[str, Any]:
        nonlocal failed
        try:
            key, connection = accounts.redeem(
                store, body.get("code"), agent=body.get("agent"), computer=body.get("computer"),
                version=body.get("version"), now=handler.server.clock(),
            )
        except accounts.AccountError:
            failed = True
            raise
        found = exchange.summary(store)
        return {"key": key, "connection": connection, "version": __version__, "api": API_VERSION,
                "latest": found["latest"], "epoch": found["epoch"]}

    try:
        _with_store(handler, work)
    finally:
        guard.end(address, failed=failed)


def _hello(handler: ServerHandler, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> dict[str, Any]:
        connection = handler.authenticated(store)
        found = exchange.summary(store)
        return {"connection": connection, "version": __version__, "api": API_VERSION, "latest": found["latest"],
                "memories": found["memories"], "columns": found["columns"], "epoch": found["epoch"]}

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
        print("The admin account was removed because of KNOWITALL2_RESET_ADMIN. Set it up again with the code "
              "below, then empty that setting in the compose file.", flush=True)
    if server.setup_code:
        print("This server has no admin account yet. To create it, open the server's page and enter this "
              f"one-time setup code: {server.setup_code}", flush=True)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    return 0
