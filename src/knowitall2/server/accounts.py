"""Agents' connections to the server: one-time join codes and the keys they turn into.

The user makes a join code on the server for one agent and gives it to that
agent's install. The agent spends the code once and gets its own key, which
stays on its computer. The server keeps only hashes of codes and keys, so
neither can be read back from its database.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets as token_source
import uuid
from typing import Any

from ..store import Store
from . import later

JOIN_CODE_MINUTES = 15
# No 0/O or 1/I, so a code read aloud or copied by hand survives.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
KEY_PREFIX = "kia_"
# How often a connection's "last checked in" is written, at most.
CHECK_IN_SECONDS = 60


class AccountError(ValueError):
    """A join code or key that cannot be used, said in plain words."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", code.upper())


def _clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())[:limit]
    return value or None


def new_join_code(store: Store, name: str, *, now: str, minutes: int = JOIN_CODE_MINUTES) -> dict[str, Any]:
    """A one-time code for one agent; the only time the code itself is seen."""

    label = _clean(name, 80)
    if label is None:
        raise AccountError("give the agent a name, such as \"Workstation - Codex\"")
    raw = "".join(token_source.choice(CODE_ALPHABET) for _ in range(16))
    code = "-".join(raw[index:index + 4] for index in range(0, 16, 4))
    entry = {"id": "j-" + uuid.uuid4().hex[:10], "name": label, "created_at": now,
             "expires_at": later(now, seconds=minutes * 60)}
    store.connection.execute(
        "INSERT INTO server_join_codes (id, code_hash, name, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (entry["id"], _hash(raw), label, now, entry["expires_at"]),
    )
    return {**entry, "code": code}


def waiting_codes(store: Store, *, now: str) -> list[dict[str, Any]]:
    """Codes made but not yet used, cancelled, or expired."""

    rows = store.connection.execute(
        "SELECT id, name, created_at, expires_at FROM server_join_codes "
        "WHERE used_at IS NULL AND cancelled_at IS NULL AND expires_at > ? ORDER BY created_at",
        (now,),
    ).fetchall()
    return [dict(row) for row in rows]


def cancel_code(store: Store, code_id: str, *, now: str) -> bool:
    cursor = store.connection.execute(
        "UPDATE server_join_codes SET cancelled_at = ? WHERE id = ? AND used_at IS NULL AND cancelled_at IS NULL",
        (now, code_id),
    )
    return cursor.rowcount == 1


def redeem(
    store: Store, code: str, *, agent: object, computer: object, version: object, now: str,
) -> tuple[str, dict[str, Any]]:
    """Spend a join code: a new connection and its key, which is returned only this once."""

    code_hash = _hash(_normalize_code(code if isinstance(code, str) else ""))
    with store.transaction():
        row = store.connection.execute(
            "SELECT id, name FROM server_join_codes WHERE code_hash = ? AND used_at IS NULL "
            "AND cancelled_at IS NULL AND expires_at > ?",
            (code_hash, now),
        ).fetchone()
        if row is None:
            raise AccountError(
                "this join code does not work: it may be mistyped, used already, cancelled, or expired "
                f"(a code lasts {JOIN_CODE_MINUTES} minutes). Make a new one on the server's page"
            )
        key = KEY_PREFIX + token_source.token_urlsafe(32)
        connection = {
            "id": "c-" + uuid.uuid4().hex[:10], "name": row["name"], "agent": _clean(agent, 40),
            "computer": _clean(computer, 80), "version": _clean(version, 20), "created_at": now,
        }
        store.connection.execute(
            "INSERT INTO server_connections (id, name, agent, computer, version, key_hash, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (connection["id"], connection["name"], connection["agent"], connection["computer"],
             connection["version"], _hash(key), now, now),
        )
        store.connection.execute(
            "UPDATE server_join_codes SET used_at = ?, connection_id = ? WHERE id = ?", (now, connection["id"], row["id"]),
        )
    return key, connection


def authenticate(
    store: Store, key: str, *, now: str, version: object = None, computer: object = None,
) -> dict[str, Any] | None:
    """The connection a key belongs to, or None; notes that it checked in."""

    if not key.startswith(KEY_PREFIX):
        return None
    offered = _hash(key)
    row = store.connection.execute(
        "SELECT id, name, agent, computer, version, key_hash, last_seen_at FROM server_connections "
        "WHERE key_hash = ? AND removed_at IS NULL",
        (offered,),
    ).fetchone()
    if row is None or not hmac.compare_digest(row["key_hash"], offered):
        return None
    found = {name: row[name] for name in ("id", "name", "agent", "computer", "version", "last_seen_at")}
    version, computer = _clean(version, 20) or found["version"], _clean(computer, 80) or found["computer"]
    stale = found["last_seen_at"] is None or later(found["last_seen_at"], seconds=CHECK_IN_SECONDS) <= now
    if stale or version != found["version"] or computer != found["computer"]:
        store.connection.execute(
            "UPDATE server_connections SET last_seen_at = ?, version = ?, computer = ? WHERE id = ?",
            (now, version, computer, found["id"]),
        )
        found.update(last_seen_at=now, version=version, computer=computer)
    return found


def connections(store: Store) -> list[dict[str, Any]]:
    """The connected agents, newest first; removed ones are left out."""

    rows = store.connection.execute(
        "SELECT id, name, agent, computer, version, created_at, last_seen_at FROM server_connections "
        "WHERE removed_at IS NULL ORDER BY created_at DESC",
    ).fetchall()
    return [dict(row) for row in rows]


def remove(store: Store, connection_id: str, *, now: str) -> bool:
    """Stop a connection's key working, at once."""

    cursor = store.connection.execute(
        "UPDATE server_connections SET removed_at = ? WHERE id = ? AND removed_at IS NULL", (now, connection_id),
    )
    return cursor.rowcount == 1
