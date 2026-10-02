"""The server's one admin account: first-visit setup, sign-in sessions, and the two ways back in.

- Whoever opens a fresh server's page first creates the admin, with a
  username and password of their choosing. A user does not make a fresh
  server public, and a mistake is fixed by recreating the container.
- Right after that, the page shows a one-time recovery code. With the
  username and that code, "Forgot password" sets a new password and shows a
  new code; each code works once.
- When the code is lost too, ``KNOWITALL2_RESET_ADMIN`` in the compose file
  removes the admin on the next start, once per value, so a line left in
  place does not reset again. Memories and agent connections are untouched.

Passwords are kept as scrypt hashes; recovery codes and session tokens,
which are long and random, as SHA-256 hashes. Nothing here can be read back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets as token_source
from typing import Any

from ..store import Store
from . import later
from .accounts import CODE_ALPHABET

MIN_PASSWORD = 10
MAX_PASSWORD = 200
MAX_USERNAME = 64
SESSION_IDLE_SECONDS = 12 * 60 * 60
SESSION_LONGEST_SECONDS = 7 * 24 * 60 * 60
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
# Worked out once, so signing in as a name that does not exist takes as long as a wrong password.
_DECOY_SALT = b"knowitall2-decoy"


class AdminError(ValueError):
    """Something the admin did that cannot be done, said in plain words."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = token_source.token_bytes(16)
    digest = _scrypt(password, salt, **_SCRYPT)
    encode = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${encode(salt)}${encode(digest)}"


def check_password(password: str, stored: str | None) -> bool:
    if stored is None:
        _scrypt(password, _DECOY_SALT, **_SCRYPT)
        return False
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _check_new_password(candidate: object) -> str:
    if not isinstance(candidate, str) or len(candidate) < MIN_PASSWORD:
        raise AdminError(f"choose a password of at least {MIN_PASSWORD} characters")
    if len(candidate) > MAX_PASSWORD:
        raise AdminError(f"choose a password of at most {MAX_PASSWORD} characters")
    return candidate


def _check_username(username: object) -> str:
    if not isinstance(username, str) or not username.strip():
        raise AdminError("choose a username")
    username = username.strip()
    if len(username) > MAX_USERNAME:
        raise AdminError(f"choose a username of at most {MAX_USERNAME} characters")
    return username


def _new_recovery_code() -> tuple[str, str]:
    raw = "".join(token_source.choice(CODE_ALPHABET) for _ in range(20))
    return "-".join(raw[index:index + 4] for index in range(0, 20, 4)), _sha(raw)


def _normalize_code(code: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", code.upper()) if isinstance(code, str) else ""


def admin(store: Store) -> dict[str, Any] | None:
    row = store.connection.execute("SELECT username, created_at, updated_at FROM server_admin WHERE id = 1").fetchone()
    return dict(row) if row else None


def create(store: Store, username: object, new_password: object, *, now: str) -> str:
    """Create the admin, if there is none yet; returns the first recovery code."""

    name, chosen = _check_username(username), _check_new_password(new_password)
    code, code_hash = _new_recovery_code()
    with store.transaction():
        if admin(store) is not None:
            raise AdminError("this server's admin was already set up; sign in instead")
        store.connection.execute(
            "INSERT INTO server_admin (id, username, password_hash, recovery_hash, created_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (name, hash_password(chosen), code_hash, now, now),
        )
    return code


def _matching(store: Store, username: object) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT username, password_hash, recovery_hash FROM server_admin WHERE id = 1",
    ).fetchone()
    offered = username.strip() if isinstance(username, str) else ""
    if row is None or not hmac.compare_digest(row["username"].casefold().encode(), offered.casefold().encode()):
        return None
    return dict(row)


def start_session(store: Store, *, now: str) -> dict[str, str]:
    token, form = token_source.token_urlsafe(32), token_source.token_urlsafe(24)
    store.connection.execute(
        "INSERT INTO server_sessions (token_hash, form, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
        (_sha(token), form, now, now),
    )
    return {"token": token, "form": form}


def sign_in(store: Store, username: object, attempt: object, *, now: str) -> dict[str, str]:
    found = _matching(store, username)
    offered_password = attempt if isinstance(attempt, str) else ""
    if not check_password(offered_password, found["password_hash"] if found else None) or found is None:
        raise AdminError("that username and password do not match")
    return start_session(store, now=now)


def session(store: Store, token: str | None, *, now: str) -> dict[str, Any] | None:
    """The signed-in session for a cookie's token, or None; ends sessions idle or too old."""

    if not token:
        return None
    store.connection.execute(
        "DELETE FROM server_sessions WHERE last_seen_at < ? OR created_at < ?",
        (later(now, seconds=-SESSION_IDLE_SECONDS), later(now, seconds=-SESSION_LONGEST_SECONDS)),
    )
    row = store.connection.execute(
        "SELECT s.form, s.last_seen_at, a.username FROM server_sessions s JOIN server_admin a ON a.id = 1 "
        "WHERE s.token_hash = ?",
        (_sha(token),),
    ).fetchone()
    if row is None:
        return None
    if row["last_seen_at"] != now:
        store.connection.execute("UPDATE server_sessions SET last_seen_at = ? WHERE token_hash = ?", (now, _sha(token)))
    return {"form": row["form"], "username": row["username"]}


def sign_out(store: Store, token: str | None) -> None:
    if token:
        store.connection.execute("DELETE FROM server_sessions WHERE token_hash = ?", (_sha(token),))


def recover(
    store: Store, username: object, code: object, new_password: object, *, now: str,
) -> tuple[str, dict[str, str]]:
    """Set a new password with the recovery code; returns a new code and a fresh session."""

    chosen = _check_new_password(new_password)
    found = _matching(store, username)
    offered = _sha(_normalize_code(code))
    if found is None or not hmac.compare_digest(found["recovery_hash"], offered):
        raise AdminError("that username and recovery code do not match")
    new_code, new_hash = _new_recovery_code()
    with store.transaction():
        store.connection.execute(
            "UPDATE server_admin SET password_hash = ?, recovery_hash = ?, updated_at = ? WHERE id = 1",
            (hash_password(chosen), new_hash, now),
        )
        store.connection.execute("DELETE FROM server_sessions")
        started = start_session(store, now=now)
    return new_code, started


def change_password(store: Store, token: str, current: object, new_password: object, *, now: str) -> None:
    """A new password; every other session is signed out."""

    chosen = _check_new_password(new_password)
    row = store.connection.execute("SELECT password_hash FROM server_admin WHERE id = 1").fetchone()
    if row is None or not check_password(current if isinstance(current, str) else "", row["password_hash"]):
        raise AdminError("the current password is not right")
    with store.transaction():
        store.connection.execute(
            "UPDATE server_admin SET password_hash = ?, updated_at = ? WHERE id = 1", (hash_password(chosen), now),
        )
        store.connection.execute("DELETE FROM server_sessions WHERE token_hash != ?", (_sha(token),))


def replace_recovery_code(store: Store, attempt: object, *, now: str) -> str:
    """A new recovery code, once the admin's current password checks out; the old code stops working."""

    row = store.connection.execute("SELECT password_hash FROM server_admin WHERE id = 1").fetchone()
    if row is None or not check_password(attempt if isinstance(attempt, str) else "", row["password_hash"]):
        raise AdminError("the password is not right")
    code, code_hash = _new_recovery_code()
    store.connection.execute(
        "UPDATE server_admin SET recovery_hash = ?, updated_at = ? WHERE id = 1", (code_hash, now),
    )
    return code


def reset_from_setting(store: Store, value: str | None, *, now: str) -> bool:
    """Remove the admin for a ``KNOWITALL2_RESET_ADMIN`` value not used before; True if it did."""

    if not value or not value.strip():
        return False
    marker = _sha(value.strip())
    with store.transaction():
        if store.connection.execute("SELECT 1 FROM server_resets WHERE value_hash = ?", (marker,)).fetchone():
            return False
        store.connection.execute("INSERT INTO server_resets (value_hash, used_at) VALUES (?, ?)", (marker, now))
        store.connection.execute("DELETE FROM server_admin")
        store.connection.execute("DELETE FROM server_sessions")
    return True
