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

import base64
import re
from dataclasses import dataclass
from typing import Callable


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
# escapes, encrypted-PEM headers, quoted string pieces), and the END line. A
# block cut off without an END line runs to the first character that cannot
# be in a body.
_PRIVATE_KEY_BLOCK = (
    r"-----BEGIN [A-Z0-9 ]*" + _PRIVATE_KEY
    + r"(?:(?!-----END)(?:\\[rn]|[A-Za-z0-9+/=\s:,.\-\"']))*"
    + r"(?:-----END [A-Z0-9 ]*" + _PRIVATE_KEY + r")?"
)

# A prefix that can repeat inside its own token ("sk-sk-sk-...") starts only
# where no token character comes before it.
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # accounts.redeem: a fixed prefix and token_urlsafe(32), independent of the surrounding key name.
    ("KnowItAll2 access key", re.compile(r"(?<![\w-])ki" + r"a_[A-Za-z0-9_-]{43}(?![\w-])")),
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
    ("Tailscale key", re.compile(r"(?<![\w\-])tskey-[A-Za-z0-9\-]{16,}")),
    ("Vault token", re.compile(r"(?<![\w.\-])hv[sbr]\.[A-Za-z0-9_\-]{20,}")),
    ("Docker Hub token", re.compile(r"\bdckr_(?:pat|oat)_[A-Za-z0-9_\-]{20,}")),
    ("SendGrid key", re.compile(r"(?<![\w.\-])SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    ("DigitalOcean token", re.compile(r"\bdo[por]_v1_[a-f0-9]{64}\b")),
    ("age secret key", re.compile(r"\bAGE-SECRET" + r"-KEY-1[0-9AC-HJ-NP-Z]{40,}")),
    ("JSON web token", re.compile(
        r"(?<![\w\-])eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("Azure key", re.compile(r"\b(?:AccountKey|SharedAccessKey)\s*=\s*[A-Za-z0-9+/]{20,}={0,2}", re.IGNORECASE)),
    ("authorization header", re.compile(
        r"\b(?:proxy-)?authorization\s*:\s*(?:basic|bearer|token|digest)?\s*[A-Za-z0-9._~+/=\-]{12,}",
        re.IGNORECASE,
    )),
    # KnowItAll2's own: an agent's key ("kia_" and 43 characters); its codes are below.
    ("KnowItAll2 key", re.compile(r"(?<![\w\-])kia_[A-Za-z0-9_\-]{40,}")),
    ("connection-string password", re.compile(
        r"\b(?:password|pwd)\s*=\s*[^;\s'\"]{3,%d}+\s*;" % _REACH, re.IGNORECASE)),
)


def _encoded_private_key(value: str) -> bool:
    """Whether base64 that decodes to a PEM BEGIN line is a private key's (a certificate's is public)."""

    head = value[:120]
    try:
        decoded = base64.b64decode(head[:len(head) // 4 * 4])
    except ValueError:
        return False
    return b"PRIVATE " + b"KEY" in decoded


def _basic_credentials(value: str) -> bool:
    """Whether base64 decodes to "user:password" with a password, as Docker's "auth" holds."""

    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError:
        return False
    user, colon, password = decoded.partition(b":")
    return bool(colon and user and password)


def _bearer_value(match: str) -> bool:
    """Whether what follows "bearer" looks like a token: a digit, a token symbol, or letters in both cases.

    "bearer authentication-only tokens" is prose.
    """

    value = match.split()[-1]
    return bool(re.search(r"[0-9._~+/=]", value) or (re.search(r"[a-z]", value) and re.search(r"[A-Z]", value)))


# Shapes matched first and judged afterwards; the secret is the "value" group, or the whole match.
_CHECKED_TOKENS: tuple[tuple[str, re.Pattern[str], Callable[[str], bool]], ...] = (
    ("bearer token", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}", re.IGNORECASE), _bearer_value),
    # The KnowItAll2 server's recovery and setup codes: five groups of four from the code alphabet, which has no
    # 0, 1, I or O. A real one holds about 15 different characters; one with fewer than 8 ("XXXX-XXXX-...",
    # "ABCD-ABCD-...") is a placeholder.
    ("KnowItAll2 code", re.compile(r"(?<![\w\-])(?!X{4}-X{4})[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){4}(?![\w\-])"),
     lambda code: len(set(code) - {"-"}) >= 8),
    # A PEM block in base64, as Kubernetes secrets keep it: "-----BEGIN" encodes as "LS0tLS1CRUdJTi".
    ("private key", re.compile(r"(?<![A-Za-z0-9+/])LS0tLS1CRUdJTi[A-Za-z0-9+/]*={0,2}"), _encoded_private_key),
    # Docker's config.json: "auth" holds base64 of "user:password".
    ("registry credentials", re.compile(
        r"[\"']auth[\"'][ \t]*:[ \t]*[\"'](?P<value>[A-Za-z0-9+/]{4,%d}={0,2})[\"']" % _REACH), _basic_credentials),
)
# A PuTTY key file's private lines: as many lines as the header says.
_PUTTY_PRIVATE = re.compile(r"(?<![\w\-])Private-Lines:[ \t]*(?P<lines>\d{1,2})[ \t]*\r?\n(?=[A-Za-z0-9+/=]{16})")

# A value: quoted, or up to the next space, comma, or semicolon.
_VALUE_TEXT = r"\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]{1,%d}" % _REACH
_VALUE_GROUP = r"(?P<value>" + _VALUE_TEXT + r")"
_VALUE = re.compile(_VALUE_TEXT)
_TO_SPACE = re.compile(r"\S*")

# A password may hold '@' and '/': it runs to the last '@' before the host.
# A port ("host:443/...") is never read as a password. The user name may be
# empty, as in a Redis URL ("redis://:<password>@host").
_URL_CREDENTIALS = re.compile(
    r"(?<![\w+.\-])[a-z][a-z0-9+.\-]*+://(?P<user>[^/\s:@]*+):(?!\d{1,5}(?:[/?#\s]|$))"
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
    r"pass(?:word|wd|phrase)?|pwd|psk|secret|token"
    r"|(?:access|auth|refresh|bearer|session|id)[_\-]?token"
    r"|(?:api|access|secret|private|auth|account|encryption|signing|master)[_\-]?key|apikey"
    r"|shared[_\-]?access[_\-]?key|client[_\-]?secret"
)
_SNAKE_KEY = re.compile(r"(?:^|[_\-.])(?:" + _KEY_CORE + r")$", re.IGNORECASE)  # DB_PASSWORD, x-api-key, a.b.secret
_CAMEL_KEY = re.compile(r"[a-z0-9](?:Password|Passwd|Passphrase|Pass|Pwd|Secret|Token|ApiKey|AccessKey|SecretKey"
                        r"|PrivateKey|AuthKey|AccountKey)$")  # dbPassword, clientSecret, $adminPass
_UPPER_KEY = re.compile(r"^[A-Z][A-Z0-9]*(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY)$")  # PGPASSWORD
# "is" and "was" only introduce a secret after a password-like word; "the
# token is required" or "the test pass is flaky" are ordinary prose.
_PROSE_KEY = re.compile(r"pass(?:word|wd|phrase)$", re.IGNORECASE)
# A psk names a WPA key, which has 8 characters at least; "PSK=0x20" is a mode's number in a table.
_PSK_KEY = re.compile(r"(?:^|[_\-.])psk$", re.IGNORECASE)
_HEX_NUMBER = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)

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
# Flags such as --password, --api-key, --authkey, openssl's -passin, and PowerShell's -NewPassword; never
# --password-stdin.
_CLI_FLAG = re.compile(
    r"(?<![\w-])--?(?P<key>(?:[A-Za-z0-9]+-)*(?:[A-Za-z]+(?-i:Password)|pass(?:word|wd|phrase|in|out)?|pwd|secret"
    r"|token|api-?key|access-?key|secret-?key|client-?secret|auth-?token|access-?token|private-?key|auth-?key))"
    r"(?:=|\s+)(?=" + _VALUE_GROUP + r")",
    re.IGNORECASE,
)
# The options before a secret, within one command.
_OPTIONS = r"[^\n|;&]{0,%d}?" % _REACH
# An option's value: quoted, or a word that is not another option.
_ARGUMENT = r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"'\-][^\s,;]{0,%d})" % _REACH
# Passwords given to commands; only a variable or a placeholder is not one. Each form comes with the words
# every match holds (lowercase): a text holding none of them is not searched for that form.
_Form = tuple[tuple[str, ...], re.Pattern[str]]
_COMMAND_FORMS: tuple[_Form, ...] = (
    (("mysql", "mariadb"), re.compile(r"\b(?:mysql|mysqldump|mysqladmin|mariadb|mariadb-dump)\b" + _OPTIONS
                                      + r"\s-p(?P<value>[^\s\-]\S{0,%d})" % _REACH)),
    (("sshpass",), re.compile(r"\bsshpass\b" + _OPTIONS + r"\s-p\s*" + _VALUE_GROUP)),
    (("curl", "wget"), re.compile(r"\b(?:curl|wget)\b" + _OPTIONS + r"\s(?:-u|--user)(?:\s+|=)?[\"']?"
                                  r"(?P<user>[^\s:\"']+):(?P<value>[^\s\"']{1,%d})" % _REACH)),
    (("net",), re.compile(r"\bnet\s+use\b[^\n]{0,%d}?\s/user:\S+\s+(?P<value>[^\s/]\S{0,%d})" % (_REACH, _REACH),
                          re.IGNORECASE)),
    (("net",), re.compile(r"\bnet\s+use\s+(?:[A-Z]:\s+)?\\\\\S+\s+(?P<value>[^\s/]\S{0,%d})\s+/user:" % _REACH,
                          re.IGNORECASE)),
    # .netrc, on one line or several
    (("password",), re.compile(r"(?:\bmachine\s+\S+|(?m:^)[ \t]*default)(?:\s+(?:login|account|port)\s+\S+)*"
                               r"\s+password\s+(?P<value>\S{1,%d})" % _REACH)),
    (("convertto-securestring",), re.compile(
        r"\bConvertTo-SecureString\b(?:\s+-[A-Za-z]+(?::\$\w+)?){0,4}\s+(?P<value>\"[^\"\n]*\"|'[^'\n]*')",
        re.IGNORECASE)),
    (("convertto-securestring",), re.compile(
        r"(?P<value>\"[^\"\n]{0,%d}\"|'[^'\n]{0,%d}')[ \t]*\|[ \t]*ConvertTo-SecureString\b" % (_REACH, _REACH),
        re.IGNORECASE)),
    (("login",), re.compile(r"\b(?:docker|podman|helm)\s+(?:registry\s+)?login\b" + _OPTIONS + r"\s-p(?:\s+|=)"
                            + _VALUE_GROUP)),
    (("smbclient",), re.compile(r"\bsmbclient\b" + _OPTIONS + r"\s(?:-U|--user)(?:\s+|=)?[\"']?(?P<user>[^\s%%\"']+)%%"
                                r"(?P<value>[^\s\"']{1,%d})" % _REACH)),
    (("sqlcmd", "bcp", "osql"), re.compile(r"\b(?i:sqlcmd|bcp|osql)\b" + _OPTIONS + r"\s-P[ \t]*" + _ARGUMENT)),
    (("redis-cli",), re.compile(r"\bredis-cli\b" + _OPTIONS + r"\s-a[ \t]+" + _ARGUMENT)),
    (("mongo",), re.compile(r"\b(?:mongosh|mongodump|mongorestore|mongoexport|mongoimport)\b" + _OPTIONS
                            + r"\s-p(?:[ \t]+|=)" + _ARGUMENT)),
    # "user:password" given to chpasswd
    (("chpasswd",), re.compile(r"(?<![\w.\-])[\w.\-]{1,64}:(?P<value>[^\s'\":|]{1,%d})['\"]?[ \t]*\|[ \t]*"
                               r"(?:sudo[ \t]+)?chpasswd\b" % _REACH)),
    (("chpasswd",), re.compile(r"\bchpasswd\b[^\n|;&]{0,64}?<<<[ \t]*['\"]?[\w.\-]{1,64}:(?P<value>[^\s'\"]{1,%d})"
                               % _REACH)),
    # Cisco: a password given in clear (type 0) or the reversible type 7
    (("username",), re.compile(r"\busername\s+\S{1,64}(?:\s+privilege\s+\d{1,2})?\s+(?:password|secret)\s+[07]\s+"
                               r"(?P<value>\S{1,%d})" % _REACH)),
    (("wpa-psk",), re.compile(r"\bwpa-psk\s+(?:ascii|hex)\s+[07]\s+(?P<value>\S{1,%d})" % _REACH)),
)
# Forms prose can take too ("net user alice to list her groups"): the value must also look like a password
# (quoted, holding a digit or symbol, or a well-known one), as for prose.
_SHAPED_FORMS: tuple[_Form, ...] = (
    (("net",), re.compile(r"\bnet\s+user\s+(?:\"[^\"\n]{1,64}\"|[^\s\"/]\S{0,64})\s+(?P<value>[^\s/]\S{0,%d})"
                          % _REACH, re.IGNORECASE)),
    # htpasswd -b [file] user password ("-n" has no file). A flag word ("-nbB") is short: one of any length,
    # searched for its "b", would be read again from every letter. The spaces between words are never given
    # back, so a long run of them is read once from each start.
    (("htpasswd",), re.compile(r"\bhtpasswd\b" + _OPTIONS + r"\s-[A-Za-z]{0,8}b[A-Za-z]{0,8}\s++"
                               r"(?:-[A-Za-z]{1,8}\s++){0,3}(?:[^\s\-\[]\S{0,%d}\s++)?[^\s\-\[]\S{0,64}\s++" % _REACH
                               + _ARGUMENT)),
    (("nmcli",), re.compile(r"\bnmcli\b" + _OPTIONS + r"\spassword\s+" + _ARGUMENT)),
    (("username",), re.compile(r"\busername\s+\S{1,64}(?:\s+privilege\s+\d{1,2})?\s+(?:password|secret)\s+(?!\d\s)"
                               r"(?P<value>\S{1,%d})" % _REACH)),
    (("requirepass", "masterauth"), re.compile(r"\b(?:requirepass|masterauth)\s+" + _ARGUMENT, re.IGNORECASE)),
    # OpenWrt's "option key" in a sentence or after other text; on a line of its own it is a passphrase form
    (("option",), re.compile(r"(?<![\w\-])option[ \t]+key[ \t]+" + _ARGUMENT)),
)
# Wi-Fi keys, which may be all letters: eight characters at least, as WPA requires. Since any word will do, each
# form is matched only in its own syntax.
_PASSPHRASE_FORMS: tuple[_Form, ...] = (
    (("nmcli",), re.compile(r"\bnmcli\b" + _OPTIONS + r"\s[\w\-]{1,40}\.(?:psk|password|leap-password|wep-key\d)\s+"
                            + _ARGUMENT)),
    (("wpa-psk",), re.compile(r"\bwpa-psk\s+(?:ascii|hex)\s+(?!\d\s)(?P<value>\S{1,%d})" % _REACH)),
    # OpenWrt's "option key <value>" alone on a config line. Elsewhere, or quoted, it is a shaped form: "use the
    # option key shortcuts" is prose.
    (("option",), re.compile(r"(?m:^)[ \t]*option[ \t]+key[ \t]+(?P<value>[^\s\"'\-]\S{0,%d})[ \t\r]*(?m:$)"
                             % _REACH)),
    (("uci",), re.compile(r"\buci\s+set\s+[\w.\-@\[\]]{1,128}\.key=" + _ARGUMENT)),
)
# OpenWrt's other secret options ("option password", "option sae_password", "option auth_secret").
_UCI_OPTION = re.compile(r"(?<![\w\-])option\s+(?P<key>[A-Za-z_]\w{0,63})\s+" + _ARGUMENT)
# XML elements whose name is a secret's, and Windows unattend files' "<Value>" inside one.
_XML_ELEMENT = re.compile(
    r"<(?P<key>[A-Za-z_][\w.\-:]{0,64})(?:[ \t][^<>\n]{0,%d})?>\s{0,64}(?:<Value>\s{0,64})?"
    r"(?P<value>[^<\s][^<\n]{0,%d})(?=</)" % (_REACH, _REACH))
# key="ClearTextPassword" value="..." (NuGet, appSettings), name="password" value="..."
_KEY_VALUE_ATTRIBUTES = re.compile(
    r"\b(?:key|name)[ \t]*=[ \t]*(?P<quote>[\"'])(?P<key>[A-Za-z_][\w.\-:]{0,64})(?P=quote)[ \t]+value[ \t]*=[ \t]*"
    r"(?P<value>\"[^\"\n]{0,%d}\"|'[^'\n]{0,%d}')" % (_REACH, _REACH),
    re.IGNORECASE,
)
# Cookies sent or set: a long value in one is taken for a session's. A Cookie header runs to the end of its line
# or quote; curl's -b, and any --cookie, take one argument, quoted ("value") or not ("word"). Another command's
# -b ("git checkout -b"), and what follows the argument, are no cookies.
_COOKIE_HEADER = re.compile(r"\b(?:set-)?cookie[ \t]*:[ \t]*[\"']?(?P<value>[^\n\"']{1,%d})" % _REACH,
                            re.IGNORECASE)
_COOKIE_OPTION = re.compile(r"(?<![\w-])(?:(?P<b>-b)[ \t]++|--cookie(?:[ \t]++|=))"
                            r"(?:[\"'](?P<value>[^\n\"']{1,%d})|(?P<word>[^\s\"']{1,%d}))" % (_REACH, _REACH))
_COOKIE = re.compile(r"(?<![^\s;,])[^\s=;,]{1,64}=(?P<value>[^\s;,]{16,%d})" % _REACH)
_CURL = re.compile(r"\bcurl\b", re.IGNORECASE)

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
_PATH_PREFIXES = ("<", "${", "$(", "{{", "...", "~/", "~\\", "./", "../", ".\\", "..\\", "\\\\", "file:")
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
# A call, then perhaps fields or subscripts: getpass(), response.json()['access_token'], vault.get('db').value
_CALL = re.compile(r"^[A-Za-z_][\w.]*\([^()\s]*\)(?:\[[^\[\]\s]*\]|\.[A-Za-z_]\w*)*$")
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
        if not _is_reference(match.group("value"), strict=True) and not _example(match):
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
        if not _fits_key(key, value.group()):
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
    lowered = text.lower()
    for kind, pattern, check in _CHECKED_TOKENS:
        group = "value" if "value" in pattern.groupindex else 0
        for match in pattern.finditer(text):
            if check(match.group(group)):
                findings.append(SecretFinding(kind, match.start(group), match.end(group)))
    for match in _PUTTY_PRIVATE.finditer(text) if "private-lines" in lowered else ():
        end = match.end() - 1   # the header's line break; then to the end of its last private line
        for _ in range(max(1, int(match.group("lines")))):
            end = text.find("\n", end + 1)
            if end < 0:
                end = len(text)
                break
        findings.append(SecretFinding("private key", match.start(), end))
    for forms, check in ((_COMMAND_FORMS, _given_strictly), (_SHAPED_FORMS, _given_shaped),
                         (_PASSPHRASE_FORMS, _given_passphrase)):
        for triggers, pattern in forms:
            if not any(word in lowered for word in triggers):
                continue
            for match in pattern.finditer(text):
                if check(match.group("value")) and not _example(match):
                    findings.append(SecretFinding("password", match.start("value"), ends.end(*match.span("value"))))
    for match in _UCI_OPTION.finditer(text) if "option" in lowered else ():
        key, value = match.group("key", "value")
        if _secret_key(key) and _fits_key(key, value) and _given_shaped(value):
            findings.append(SecretFinding(_kind(key), match.start("value"), ends.end(*match.span("value"))))
    for trigger, pattern in (("</", _XML_ELEMENT), ("value", _KEY_VALUE_ATTRIBUTES)):
        for match in pattern.finditer(text) if trigger in lowered else ():
            key = match.group("key").rpartition(":")[2]
            if not _secret_key(key):
                continue
            value = match.group("value").rstrip()
            words = value.strip(_QUOTES).split()
            kind = _kind(key)
            # "<password>kept in Vaultwarden</password>" says where it is.
            if not words or words[0].lower() in _REFERENCE_WORDS or _is_reference(value, loose=kind != "password"):
                continue
            if not _fits_key(key, value):
                continue
            findings.append(SecretFinding(kind, match.start("value"), match.start("value") + len(value)))
    for pattern in (_COOKIE_HEADER, _COOKIE_OPTION) if "cookie" in lowered or "curl" in lowered else ():
        for match in pattern.finditer(text):
            if pattern is _COOKIE_OPTION and match.group("b") and not _curl_option(text, match.start()):
                continue
            group = match.lastgroup   # "value", or "word" for an unquoted argument
            for cookie in _COOKIE.finditer(match.group(group)):
                if not _is_reference(cookie.group("value"), loose=True):
                    start = match.start(group) + cookie.start("value")
                    findings.append(SecretFinding("session cookie", start, start + len(cookie.group("value"))))
    # Two forms may find the same value, as "option key" on a config line of its own.
    return sorted(dict.fromkeys(findings), key=lambda item: (item.start, -item.end))


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


# How documentation writes a user name and password ("user%pass", "user:password"), not one.
_EXAMPLE_USERS = frozenset({"user", "username"})
_EXAMPLE_PASSWORDS = frozenset({"pass", "password", "passwd", "pwd", "pw"})


def _example(match: re.Match[str]) -> bool:
    if "user" not in match.re.groupindex or match.group("user") is None:
        return False
    user = re.split(r"[\\/]", match.group("user").strip(_QUOTES))[-1].lower()   # WORKGROUP\user
    value = match.group("value").strip(_QUOTES).rstrip(_TRAILING).lower()
    return user in _EXAMPLE_USERS and value in _EXAMPLE_PASSWORDS


def _given_strictly(value: str) -> bool:
    """A password given to a command: anything but a variable or a placeholder."""

    return not _is_reference(value, strict=True)


def _given_shaped(value: str) -> bool:
    return not _is_reference(value) and _prose_password(value)


def _given_passphrase(value: str) -> bool:
    return not _is_reference(value) and (_prose_password(value) or len(value.strip(_QUOTES)) >= 8)


def _secret_key(key: str) -> bool:
    return bool(_SNAKE_KEY.search(key) or _CAMEL_KEY.search(key) or _UPPER_KEY.match(key))


def _curl_option(text: str, start: int) -> bool:
    """Whether the option at ``start`` is curl's: curl is named before it in its command, within ``_REACH``.

    A command starts after "|", ";", "&", or a line break, unless the line before ends in "\\" (or PowerShell's
    "`") and goes on. Only the command's own text is read, so each option costs at most ``_REACH`` characters.
    """

    begin = max(0, start - _REACH)
    for mark in "|;&":
        begin = max(begin, text.rfind(mark, begin, start) + 1)
    line = text.rfind("\n", begin, start)
    while line >= 0 and text[max(0, line - 2):line].rstrip("\r").endswith(("\\", "`")):
        line = text.rfind("\n", begin, line)
    command = max(begin, line + 1)
    return "curl" in text[command:start].lower() and bool(_CURL.search(text, command, start))


def _fits_key(key: str, value: str) -> bool:
    """Whether a value can be what its key names: a psk's must be long enough for a WPA key, and not a 0x number."""

    if not _PSK_KEY.search(key):
        return True
    bare = value.strip().strip(_QUOTES)
    return len(bare) >= 8 and not _HEX_NUMBER.fullmatch(bare.rstrip(_TRAILING))


def _kind(key: str) -> str:
    """What a marker calls the secret held under a key name."""

    lowered = key.lower()
    if "pass" in lowered or lowered.endswith(("pwd", "psk")):
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
    if bare.startswith("[REDACTED") or set(bare.lower()) <= {"*", "x", "."} or bare.lower() == "!vault":
        return True
    if candidate.startswith(_PATH_PREFIXES) or _ABSOLUTE_PATH.match(candidate) or _DRIVE_PATH.match(candidate):
        return True
    code = _CODE_REFERENCE.match(bare)
    if code and (code.group("field") is None or _secret_key(code.group("field"))):
        return True
    if _CALL.match(candidate.rstrip(".,:;!?" + _QUOTES)):  # password = getpass(), pwd = Path.cwd(): code, not a value
        return True
    return bool(_VARIABLE_NAME.match(bare) or _CONSTANT.match(bare) or _SHELL_VARIABLE.match(bare)
                or _FORMAT_FIELD.match(bare) or _FETCH_COMMAND.match(bare.removeprefix("(")))


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
