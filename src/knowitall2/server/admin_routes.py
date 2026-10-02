"""The admin page's routes, under ``/admin/api/``.

Signing in sets a cookie the page's scripts cannot read (``HttpOnly``),
sent only to this site (``SameSite=Strict``), and over HTTPS only when the
page came over HTTPS. Every change also needs the session's form token in a
header, comes as JSON, and, when the browser says where it came from, from
this page's own address. Agents' keys are never accepted here, and the
admin's session is never accepted on the agents' routes.

- ``GET state``: whether setup is needed, and who is signed in.
- ``POST setup``, ``sign-in``, ``sign-out``, ``recover``.
- ``GET overview``: agents, codes waiting to be used, and the server's health.
- ``POST agents/add``, ``agents/remove``, ``codes/cancel``.
- ``POST password``, ``recovery-code``.
- ``GET backup``: a consistent copy of the server's database to download.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .. import __version__
from ..store import Store
from . import accounts, admin, backups, exchange, parse_iso

COOKIE = "knowitall2_session"
FORM_HEADER = "X-KnowItAll2-Form"


def _error(status: HTTPStatus, message: str) -> Exception:
    from .web import RequestError

    return RequestError(status, message)


def _token(handler: Any) -> str | None:
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    try:
        cookie = SimpleCookie(raw)
    except CookieError:
        return None
    found = cookie.get(COOKIE)
    return found.value if found else None


def _secure(handler: Any) -> bool:
    return (handler.server.trusts(handler.client_address[0])
            and (handler.headers.get("X-Forwarded-Proto") or "").lower() == "https")


def _cookie(handler: Any, token: str | None) -> dict[str, str]:
    attributes = "; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if _secure(handler) else "")
    if token is None:
        return {"Set-Cookie": f"{COOKIE}=; Max-Age=0{attributes}"}
    return {"Set-Cookie": f"{COOKIE}={token}; Max-Age={admin.SESSION_LONGEST_SECONDS}{attributes}"}


def _check_origin(handler: Any) -> None:
    origin = handler.headers.get("Origin")
    if origin is None:
        return
    hosts = {handler.headers.get("Host")}
    if handler.server.trusts(handler.client_address[0]):
        hosts.add(handler.headers.get("X-Forwarded-Host"))
    if urlsplit(origin).netloc not in hosts - {None}:
        raise _error(HTTPStatus.FORBIDDEN, "this request did not come from the server's own page")


def _signed_in(handler: Any, store: Store, *, change: bool) -> dict[str, Any]:
    found = admin.session(store, _token(handler), now=handler.server.clock())
    if found is None:
        raise _error(HTTPStatus.UNAUTHORIZED, "sign in first")
    if change:
        _check_origin(handler)
        if handler.headers.get(FORM_HEADER) != found["form"]:
            raise _error(HTTPStatus.FORBIDDEN, "this page is out of date; reload it and try again")
    return found


def _run(handler: Any, work: Callable[[Store], tuple[Any, dict[str, str] | None]]) -> None:
    store = handler.server.open()
    try:
        payload, extra = work(store)
    finally:
        store.close()
    handler._send_json(HTTPStatus.OK, payload, extra)


def _guarded(handler: Any) -> str:
    address = handler.address()
    if not handler.server.signin_guard.allowed(address):
        raise _error(HTTPStatus.TOO_MANY_REQUESTS, "too many tries that did not work; wait ten minutes")
    return address


# --- Signing in ---


def _state(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> tuple[Any, None]:
        found = admin.session(store, _token(handler), now=handler.server.clock())
        state: dict[str, Any] = {"version": __version__, "setup_needed": admin.admin(store) is None,
                                 "signed_in": found is not None, "reset_reminder": handler.server.reset_reminder}
        if found is not None:
            state.update(username=found["username"], form=found["form"])
        return state, None

    _run(handler, work)


def _setup(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    _check_origin(handler)

    def work(store: Store) -> tuple[Any, dict[str, str]]:
        now = handler.server.clock()
        code = admin.create(store, body.get("username"), body.get("new_password"), now=now)
        started = admin.start_session(store, now=now)
        return {"recovery_code": code, "form": started["form"]}, _cookie(handler, started["token"])

    _run(handler, work)


def _sign_in(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    _check_origin(handler)
    address = _guarded(handler)

    def work(store: Store) -> tuple[Any, dict[str, str]]:
        try:
            started = admin.sign_in(store, body.get("username"), body.get("password"), now=handler.server.clock())
        except admin.AdminError:
            handler.server.signin_guard.failed(address)
            raise
        return {"form": started["form"]}, _cookie(handler, started["token"])

    _run(handler, work)


def _sign_out(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    _check_origin(handler)

    def work(store: Store) -> tuple[Any, dict[str, str]]:
        admin.sign_out(store, _token(handler))
        return {"ok": True}, _cookie(handler, None)

    _run(handler, work)


def _recover(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    _check_origin(handler)
    address = _guarded(handler)

    def work(store: Store) -> tuple[Any, dict[str, str]]:
        try:
            code, started = admin.recover(store, body.get("username"), body.get("recovery_code"),
                                          body.get("new_password"), now=handler.server.clock())
        except admin.AdminError as exc:
            if "do not match" in str(exc):
                handler.server.signin_guard.failed(address)
            raise
        return {"recovery_code": code, "form": started["form"]}, _cookie(handler, started["token"])

    _run(handler, work)


# --- Running the server ---


def _database_bytes(handler: Any) -> int:
    total = 0
    for suffix in ("", "-wal"):
        path = Path(f"{handler.server.database}{suffix}")
        if path.exists():
            total += path.stat().st_size
    return total


def _overview(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> tuple[Any, None]:
        found = _signed_in(handler, store, change=False)
        summary = exchange.summary(store)
        newest = backups.latest(handler.server.home / "backups")
        made = backups.made_at(newest) if newest else None
        return {
            "username": found["username"],
            "server": {"version": __version__, "memories": summary["memories"], "database_bytes": _database_bytes(handler),
                       "last_backup": made.strftime("%Y-%m-%dT%H:%M:%SZ") if made else None,
                       "backups": len(backups.backups(handler.server.home / "backups"))},
            "agents": accounts.connections(store),
            "codes": accounts.waiting_codes(store, now=handler.server.clock()),
            "reset_reminder": handler.server.reset_reminder,
        }, None

    _run(handler, work)


def _add_agent(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> tuple[Any, None]:
        _signed_in(handler, store, change=True)
        return accounts.new_join_code(store, body.get("name") or "", now=handler.server.clock()), None

    _run(handler, work)


def _remove_agent(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> tuple[Any, None]:
        _signed_in(handler, store, change=True)
        return {"removed": accounts.remove(store, str(body.get("id") or ""), now=handler.server.clock())}, None

    _run(handler, work)


def _cancel_code(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> tuple[Any, None]:
        _signed_in(handler, store, change=True)
        return {"cancelled": accounts.cancel_code(store, str(body.get("id") or ""), now=handler.server.clock())}, None

    _run(handler, work)


def _password(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> tuple[Any, None]:
        _signed_in(handler, store, change=True)
        admin.change_password(store, _token(handler) or "", body.get("current"), body.get("new_password"),
                              now=handler.server.clock())
        return {"ok": True}, None

    _run(handler, work)


def _recovery_code(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    def work(store: Store) -> tuple[Any, None]:
        _signed_in(handler, store, change=True)
        return {"recovery_code": admin.replace_recovery_code(store, body.get("password"),
                                                            now=handler.server.clock())}, None

    _run(handler, work)


def _backup(handler: Any, query: dict[str, list[str]], body: dict[str, Any]) -> None:
    store = handler.server.open()
    try:
        _signed_in(handler, store, change=False)
    finally:
        store.close()
    folder = handler.server.home / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="backup-", suffix=".db", dir=folder)
    os.close(handle)
    target = Path(name)
    try:
        origin = sqlite3.connect(str(handler.server.database), timeout=5.0)
        copy = sqlite3.connect(str(target))
        try:
            origin.backup(copy)
        finally:
            copy.close()
            origin.close()
        data = target.read_bytes()
    finally:
        target.unlink(missing_ok=True)
    stamp = parse_iso(handler.server.clock()).strftime("%Y%m%d-%H%M%S")
    handler._send(HTTPStatus.OK, data, "application/vnd.sqlite3",
                  {"Content-Disposition": f'attachment; filename="knowitall2-server-{stamp}.db"'})


ROUTES: dict[tuple[str, str], Callable[[Any, dict[str, list[str]], dict[str, Any]], None]] = {
    ("GET", "/admin/api/state"): _state,
    ("POST", "/admin/api/setup"): _setup,
    ("POST", "/admin/api/sign-in"): _sign_in,
    ("POST", "/admin/api/sign-out"): _sign_out,
    ("POST", "/admin/api/recover"): _recover,
    ("GET", "/admin/api/overview"): _overview,
    ("POST", "/admin/api/agents/add"): _add_agent,
    ("POST", "/admin/api/agents/remove"): _remove_agent,
    ("POST", "/admin/api/codes/cancel"): _cancel_code,
    ("POST", "/admin/api/password"): _password,
    ("POST", "/admin/api/recovery-code"): _recovery_code,
    ("GET", "/admin/api/backup"): _backup,
}
