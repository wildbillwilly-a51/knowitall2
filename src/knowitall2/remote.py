"""Talking to a KnowItAll2 server from a computer: the agent's side of ``server.web``.

Only the standard library; every call has a short timeout, so a slow or
missing server never holds an agent up for long. Errors come back as
:class:`RemoteError` with the server's own plain message when it sent one;
a proxy answering 502, 503, or 504 for a stopped server reads as unreachable.
A server on this computer or its own network is reached directly, never
through the system proxy, which is for the internet. A redirect is refused,
never followed, so the key is sent only to the address the computer joined.
"""

from __future__ import annotations

import ipaddress
import json
import platform
import urllib.error
import urllib.request
from typing import Any, Sequence
from urllib.parse import urljoin, urlsplit

from . import __version__

DEFAULT_TIMEOUT = 5.0
# Bad Gateway, Service Unavailable, Gateway Timeout: without KnowItAll2's own error, a proxy saying the server is down.
PROXY_UNREACHABLE = frozenset({502, 503, 504})
# Carrier-grade NAT, which Tailscale also uses: never global, yet not "private" to ipaddress.
_SHARED_ADDRESSES = ipaddress.ip_network("100.64.0.0/10")


class RemoteError(Exception):
    """The server could not be reached, or refused the request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RemoteClient:
    def __init__(
        self, address: str, *, key: str | None = None, agent: str | None = None, computer: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.address = address.rstrip("/")
        self.key = key
        self.agent = agent
        self.computer = computer if computer is not None else platform.node()
        self.timeout = timeout
        host = urlsplit(self.address).hostname or ""
        self._open = (direct_opener() if bypasses_proxy(host) else urllib.request.build_opener(NoRedirects())).open

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[bytes, dict[str, str]]:
        headers = {"X-KnowItAll2-Version": __version__, "Accept": "application/json"}
        if self.computer:
            headers["X-KnowItAll2-Computer"] = self.computer
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.address + path, data=data, headers=headers, method=method)
        try:
            with self._open(request, timeout=self.timeout) as response:
                return response.read(), {name.lower(): value for name, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                exc.close()
                location = urlsplit(urljoin(request.full_url, (exc.headers or {}).get("Location") or ""))
                where = f" to {location.scheme}://{location.netloc}" if location.netloc else ""
                raise RemoteError(f"the KnowItAll2 server at {self.address} answered with a redirect{where} "
                                  f"({exc.code}). KnowItAll2 never follows one, so this computer's key is not sent "
                                  "anywhere else; if that is the server's own address, connect with it instead",
                                  status=exc.code) from None
            try:
                found = json.loads(exc.read())
                message = found.get("error") if isinstance(found, dict) else None
            except (ValueError, OSError):
                message = None
            if not message and exc.code in PROXY_UNREACHABLE:
                # A proxy in front of the server (such as Traefik) answers for it when it is stopped.
                raise RemoteError(f"cannot reach the KnowItAll2 server at {self.address}: a proxy between here "
                                  f"and the server says it is not answering ({exc.code})", status=exc.code) from None
            raise RemoteError(f"the KnowItAll2 server said: {message or exc.reason}", status=exc.code) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise RemoteError(f"cannot reach the KnowItAll2 server at {self.address}: {reason}") from None

    def _json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        raw, _ = self._request(method, path, body)
        try:
            found = json.loads(raw)
        except ValueError:
            raise RemoteError(f"{self.address} did not answer like a KnowItAll2 server") from None
        if not isinstance(found, dict):
            raise RemoteError(f"{self.address} did not answer like a KnowItAll2 server")
        return found

    def health(self) -> dict[str, Any]:
        found = self._json("GET", "/api/v1/health")
        if found.get("product") != "knowitall2":
            raise RemoteError(f"{self.address} is not a KnowItAll2 server")
        return found

    def join(self, code: str) -> dict[str, Any]:
        """Spend a join code; the client keeps the key it returns for its next calls."""

        found = self._json("POST", "/api/v1/join", {
            "code": code, "agent": self.agent, "computer": self.computer, "version": __version__,
        })
        self.key = found["key"]
        return found

    def hello(self) -> dict[str, Any]:
        return self._json("GET", "/api/v1/hello")

    def changes(self, since: int, *, limit: int = 500) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/changes?since={int(since)}&limit={int(limit)}")

    def push(self, operations: Sequence[dict[str, Any]]) -> dict[str, Any]:
        sent = [{name: value for name, value in operation.items() if name != "through"} for operation in operations]
        return self._json("POST", "/api/v1/push", {"operations": sent})

    def usage(self, uses: Sequence[dict[str, Any]]) -> dict[str, Any]:
        return self._json("POST", "/api/v1/usage", {"uses": list(uses)})

    def lease(self, name: str, *, seconds: int = 1800, release: bool = False) -> dict[str, Any]:
        return self._json("POST", "/api/v1/lease", {"name": name, "seconds": seconds, "release": release})

    def copy(self) -> tuple[bytes, int]:
        """A full copy of the shared memory, as SQLite file bytes, and the change number it is current to."""

        data, headers = self._request("GET", "/api/v1/copy")
        return data, int(headers.get("x-knowitall2-changes", "0"))


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect: urllib sends a request's headers, its key among them, wherever one points.

    A redirect then arrives as an ``HTTPError`` with its 3xx status.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def direct_opener() -> urllib.request.OpenerDirector:
    """An opener that never uses a proxy, for this computer and its own network, and never follows a redirect."""

    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())


def bypasses_proxy(host: str) -> bool:
    """Whether a server at ``host`` is reached directly: on this computer, or on its own network.

    That is a loopback, private, shared (CGNAT, Tailscale) or link-local
    address, a name with no dots, ``localhost``, or a ``.local`` name. A
    system proxy (often set by an employer) is for the internet: one that
    takes these anyway answers for a server it cannot reach, and sees the
    key sent with each request.
    """

    name = host.strip("[]").rstrip(".").lower()
    try:
        address = ipaddress.ip_address(name.split("%")[0])
    except ValueError:
        return "." not in name or name.endswith((".local", ".localhost"))
    return address.is_loopback or address.is_private or address.is_link_local or address in _SHARED_ADDRESSES
