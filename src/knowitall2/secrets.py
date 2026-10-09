"""Find and redact secret material so it is never stored or sent to a model.

KnowItAll2 stores where a credential is kept, never the credential itself.
Detection tolerates references such as "the password is in Vaultwarden" so
that useful pointers are not rejected.

Whole documents and tool output pass through here, so every pattern runs in
time linear in the text: a key name is matched as any identifier and judged
afterwards, and no scan that can start again inside an earlier one reads
further than ``_REACH`` characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    start: int
    end: int


# How far one value, URL password, or command's options are read. A value
# cut off here is still redacted to its end.
_REACH = 256

# The private-key marker is split so repository secret scanners do not flag
# this source file itself.
_PRIVATE_KEY = r"PRIVATE " + r"KEY(?: BLOCK)?-----"
# The whole block: the BEGIN line, the body (real newlines or JSON "\n"
# escapes, encrypted-PEM headers), and the END line. A block cut off without
# an END line runs to the first character that cannot be in a body.
_PRIVATE_KEY_BLOCK = (
    r"-----BEGIN [A-Z0-9 ]*" + _PRIVATE_KEY
    + r"(?:(?!-----END)(?:\\[rn]|[A-Za-z0-9+/=\s:,.\-]))*"
    + r"(?:-----END [A-Z0-9 ]*" + _PRIVATE_KEY + r")?"
)

# A prefix that can repeat inside its own token ("sk-sk-sk-...") starts only
# where no token character comes before it.
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(_PRIVATE_KEY_BLOCK)),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("GitLab token", re.compile(r"(?<![\w\-])gl(?:pat|ptt|dt|rt|soat|ft|cbt|imt|agent)-[A-Za-z0-9_\-]{20,}")),
    ("Slack token", re.compile(r"(?<![\w\-])xox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("API key", re.compile(r"(?<![\w\-])sk-[A-Za-z0-9_\-]{20,}")),
    ("Stripe key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|\bwhsec_[A-Za-z0-9]{24,}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("JSON web token", re.compile(
        r"(?<![\w\-])eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("Azure key", re.compile(r"\b(?:AccountKey|SharedAccessKey)\s*=\s*[A-Za-z0-9+/]{20,}={0,2}", re.IGNORECASE)),
    ("authorization header", re.compile(
        r"\b(?:proxy-)?authorization\s*:\s*(?:basic|bearer|token|digest)?\s*[A-Za-z0-9._~+/=\-]{12,}",
        re.IGNORECASE,
    )),
    ("bearer token", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}", re.IGNORECASE)),
    # KnowItAll2's own: an agent's key ("kia_" and 43 characters), and the server's recovery and setup
    # codes (five groups of four from the code alphabet, which has no 0, 1, I or O); "XXXX-..." is a placeholder.
    ("KnowItAll2 key", re.compile(r"(?<![\w\-])kia_[A-Za-z0-9_\-]{40,}")),
    ("KnowItAll2 code", re.compile(
        r"(?<![\w\-])(?!X{4}-X{4})[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){4}(?![\w\-])")),
    ("connection-string password", re.compile(
        r"\b(?:password|pwd)\s*=\s*[^;\s'\"]{3,%d}+\s*;" % _REACH, re.IGNORECASE)),
)

# A value: quoted, or up to the next space, comma, or semicolon.
_VALUE_TEXT = r"\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]{1,%d}" % _REACH
_VALUE_GROUP = r"(?P<value>" + _VALUE_TEXT + r")"
_VALUE = re.compile(_VALUE_TEXT)
_TO_SPACE = re.compile(r"\S*")

# A password may hold '@' and '/': it runs to the last '@' before the host.
# A port ("host:443/...") is never read as a password.
_URL_CREDENTIALS = re.compile(
    r"(?<![\w+.\-])[a-z][a-z0-9+.\-]*+://[^/\s:@]++:(?!\d{1,5}(?:[/?#\s]|$))"
    r"(?P<value>\S{1,%d}?)@(?=[^\s@/?#]*+(?:[/?#\s]|$))" % _REACH,
    re.IGNORECASE,
)

# Any identifier, or quoted key, before a separator. Whether its name is a
# secret's is decided afterwards, so each identifier is read once. A value
# after "=" or ":" is on the same line ("if token:" ends a line of code);
# prose wraps, so "is" and "was" may have a line break around them.
_ASSIGNMENT = re.compile(
    r"(?<![\w.\-])(?P<quote>[\"']?)(?P<key>[A-Za-z_][\w.\-]*+)(?P=quote)"
    r"(?:[ \t]*+(?P<separator>:=|=>|[:=])[ \t]*+|\s++(?P<prose>is|was)\b\s*+)",
    re.IGNORECASE,
)
_KEY_CORE = (
    r"pass(?:word|wd|phrase)?|pwd|secret|token"
    r"|(?:access|auth|refresh|bearer|session|id)[_\-]?token"
    r"|(?:api|access|secret|private|auth|account|encryption|signing|master)[_\-]?key|apikey"
    r"|shared[_\-]?access[_\-]?key|client[_\-]?secret"
)
_SNAKE_KEY = re.compile(r"(?:^|[_\-.])(?:" + _KEY_CORE + r")$", re.IGNORECASE)  # DB_PASSWORD, x-api-key, a.b.secret
_CAMEL_KEY = re.compile(r"[a-z0-9](?:Password|Passwd|Passphrase|Pwd|Secret|Token|ApiKey|AccessKey|SecretKey"
                        r"|PrivateKey|AuthKey|AccountKey)$")  # dbPassword, clientSecret
_UPPER_KEY = re.compile(r"^[A-Z][A-Z0-9]*(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY)$")  # PGPASSWORD
# "is" and "was" only introduce a secret after a password-like word; "the
# token is required" or "the test pass is flaky" are ordinary prose.
_PROSE_KEY = re.compile(r"pass(?:word|wd|phrase)$", re.IGNORECASE)

# "the root password for nas01 is X", when X has a digit or a symbol: "the
# password for nas01 is weak" is prose.
_PROSE_FOR = re.compile(
    r"\bpass(?:word|wd|phrase)\s+(?:for|of|on|to)\s+[\w.@\-]+(?:\s+[\w.@\-]+){0,3}?\s+(?:is|was)\s+"
    + _VALUE_GROUP,
    re.IGNORECASE,
)
_PASSWORD_SHAPED = re.compile(r"[0-9]|[^A-Za-z0-9\s]")
# Factory and habitual passwords, which are passwords even as plain words, such as a device's default.
_WELL_KNOWN_PASSWORDS = frozenset({"password", "admin", "changeme", "letmein", "welcome", "qwerty", "toor", "root",
                                   "ubnt", "raspberry", "guest", "abc123", "passw0rd"})
# Flags such as --password, --api-key, and openssl's -passin; never --password-stdin.
_CLI_FLAG = re.compile(
    r"(?<![\w-])--?(?P<key>(?:[A-Za-z0-9]+-)*(?:pass(?:word|wd|phrase|in|out)?|pwd|secret|token"
    r"|api-?key|access-?key|secret-?key|client-?secret|auth-?token|access-?token|private-?key))"
    r"(?:=|\s+)(?=" + _VALUE_GROUP + r")",
    re.IGNORECASE,
)
# The options before a secret, within one command.
_OPTIONS = r"[^\n|;&]{0,%d}?" % _REACH
# Passwords given to commands; only a variable or a placeholder is not one.
_COMMAND_FORMS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:mysql|mysqldump|mysqladmin|mariadb|mariadb-dump)\b" + _OPTIONS
               + r"\s-p(?P<value>[^\s\-]\S{0,%d})" % _REACH),
    re.compile(r"\bsshpass\b" + _OPTIONS + r"\s-p\s*" + _VALUE_GROUP),
    re.compile(r"\b(?:curl|wget)\b" + _OPTIONS
               + r"\s(?:-u|--user)(?:\s+|=)?[\"']?[^\s:\"']+:(?P<value>[^\s\"']{1,%d})" % _REACH),
    re.compile(r"\bnet\s+use\b[^\n]{0,%d}?\s/user:\S+\s+(?P<value>[^\s/]\S{0,%d})" % (_REACH, _REACH),
               re.IGNORECASE),
    re.compile(r"\bnet\s+use\s+(?:[A-Z]:\s+)?\\\\\S+\s+(?P<value>[^\s/]\S{0,%d})\s+/user:" % _REACH,
               re.IGNORECASE),
    # .netrc, on one line or several
    re.compile(r"(?:\bmachine\s+\S+|(?m:^)[ \t]*default)(?:\s+(?:login|account|port)\s+\S+)*"
               r"\s+password\s+(?P<value>\S{1,%d})" % _REACH),
    re.compile(r"\bConvertTo-SecureString\b(?:\s+-String)?\s+(?P<value>\"[^\"\n]*\"|'[^'\n]*')", re.IGNORECASE),
    re.compile(r"\b(?:docker|podman|helm)\s+(?:registry\s+)?login\b" + _OPTIONS + r"\s-p(?:\s+|=)" + _VALUE_GROUP),
    re.compile(r"\bsmbclient\b" + _OPTIONS + r"\s(?:-U|--user)(?:\s+|=)?[\"']?[^\s%%\"']+%%(?P<value>[^\s\"']{1,%d})"
               % _REACH),
)

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
    # command-line help and status prose
    "flag", "option", "argument", "parameter", "switch", "value", "prompts", "file", "here",
    "refreshed", "revoked", "issued", "generated", "renewed", "valid",
    # type annotations in code ("new_password: object")
    "object", "bytes", "string", "secretstr",
})
_PATH_PREFIXES = ("<", "${", "$(", "{{", "...", "~/", "~\\", "./", "../", ".\\", "..\\", "\\\\")
# A base64 key may start with "/" too; a path rarely holds its "+" or "=".
_ABSOLUTE_PATH = re.compile(r"^/[^+=]*$")
_DRIVE_PATH = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
_VARIABLE_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_FORMAT_FIELD = re.compile(r"^\{[A-Za-z_][\w.]*\}?$")  # f"PGPASSWORD={password}" (its "}" is trimmed first)
_CONSTANT = re.compile(r"^[A-Z]{4,}$")  # password=PASSWORD names a constant; HUNTER22 is a value
# $NAME, $env:NAME, %NAME%; a digit inside or a symbol makes "$uperS3cret!" a value.
_SHELL_VARIABLE = re.compile(r"^(?:\$(?:env:)?[A-Za-z_][A-Za-z_]*\d*|%[A-Za-z_][A-Za-z0-9_]*%)$", re.IGNORECASE)
# PowerShell that fetches a secret: "$password = Read-Host -AsSecureString"
_FETCH_COMMAND = re.compile(r"^(?:Read-Host|Get-[A-Z][A-Za-z]+|ConvertTo-SecureString|ConvertFrom-[A-Z][A-Za-z]+"
                            r"|New-Object|Import-[A-Z][A-Za-z]+)$", re.IGNORECASE)
# Code that reads a secret from somewhere: a field with a secret's name (data.password,
# process.env.DB_PASSWORD), or a lookup by name (os.environ["DB_PASSWORD"], getenv('TOKEN')).
_CODE_REFERENCE = re.compile(
    r"^(?:[A-Za-z_$][\w$]*\.)+(?P<field>[A-Za-z_]\w*)$"
    r"|^(?:[A-Za-z_$][\w$]*\.)*[A-Za-z_$][\w$]*[\[(][ \t]*[\"'][A-Za-z_][\w.\-]*(?:[\"'][ \t]*[\])]?)?$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")
_CALL = re.compile(r"^[A-Za-z_][\w.]*\([^()\s]*\)$")
_PLAIN_WORDS = re.compile(r"^[a-z]+(?:[-_][a-z]+)*$")
_QUOTES = "\"'`"
# Punctuation and closing quotes after a value: "(see the vault)", 'is weak".'
_TRAILING = ".,:;!?)]}" + _QUOTES


def find_secrets(text: str) -> list[SecretFinding]:
    findings = [
        SecretFinding(kind, match.start(), match.end())
        for kind, pattern in _TOKEN_PATTERNS
        for match in pattern.finditer(text)
    ]
    for match in _URL_CREDENTIALS.finditer(text):
        if not _is_reference(match.group("value"), strict=True):
            findings.append(SecretFinding("credentials in a URL", match.start(), match.end()))
    ends = _ValueEnds(text)
    for match in _ASSIGNMENT.finditer(text):
        key = match.group("key")
        if not _secret_key(key):
            continue
        if match.group("prose") and not _PROSE_KEY.search(key):
            continue
        value = _VALUE.match(text, match.end())
        kind = _kind(key)
        if value is None or _is_reference(value.group(), loose=kind != "password", code=not match.group("prose")):
            continue
        if match.group("prose") and not _prose_password(value.group()):
            continue  # "the password is weak", "the password was shared": prose, as with "for nas01 is weak"
        findings.append(SecretFinding(kind, value.start(), ends.end(*value.span())))
    for match in _PROSE_FOR.finditer(text):
        value = match.group("value")
        if _PASSWORD_SHAPED.search(value.strip(_QUOTES).rstrip(_TRAILING)) and not _is_reference(value):
            findings.append(SecretFinding("password", match.start("value"), ends.end(*match.span("value"))))
    for match in _CLI_FLAG.finditer(text):
        kind = _kind(match.group("key"))
        value = match.group("value")
        if value.startswith("-") or _is_reference(value, loose=kind != "password"):
            continue
        findings.append(SecretFinding(kind, match.start("value"), ends.end(*match.span("value"))))
    for pattern in _COMMAND_FORMS:
        for match in pattern.finditer(text):
            if not _is_reference(match.group("value"), strict=True):
                findings.append(SecretFinding("password", match.start("value"), ends.end(*match.span("value"))))
    return sorted(findings, key=lambda item: (item.start, -item.end))


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


class _ValueEnds:
    """Where values end: one cut off at ``_REACH`` runs on to the next whitespace.

    The last stretch read is remembered, so many values starting in one long
    stretch read it once.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.stretch = (0, 0)  # holds no whitespace, and ends at whitespace or the end of the text

    def end(self, start: int, end: int) -> int:
        value = self.text[start:end]
        quoted = len(value) > 1 and value[0] in "\"'" and value[-1] == value[0]
        if end - start < _REACH or quoted:
            return end
        if not self.stretch[0] <= end - 1 < self.stretch[1]:
            self.stretch = (start, _TO_SPACE.match(self.text, end).end())
        return self.stretch[1]


def _prose_password(value: str) -> bool:
    """Whether the value prose gives for a password counts as one: quoted, password-shaped, or well known.

    An all-letter word, or words joined by hyphens ("case-sensitive",
    "per-user"), is how people describe a password, not the password; the
    same known limit as "the password for X is Y".
    """

    raw = value.strip().rstrip(".,:;!?)]}")
    if raw[:1] in _QUOTES and raw[-1:] == raw[:1] and len(raw) > 2:
        return True
    bare = raw.strip(_QUOTES)
    if bare.lower() in _WELL_KNOWN_PASSWORDS:
        return True
    return bool(_PASSWORD_SHAPED.search(bare)) and not re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+)+", bare)


def _secret_key(key: str) -> bool:
    return bool(_SNAKE_KEY.search(key) or _CAMEL_KEY.search(key) or _UPPER_KEY.match(key))


def _kind(key: str) -> str:
    """What a marker calls the secret held under a key name."""

    lowered = key.lower()
    if "pass" in lowered or lowered.endswith("pwd"):
        return "password"
    if "token" in lowered:
        return "token"
    if "private" in lowered:
        return "private key"
    if re.search(r"api[_\-]?key", lowered):
        return "API key"
    if "secret" in lowered:
        return "secret"
    return "key"


def _is_placeholder(candidate: str) -> bool:
    """A variable, template, path, mask, or earlier redaction marker: never a secret itself."""

    bare = candidate.rstrip(_TRAILING)
    if bare.startswith("[REDACTED") or set(bare.lower()) <= {"*", "x", "."}:
        return True
    if candidate.startswith(_PATH_PREFIXES) or _ABSOLUTE_PATH.match(candidate) or _DRIVE_PATH.match(candidate):
        return True
    code = _CODE_REFERENCE.match(bare)
    if code and (code.group("field") is None or _secret_key(code.group("field"))):
        return True
    if _CALL.match(candidate.rstrip(".,:;!?" + _QUOTES)):  # password = getpass(), pwd = Path.cwd(): code, not a value
        return True
    return bool(_VARIABLE_NAME.match(bare) or _CONSTANT.match(bare) or _SHELL_VARIABLE.match(bare)
                or _FORMAT_FIELD.match(bare) or _FETCH_COMMAND.match(bare))


def _is_reference(value: str, *, loose: bool = False, strict: bool = False, code: bool = False) -> bool:
    """True when a value points at a secret rather than being one.

    ``strict`` (URL and command forms): only placeholders count, since even
    the word "secret" is the password when a URL holds it. ``loose`` (token,
    secret, and key names, not passwords): numbers, plain lowercase words,
    words with spaces between them, and short values without both letters
    and digits are prose or code. ``code`` (after "=" or ":", not "is"): a
    name that is itself a secret's, as in {"password": password}, is a
    variable passed along; in prose that word may be the password itself.
    """

    candidate = value.strip().strip(_QUOTES).strip()
    if _is_placeholder(candidate):
        return True
    if strict:
        return False
    named = candidate.rstrip(_TRAILING)
    if code and _IDENTIFIER.match(named) and _secret_key(named):
        return True
    lowered = candidate.lstrip("([").strip().lower().rstrip(_TRAILING)
    if len(lowered) < 4 or lowered in _REFERENCE_WORDS:
        return True
    if loose:
        if lowered.isdigit() or _PLAIN_WORDS.match(lowered) or " " in lowered:
            return True
        return len(lowered) < 16 and not (any(character.isdigit() for character in lowered)
                                          and any(character.isalpha() for character in lowered))
    return False
