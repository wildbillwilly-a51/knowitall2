"""Find and redact secret material so it is never stored or sent to a model.

KnowItAll2 stores where a credential is kept, never the credential itself.
Detection tolerates references such as "the password is in Vaultwarden" so
that useful pointers are not rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    start: int
    end: int


# The private-key marker is split so repository secret scanners do not flag
# this source file itself.
_PRIVATE_KEY_MARKER = r"-----BEGIN [A-Z0-9 ]*PRIVATE " + r"KEY-----"

_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(_PRIVATE_KEY_MARKER)),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("JSON web token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("credentials in a URL", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)),
    ("authorization header", re.compile(
        r"\b(?:proxy-)?authorization\s*:\s*(?:basic|bearer|token|digest)?\s*[A-Za-z0-9._~+/=\-]{12,}",
        re.IGNORECASE,
    )),
    ("bearer token", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}", re.IGNORECASE)),
    ("connection-string password", re.compile(r"\b(?:password|pwd)\s*=\s*[^;\s'\"]{3,}\s*;", re.IGNORECASE)),
)

_ASSIGNMENT = re.compile(
    r"\b(?P<key>pass(?:word|wd|phrase)?|secret|client[_\-]?secret|api[_\-]?key|apikey"
    r"|(?:access|auth|refresh|bearer|session)[_\-]?token|private[_\-]?key)\b"
    r"\s*(?P<separator>[:=]|\bis\b|\bwas\b)\s*"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
# "is" and "was" only introduce a secret after a password-like word; "the
# token is required" or "the test pass is flaky" are ordinary prose.
_PROSE_SEPARATORS = frozenset({"is", "was"})
_PROSE_KEYS = frozenset({"password", "passwd", "passphrase"})
_REFERENCE_WORDS = frozenset({
    "a", "an", "at", "credential", "credentials", "default", "empty", "entry", "env", "environment",
    "false", "from", "held", "hidden", "in", "item", "kept", "managed", "masked", "my", "n/a", "never",
    "no", "none", "not", "null", "on", "only", "optional", "our", "prompt", "prompted", "provided",
    "redacted", "ref", "reference", "required", "reset", "rotated", "same", "saved", "secret", "see",
    "set", "stored", "tbd", "the", "their", "todo", "true", "unknown", "unset", "variable", "via",
    "yes", "your", "vault", "vaultwarden", "bitwarden", "1password", "keepass", "keepassxc",
    "lastpass", "keychain", "changed", "configured",
    "available", "unavailable", "recorded", "missing", "expired", "correct", "incorrect", "wrong",
    "invalid", "needed", "known", "blank", "encrypted", "hashed", "logged",
})
_REFERENCE_PREFIXES = ("<", "${", "$", "%", "{{", "*", "[", "(", "...", "~", "/", "./", "../")
_DRIVE_PATH = re.compile(r"^[a-z]:[\\/]")
_VARIABLE_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


def find_secrets(text: str) -> list[SecretFinding]:
    findings = [
        SecretFinding(kind, match.start(), match.end())
        for kind, pattern in _TOKEN_PATTERNS
        for match in pattern.finditer(text)
    ]
    for match in _ASSIGNMENT.finditer(text):
        key = match.group("key").lower()
        if match.group("separator").lower() in _PROSE_SEPARATORS and key not in _PROSE_KEYS:
            continue
        if _is_reference(match.group("value")):
            continue
        kind = re.sub(r"[_\-]", " ", key)
        findings.append(SecretFinding(kind, match.start("value"), match.end("value")))
    return sorted(findings, key=lambda item: (item.start, item.end))


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))


def redact(text: str) -> str:
    """Replace every detected secret with a ``[REDACTED kind]`` marker."""

    pieces: list[str] = []
    position = 0
    for finding in find_secrets(text):
        if finding.end <= position:
            continue
        pieces.append(text[position:max(finding.start, position)])
        pieces.append(f"[REDACTED {finding.kind}]")
        position = finding.end
    pieces.append(text[position:])
    return "".join(pieces)


def _is_reference(value: str) -> bool:
    """True when an assigned value points at a secret rather than being one."""

    candidate = value.strip().strip("\"'`").strip()
    lowered = candidate.lower().rstrip(".,:;!?)")
    if len(lowered) < 4 or lowered in _REFERENCE_WORDS:
        return True
    if lowered.startswith(_REFERENCE_PREFIXES) or _DRIVE_PATH.match(lowered):
        return True
    if _VARIABLE_NAME.match(candidate):
        return True
    return set(lowered) <= {"*", "x", "."}
