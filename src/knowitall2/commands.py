"""Tool rules and lessons, given to the agent right after a shell command that needs them.

A *tool rule* is one standing rule about how a command behaves on this computer, written by background upkeep
(``learning.tool_rules``) when at least two chats learned the same quirk. It is an ordinary memory (a lesson
tagged ``tool-rule``) with structured tags: the shell it concerns (``shell:bash`` for Claude Code's Bash tool,
``shell:powershell``, ``shell:codex``, ``shell:any``), the programs it applies to (``command:<key>``), an
optional command pattern from the fixed set ``PATTERNS`` (``pattern:<name>``), the error text it explains
(``error:<text>``), and how many chats learned it (``chats:<n>``). Its text says what to do and names the
lessons it came from, which are kept.

The knowledge-file refresh writes ``commands.json`` in the data home: the checked tool rules, most recurrent
first, and the quoted error text of the checked rules and lessons. The agent's hook (Claude Code: after every
Bash or PowerShell command, failed or not) reads only that file, and gives, once per rule in a chat and at most
``HINTS_PER_CHAT`` in all:

- a rule whose shell, programs, and pattern match the command (the first heredoc of a chat teaches the heredoc
  rule before a damaging one is written);
- a rule or checked lesson whose quoted error text appears in the command's output, even when it exited 0;
- never a lesson that a rule given in the chat (or its briefing) was made from, which says what the rule says.

Why (study of 2026-10-13, ``docs/research/2026-10-13-root-causes/quirks.md``): quirks are needed while the agent
writes a command, before anything has failed, and the failure signal the earlier reminder waited for was missing
two times in three (the error buried in the output of a command that exited 0, a command blocked before any
hook, Codex). The study's design (R2) answered 21 of 33 held quirk moments, 14 before the first try, against 3
(none before) for the failure-only reminder this replaces. Replayed with this code on the same 31 chats
(``tools/quirks/rules_replay.py``, rules written by a stand-in for the model, 2026-10-14), as shipped without a
Codex hook: 16 of 33, 14 before the first try, at 4.8 lines a chat (3 in the briefing); with a Codex hook, 20.
A guess is never given (review 2026-10-11: a lesson planted in a cloned repository's README reached every
project), nor a lesson the final pass has not checked yet, nor a rule anyone but the rule writer tagged (review
2026-10-14); recall and the knowledge files still show them, marked.

The hook imports only json, os, and re (and the built-in sha1), and no SQLite, which only the refresh that writes
the index needs. The user accepted about 0.16 s per shell command for it (2026-10-13).
"""

from __future__ import annotations

import json
import os
import re

# The hook runs after every shell command: importing ``typing`` would add about 10 ms to each (2026-10-14).
TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Iterable, Sequence

COMMAND_TAG = "command:"
RULE_TAG = "tool-rule"
SHELL_TAG = "shell:"
PATTERN_TAG = "pattern:"
ERROR_TAG = "error:"
CHATS_TAG = "chats:"
INDEX = "commands.json"
INDEX_VERSION = 2
HINT_LOG = "command_hints.jsonl"
HINT_STATE = "command_hints"         # a folder in the data home: per chat, the rules and lessons it was given
TEXT_LIMIT = 500
# Given at most this many times in a chat (as the failure-only reminder was), and at most two at one command.
HINTS_PER_CHAT = 8
HINTS_PER_COMMAND = 2
EVENTS = ("PostToolUse", "PostToolUseFailure")
HEREDOC = "python heredoc"
# The shells a rule may concern, as the briefing and recall name them.
SHELLS = {"bash": "Claude Code's Bash tool", "powershell": "PowerShell", "codex": "Codex's shell", "any": "any shell"}
TOOL_SHELLS = {"Bash": "bash", "PowerShell": "powershell"}
# Claude Code has a PowerShell tool, and Codex runs PowerShell, only on Windows.
WINDOWS = os.name == "nt"
# Programs too general to be a key alone: a lesson must name the subcommand it is about (a rule may name one
# with a pattern, such as ``node`` with inline code).
GENERAL = frozenset({
    "git", "ssh", "python", "python3", "py", "docker", "bash", "sh", "zsh", "pwsh", "powershell", "cmd", "sudo",
    "cd", "ls", "cat", "grep", "sed", "awk", "echo", "find", "node", "npm", "npx", "go", "dotnet", "make", "pip",
    "java", "cargo", "env", "kubectl", "gh", "glab", "read", "diff", "head", "tail", "sort", "tee", "test", "rm",
    "cp", "mv", "mkdir", "touch", "chmod", "which", "where", "type", "printf", "remove-item", "copy-item",
    "move-item", "new-item", "get-childitem", "get-content", "set-content", "set-location", "write-output",
    "write-host", "select-object", "where-object", "foreach-object", "out-file", "out-null", "out-string",
})
# Only these are given; an index entry without one is never given.
CHECKED = ("observed", "user_stated")
_SEGMENT = re.compile(r"&&|\|\||[;|\n(){}]|\$\(|`")
_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
# Words that only run what follows them.
_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "exec", "then", "do", "else", "elif", "if", "while", "until",
                       "for", "!", "&", ".", "call", "start", "command", "builtin", "xargs"})
_KEY = re.compile(r"[a-z0-9][\w.+-]*(?: [a-z][\w.-]*)?")
# Options before a subcommand that take the next word as their value.
_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "-H", "-i", "-o", "-p", "-u", "-F", "-l", "--git-dir", "--work-tree",
                                 "--host", "--context", "--namespace", "--hostname", "-R", "--repo"})
_WORD = re.compile(r"[a-z][\w.-]*", re.IGNORECASE)
_PYTHONS = ("python", "python3", "py")
_PYTHON_FLAGS = frozenset({"-B", "-P", "-u", "-I", "-E", "-s", "-S", "-O", "-OO", "-q", "-X", "-W"})
# A script run by PowerShell: ``pwsh -NoProfile -File C:\...\Invoke-TacticalSsh.ps1`` (study, Q3).
_FILE_ARGUMENT = re.compile(r"(?i)(?<![\w-])(?:pwsh|powershell)(?:\.exe)?\b[^\n|;&]*?\s-File\s+(\"[^\"]+\"|'[^']+'|\S+)")
# A Bash heredoc and its body, up to the line holding only its word (or the end).
_HEREDOC = re.compile(r"(?<!<)<<-?(?!<)[ \t]*(['\"]?)([A-Za-z_]\w*)\1[^\n]*\n(.*?)(?:\n[ \t]*\2[ \t]*(?=\n|$)|\Z)",
                      re.DOTALL)


def _base(word: str) -> str:
    return re.sub(r"\.(?:exe|cmd|bat)$", "", re.split(r"[\\/]", word)[-1].casefold())


def keys(command: str, tool: str = "") -> set[str]:
    """Each program a shell command runs, with its subcommand: 'git archive' and 'git' for 'git archive HEAD'.

    Read the way agents call tools (study 2026-10-13, H3): the script after ``pwsh -File``, a quoted program
    path (``& 'C:\\...\\python.exe' x.py``), and in Claude Code's Bash tool any heredoc, not only Python's
    (``cat > shot.cjs <<'EOF'`` then run, Q7 and E12).
    """

    found: set[str] = set()
    text = command or ""
    for segment in _SEGMENT.split(_HEREDOC.sub("", text)):
        words = [word.strip("\"'") for word in _TOKEN.findall(segment)]
        while words:
            if words[0] in _WRAPPERS or re.fullmatch(r"\w+=\S*", words[0]):
                words = words[1:]
                while words and words[0].startswith("-"):   # sudo -n docker ...
                    words = words[1:]
            elif words[0] == "timeout":   # timeout 600 python ...
                words = words[2:] if len(words) > 1 and re.fullmatch(r"[\d.]+[smhd]?", words[1]) else words[1:]
            else:
                break
        if not words:
            continue
        program = _base(words[0])
        rest = words[1:]
        while program in _PYTHONS and rest and rest[0] in _PYTHON_FLAGS:   # python -B -X utf8 x.py
            rest = rest[2:] if rest[0] in ("-X", "-W") else rest[1:]
        if program in _PYTHONS and rest and rest[0].casefold().endswith(".py"):
            program, rest = re.split(r"[\\/]", rest[0])[-1].casefold(), rest[1:]
        elif program in _PYTHONS and len(rest) > 1 and rest[0] == "-m":
            rest = rest[1:]   # python -m unittest: "python unittest"
        if not _KEY.fullmatch(program):
            continue
        found.add(program)
        while rest and rest[0].startswith("-"):   # git -C /repo status, ssh -o BatchMode=yes host
            rest = rest[2:] if rest[0] in _OPTIONS_WITH_VALUE else rest[1:]
        if rest and _WORD.fullmatch(rest[0]):
            found.add(f"{program} {rest[0].casefold()}")
    for match in _FILE_ARGUMENT.finditer(text):
        found.add(re.split(r"[\\/]", match.group(1).strip("\"'"))[-1].casefold())
    if tool == "Bash" and _HEREDOC.search(text):
        found.add(HEREDOC)   # the Bash tool's heredoc quirk is about any heredoc, not only Python's
    # A heredoc fed to Python here, not one inside a command run elsewhere (ssh host 'python3 - <<EOF').
    elif re.search(r"^(?:(?!\b(?:ssh|wsl(?:\.exe)?|docker\s+exec)\b)[^\n])*\bpython[\d.]*(?:\.exe)?\b[^\n]*<<",
                   text, re.IGNORECASE | re.MULTILINE):
        found.add(HEREDOC)
    return found


# --- Command patterns ------------------------------------------------------------------------------------------
# A small fixed set: each is a way of writing a command that a known quirk breaks, whatever program it runs.

def _heredoc_escapes(command: str) -> bool:
    return any(re.search(r"[\\'\"`]", match.group(3)) for match in _HEREDOC.finditer(command))


_SLEEP_CHAIN = re.compile(r"(?<![\w-])sleep\s+\d+(?:\.\d+)?[smhd]?[ \t]*(?:;|&&|\|\||\n)[ \t]*\S", re.IGNORECASE)
_EMPTY_ARGUMENT = re.compile(r"(?<![\w\"'])-{1,2}[A-Za-z][\w-]*\s+(?:''|\"\"|'\"\"'|\"''\")(?=$|[\s;|)])")
_FOREACH = re.compile(r"(?<![.\w$-])foreach\s*\(", re.IGNORECASE)
_HERE_STRING_PIPE = re.compile(r"@(['\"])[ \t]*\r?\n.*?\r?\n\1@[ \t]*\|", re.DOTALL)
_INLINE_CODE = re.compile(r"(?i)(?<![\w.-])(?:node|deno|python[\d.]*|py)(?:\.exe)?(?:\s+-[A-Za-z]+)*?\s+-[ec]\s+"
                          r"(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)')")


def _closing(text: str, start: int, opening: str, closing: str) -> int:
    """The index just past the bracket that closes the one at ``start``, or -1."""

    depth = 0
    for position in range(start, len(text)):
        if text[position] == opening:
            depth += 1
        elif text[position] == closing:
            depth -= 1
            if depth == 0:
                return position + 1
    return -1


def _foreach_pipe(command: str) -> bool:
    for match in _FOREACH.finditer(command):
        end = _closing(command, match.end() - 1, "(", ")")
        brace = re.compile(r"\s*\{").match(command, end) if end > 0 else None
        if brace is None:
            continue
        end = _closing(command, brace.end() - 1, "{", "}")
        if end > 0 and re.compile(r"[ \t]*\|(?!\|)").match(command, end):
            return True
    return False


def _inline_code_quotes(command: str) -> bool:
    for match in _INLINE_CODE.finditer(command):
        double, single = match.group(1), match.group(2)
        if double is not None and re.search(r"['\\]", double):
            return True
        if single is not None and re.search(r"[\"\\]", single):
            return True
    return False


# name: (what it is, in the words the rule writer and recall see; whether a command matches it)
PATTERNS: dict[str, tuple[str, Any]] = {
    "heredoc-escapes": ("a Bash heredoc whose body holds a backslash or a quote", _heredoc_escapes),
    "sleep-chain": ("`sleep N` followed by another command, as in `sleep 30; ls` or `sleep 30 && ls`",
                    lambda command: bool(_SLEEP_CHAIN.search(command))),
    "empty-argument": ("an option given an empty quoted value, such as `-N \"\"` or `-N '\"\"'`",
                       lambda command: bool(_EMPTY_ARGUMENT.search(command))),
    "foreach-pipe": ("piping from a PowerShell `foreach (...) { ... }` statement",
                     _foreach_pipe),
    "here-string-pipe": ("a PowerShell here-string (`@' ... '@`) piped into a command",
                         lambda command: bool(_HERE_STRING_PIPE.search(command))),
    "inline-code-quotes": ("inline code (`python -c`, `node -e`) holding a backslash or a quote of the other kind",
                           _inline_code_quotes),
}


def matched_patterns(command: str) -> set[str]:
    """The names of the patterns this command shows."""

    return {name for name, (_, test) in PATTERNS.items() if test(command or "")}


# --- Quoted error text ------------------------------------------------------------------------------------------

# A single quote opens or closes a quotation only beside a non-word character: "Alpha's build prints '...'".
# Compiled when first used, by re's own cache: only the refresh and the rule writer read memories' quotations.
_QUOTED = r"(?<!\w)'([^'\n]{10,140})'(?!\w)|\"([^\"\n]{10,140})\"|\u201c([^\u201d\n]{10,140})\u201d"
_ERRORISH = (r"(?i)error|denied|not found|cannot|can't|could not|unable|failed|invalid|unexpected|refused|"
             r"not allowed|no such|unknown|in use|dubious|pathspec|illegal|not recognized|timed out|"
             r"not supported|missing|not implemented|blocked|positional|already")
_CODES = r"(?i)\b(?:os error \d+|exit code 0x[0-9a-f]+)"
# Phrases too common to point to one quirk: with them, matching on two words fired 57 times in the replay,
# mostly on "permission denied" and "no such file".
GENERIC = frozenset({
    "permission denied", "no such file", "no such file or directory", "cannot be reached", "connection refused",
    "not found", "command not found", "unexpected eof", "access is denied", "timed out", "connection timed out",
    "could not be found", "is not recognized", "operation not permitted", "file not found",
})
ERROR_CHARACTERS = 140
ERRORS_PER_MEMORY = 3


def normal(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _specific(phrase: str) -> bool:
    low = normal(phrase)
    return (len(low.split()) >= 3 and low not in GENERIC
            and not any(low.startswith(generic + " ") and len(low) < len(generic) + 6 for generic in GENERIC))


def error_phrases(text: str) -> list[str]:
    """The error text a memory quotes: three words or more that read as an error and are not a generic phrase,
    or an error code such as "os error 123", normalized."""

    found: list[str] = []
    for match in re.finditer(_QUOTED, text or ""):
        phrase = next(group for group in match.groups() if group).strip(" .:")
        if re.search(_ERRORISH, phrase) and not re.search(r"[`$]|^\s*-", phrase) and _specific(phrase):
            found.append(normal(phrase))
    found += [normal(match.group(0)) for match in re.finditer(_CODES, text or "")]
    return list(dict.fromkeys(found))


def valid_error(error: object) -> str:
    """Error text as a rule may quote it, normalized; '' for one too short, too long, or too generic."""

    if not isinstance(error, str):
        return ""
    phrase = normal(error.strip().strip("\"'`\u201c\u201d").strip(" .:"))
    if not phrase or len(phrase) > ERROR_CHARACTERS:
        return ""
    return phrase if _specific(phrase) or re.fullmatch(_CODES, phrase) else ""


# --- Lesson tags ------------------------------------------------------------------------------------------------

def valid_key(key: object) -> str | None:
    """A key as the learner may write it, normalized; None for anything else (too general, or not a command)."""

    if not isinstance(key, str):
        return None
    key = " ".join(key.casefold().split())
    if key == HEREDOC:
        return key
    if not _KEY.fullmatch(key) or key in GENERAL or len(key) < 3:
        return None
    return key


# A program fits in a tag of 80 characters with "command:" before it (review 2026-10-14, E-L8).
PROGRAM_CHARACTERS = 72


def valid_program(program: object, *, general: bool = False) -> str | None:
    """A program a rule may name: a key, or with ``general`` (the rule has a pattern) a general program too."""

    if not isinstance(program, str):
        return None
    if re.search(r"[\\/]", program):   # a path: the program is its last part
        program = re.split(r"[\\/]", program)[-1]
    words = " ".join(program.split()).split(" ")
    program = " ".join([_base(words[0]), *(word.casefold() for word in words[1:])])
    if program == HEREDOC:
        return program
    if not _KEY.fullmatch(program) or not 2 <= len(program) <= PROGRAM_CHARACTERS or (
            program in GENERAL and not general):
        return None
    return program


def valid_where(where: object) -> str:
    """The word a command must contain for a lesson to apply (a host, a target, a path part), or ''."""

    if not isinstance(where, str):
        return ""
    where = where.strip().casefold()
    return where if _WHERE.fullmatch(where) else ""


_WHERE = re.compile(r"[\w.:/\\@-]{3,80}")
_WHERE_MARK = " @ "


def tag(key: str, where: str = "") -> str:
    return COMMAND_TAG + key + (_WHERE_MARK + where if where else "")


def tagged_keys(tags: Iterable[str]) -> list[tuple[str, str]]:
    """The (key, where) of each command tag: "command:git status @ rmm-host" is ("git status", "rmm-host")."""

    found = []
    for item in tags:
        if isinstance(item, str) and item.startswith(COMMAND_TAG):
            key, _, where = item[len(COMMAND_TAG):].partition(_WHERE_MARK)
            if valid_key(key):
                found.append((valid_key(key), valid_where(where)))
    return found


def rule_fields(tags: Iterable[str]) -> dict[str, Any] | None:
    """A tool rule's shell, programs, pattern, error text, and chats, from its tags; None for any other memory."""

    tags = [item for item in tags if isinstance(item, str)]
    if RULE_TAG not in tags:
        return None
    fields: dict[str, Any] = {"shell": "any", "programs": [], "pattern": "", "error": "", "chats": 0}
    for item in tags:
        if item.startswith(SHELL_TAG) and item[len(SHELL_TAG):] in SHELLS:
            fields["shell"] = item[len(SHELL_TAG):]
        elif item.startswith(PATTERN_TAG) and item[len(PATTERN_TAG):] in PATTERNS:
            fields["pattern"] = item[len(PATTERN_TAG):]
        elif item.startswith(ERROR_TAG):
            fields["error"] = valid_error(item[len(ERROR_TAG):])
        elif item.startswith(CHATS_TAG) and item[len(CHATS_TAG):].isdigit():
            fields["chats"] = int(item[len(CHATS_TAG):])
        elif item.startswith(COMMAND_TAG):
            program = valid_program(item[len(COMMAND_TAG):].partition(_WHERE_MARK)[0], general=True)
            if program and program not in fields["programs"]:
                fields["programs"].append(program)
    return fields


def rule_tags(*, shell: str, programs: Sequence[str], pattern: str, error: str, chats: int) -> list[str]:
    """The tags that hold a rule (at most eight, each at most 80 characters, as ``Memory.remember`` allows)."""

    tags = [RULE_TAG, SHELL_TAG + shell, *(COMMAND_TAG + program for program in programs[:3])]
    if pattern:
        tags.append(PATTERN_TAG + pattern)
    if error:
        # A prefix of the error text still finds it in an output.
        tags.append(ERROR_TAG + error[:80 - len(ERROR_TAG)].rstrip())
    tags.append(f"{CHATS_TAG}{chats}")
    return tags


_LEARNED = " Learned in "
SOURCES_NAMED = 80
# The rule writer's own name (``Memory.agent``), which syncs with each memory: a rule is trusted only from it.
RULES_AGENT = "tool-rules"
# Tags of a memory not yet checked: the quick pass's, until the final pass decides, and the weaker side of a
# disagreement (``memory.PROVISIONAL_TAG``, ``memory.DISPUTED_TAG``; named here so the hook imports no memory).
UNCHECKED_TAGS = ("provisional", "disputed")


def rule_text(*, advice: str, shell: str, programs: Sequence[str], pattern: str, error: str, chats: int,
              sources: Sequence[str]) -> str:
    """A rule as recall and the briefing show it: what to do, what it applies to, the error, and its sources."""

    where = [f"`{program}`" for program in programs]
    if pattern:
        where.append(PATTERNS[pattern][0])
    applies = " or ".join(where) or "commands"
    text = f"{advice.strip().rstrip('.')}. Tool rule for {applies} in {SHELLS[shell]}"
    if error:
        text += f'; it explains the error "{error}"'
    # Every source is named (up to a limit no group reaches): the index trusts a rule only for the lessons it
    # names that still stand (``rule_chats``).
    return f"{text}.{_LEARNED}{chats} chats, from {', '.join(sources[:SOURCES_NAMED])}."


def rule_summary(text: str) -> str:
    """A rule's text without the lessons it came from: for the hook and the briefing."""

    return text.split(_LEARNED, 1)[0]


def rule_sources(text: str) -> list[str]:
    """The ids of the lessons a rule names as its sources."""

    return list(dict.fromkeys(RECORD_IDS.findall(text.split(_LEARNED, 1)[1] if _LEARNED in text else "")))


def rule_provenance(text: str) -> str:
    """The end of a rule's text that names the lessons it came from (" Learned in 3 chats, from k-..."), or ''."""

    return text[text.index(_LEARNED):] if _LEARNED in text else ""


def folded_sessions(connection: Any, ids: Iterable[str]) -> dict[str, set[str]]:
    """For each memory, the chats of it and of everything superseded into it, through any number of steps: a
    lesson maintenance merged as a duplicate, combined, or replaced still counts as learned in its chat, through
    the memory that holds it now (review 2026-10-14 b, F-H1). A memory saved without a session adds none."""

    found: dict[str, set[str]] = {}
    wanted = list(dict.fromkeys(ids))
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        rows = connection.execute(
            "WITH RECURSIVE chain(root, id, session) AS ("
            f" SELECT id, id, source_session FROM records WHERE id IN ({', '.join('?' * len(chunk))})"
            " UNION SELECT c.root, r.id, r.source_session FROM records r JOIN chain c ON r.superseded_by = c.id"
            "  WHERE r.status = 'superseded')"
            " SELECT root, session FROM chain", chunk)
        for root, session in rows:
            found.setdefault(root, set())
            if session:
                found[root].add(session)
    return found


def source_lookup(connection: Any, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Each lesson a rule names, as the memory that holds it now (itself, or what it was superseded into): its id
    ("holder"), status, verification, tags, and the chats folded into it, for ``rule_chats``."""

    rows: dict[str, dict[str, Any]] = {}
    frontier = list(dict.fromkeys(ids))
    for _ in range(20):   # supersession chains are short; this bounds a cycle
        wanted = [record_id for record_id in frontier if record_id not in rows]
        if not wanted:
            break
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            for record_id, status, verification, raw, superseded_by in connection.execute(
                    "SELECT id, status, verification, tags, superseded_by FROM records WHERE id IN "
                    f"({', '.join('?' * len(chunk))})", chunk):
                try:
                    tags = json.loads(raw or "[]")
                except ValueError:
                    tags = []
                rows[record_id] = {"status": status, "verification": verification,
                                   "tags": tags if isinstance(tags, list) else [], "superseded_by": superseded_by}
        frontier = [row["superseded_by"] for row in rows.values()
                    if row["status"] == "superseded" and row["superseded_by"] and row["superseded_by"] not in rows]
    holders: dict[str, str] = {}
    for record_id in dict.fromkeys(ids):
        current, seen = record_id, set()
        while (current in rows and rows[current]["status"] == "superseded" and rows[current]["superseded_by"] in rows
               and current not in seen):
            seen.add(current)
            current = rows[current]["superseded_by"]
        if current in rows:
            holders[record_id] = current
    chats = folded_sessions(connection, set(holders.values()))
    return {record_id: {**rows[holder], "holder": holder, "chats": chats.get(holder, set())}
            for record_id, holder in holders.items()}


def rule_chats(text: str, lookup: dict[str, dict[str, Any]], *, standing: bool = False) -> int:
    """How many chats the lessons a rule names were learned in, counted through the memories that hold them now;
    0 when fewer than two, or none of those memories checked.

    By default, for giving the rule, a lesson not yet checked by the final pass or disputed is left out until it
    is decided. With ``standing``, for deciding whether to retire the rule, it counts: a dispute may be settled in
    its favour (review b, F-M1). A chat is a real session, as ``Store.said_in_sessions`` counts them (E-M2); the
    count is made here, never read from the ``chats:`` tag, which a memory synced from elsewhere could set to
    anything (E-H1).
    """

    holders: dict[str, dict[str, Any]] = {}
    for record_id in rule_sources(text):
        found = lookup.get(record_id)
        if found is not None and found["status"] == "active":
            holders[found["holder"]] = found
    if not standing:
        holders = {key: found for key, found in holders.items() if not set(UNCHECKED_TAGS) & set(found["tags"])}
    chats = set().union(*(found["chats"] for found in holders.values())) if holders else set()
    if len(chats) < 2 or not any(found["verification"] in CHECKED for found in holders.values()):
        return 0
    return len(chats)


def trusted_chats(agent: str | None, verification: str, text: str, lookup: dict[str, dict[str, Any]]) -> int:
    """How many chats a rule is given for: only the rule writer's, from lessons still held in two chats; one the
    user restored or corrected counts on the user's word (review b, F-L4). 0: not given."""

    if agent != RULES_AGENT:
        return 0
    chats = rule_chats(text, lookup)
    return max(chats, 1) if verification == "user_stated" else chats


# --- The index the hook reads ------------------------------------------------------------------------------------

def build_index(connection: Any) -> dict[str, Any]:
    """The checked tool rules, most recurrent first, and the quoted error text of checked rules and lessons
    (redacted), each with the project folders it applies in when it is a project's."""

    from .secrets import redact

    folders: dict[str, list[str]] = {}
    for project_id, path in connection.execute("SELECT project_id, path FROM project_paths"):
        folders.setdefault(project_id, []).append(str(path))
    rules: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    rows = connection.execute(
        "SELECT id, kind, text, tags, scope, project_id, verification, source_agent FROM records "
        f"WHERE status = 'active' AND kind IN ('lesson', 'procedure', 'fact') "
        f"AND verification IN ({', '.join('?' * len(CHECKED))}) ORDER BY created_at DESC, seq DESC",
        CHECKED,
    ).fetchall()
    named = [record_id for _, _, text, raw, *_ in rows if f'"{RULE_TAG}"' in (raw or "")
             for record_id in rule_sources(str(text))]
    lookup = source_lookup(connection, named)
    for record_id, kind, text, raw, scope, project_id, verification, agent in rows:
        try:
            tags = json.loads(raw or "[]")
        except ValueError:
            continue
        tags = tags if isinstance(tags, list) else []
        if set(UNCHECKED_TAGS) & set(tags):
            continue   # not yet checked by the final pass, or disputed (review 2026-10-14, E-H2)
        fields = rule_fields(tags)
        if fields is not None:
            # Only the rule writer's, from lessons that still stand in two chats (E-H1, E-M1).
            chats = trusted_chats(agent, verification, str(text), lookup)
            if not chats:
                continue
            fields["chats"] = chats
        if fields is None and kind != "lesson" and not tagged_keys(tags):
            continue   # a procedure or fact quoting an error says how a system is, not what an error means
        body = redact(" ".join(str(text).split()))
        entry: dict[str, Any] = {"id": record_id, "verification": verification}
        if scope == "project":
            entry["folders"] = folders.get(project_id, [])
        phrases = error_phrases(body)
        if fields is not None:
            entry["text"] = rule_summary(body)[:TEXT_LIMIT]
            # The lessons it was made from, as named and as held now, which the hook does not give beside it.
            named = rule_sources(str(text))
            entry["sources"] = list(dict.fromkeys([*named, *(lookup[item]["holder"] for item in named
                                                             if item in lookup)]))
            if not (fields["programs"] or fields["pattern"] or fields["error"]):
                continue
            entry["shell"] = fields["shell"]
            rules.append({**entry, **{name: fields[name] for name in ("programs", "pattern", "chats")}})
            phrases = [fields["error"], *phrases] if fields["error"] else phrases
        else:
            entry["text"] = body[:TEXT_LIMIT]
        for phrase in list(dict.fromkeys(phrases))[:ERRORS_PER_MEMORY]:
            errors.append({**entry, "phrase": phrase, "rule": fields is not None})
    rules.sort(key=lambda rule: -rule["chats"])   # stable: newest first among equals
    errors.sort(key=lambda item: not item["rule"])
    return {"version": INDEX_VERSION, "rules": rules, "errors": errors}


def write_index(connection: Any, home: Path) -> None:
    from .files import write_text_atomic

    write_text_atomic(home / INDEX, json.dumps(build_index(connection), ensure_ascii=True, sort_keys=True) + "\n")


# --- Matching ---------------------------------------------------------------------------------------------------

def shells_for(shell: str) -> set[str]:
    """The rule shells that apply in ``shell``: Codex runs PowerShell on Windows."""

    shells = {shell, "any"}
    if shell == "codex" and WINDOWS:
        shells.add("powershell")
    return shells


def places(cwd: str) -> set[str]:
    """The agent's folder as it may be named: as given, and resolved (a short 8.3 name, a link). Resolved once a
    command: on a network share each resolution is a round trip (review 2026-10-14, E-L8)."""

    if not cwd:
        return set()
    return {os.path.normcase(os.path.abspath(cwd)), os.path.normcase(os.path.realpath(cwd))}


def in_folder(entry: dict[str, Any], cwd: str, here: set[str] | None = None) -> bool:
    """Whether a rule or lesson applies in this folder: a project's only inside that project."""

    if entry.get("verification") not in CHECKED:
        return False
    folders = entry.get("folders")
    if folders is None:
        return True
    here = places(cwd) if here is None else here
    # A project's folders are kept as git found them.
    return any(place == os.path.normcase(folder) or place.startswith(os.path.normcase(folder).rstrip("\\/") + os.sep)
               for place in here for folder in folders)


def rule_matches(rule: dict[str, Any], *, command: str, shell: str, found: set[str], patterns: set[str]) -> bool:
    """Whether a rule's shell, programs, and pattern all fit this command (a rule with neither programs nor a
    pattern is given only by its error text)."""

    programs, pattern = rule.get("programs") or [], rule.get("pattern") or ""
    if rule.get("shell", "any") not in shells_for(shell) or not (programs or pattern):
        return False
    if programs and not found & set(programs):
        return False
    return not pattern or pattern in patterns


# Output that is KnowItAll2's own (a recall, its knowledge files) quotes what it knows, not an error that happened.
RECORD_IDS = re.compile(r"\bk-[0-9a-f]{10}\b")
_OWN_OUTPUT = re.compile(r"\bk-[0-9a-f]{10}\b|knowitall2-known")
OUTPUT_CHARACTERS = 20000


def pick(index: dict[str, Any], *, command: str, shell: str, output: str, cwd: str,
         seen: Iterable[str] = (), limit: int = HINTS_PER_COMMAND) -> list[tuple[dict[str, Any], str]]:
    """What to give after this command: (rule or lesson, why), the error text it found first, then rules that
    match the command, most recurrent first; never one in ``seen``."""

    skip = set(seen)
    chosen: list[tuple[dict[str, Any], str]] = []
    resolved: list[set[str]] = []

    def applies(entry: dict[str, Any]) -> bool:
        if entry.get("folders") is not None and not resolved:
            resolved.append(places(cwd))
        return in_folder(entry, cwd, resolved[0] if resolved else set())

    text = output or ""
    if len(text) > 2 * OUTPUT_CHARACTERS:
        text = text[:OUTPUT_CHARACTERS] + "\n" + text[-OUTPUT_CHARACTERS:]
    said = normal(text)
    if said and not _OWN_OUTPUT.search(said):
        shells = shells_for(shell)
        for entry in index.get("errors") or []:
            if len(chosen) >= limit:
                return chosen
            if (entry.get("id") in skip or not entry.get("phrase") or entry["phrase"] not in said
                    or entry.get("rule") and entry.get("shell", "any") not in shells):
                continue
            if applies(entry):
                chosen.append((entry, "error"))
                skip.add(entry["id"])
    rules = index.get("rules") or []
    if rules and len(chosen) < limit:
        found = keys(command, {"bash": "Bash", "powershell": "PowerShell"}.get(shell, ""))
        patterns = matched_patterns(command)
        for rule in rules:
            if len(chosen) >= limit:
                break
            if rule.get("id") in skip or not applies(rule):
                continue
            if rule_matches(rule, command=command, shell=shell, found=found, patterns=patterns):
                chosen.append((rule, "command"))
                skip.add(rule["id"])
    return chosen


def briefing_rules(rules: Sequence[dict[str, Any]], *, agent: str | None, limit: int = 3) -> list[dict[str, Any]]:
    """The tool rules for the briefing: those for this agent's shells, most recurrent first."""

    if agent == "claude-code":
        shells = {"bash", "any"} | ({"powershell"} if WINDOWS else set())
    elif agent == "codex":
        shells = shells_for("codex")
    else:
        shells = set(SHELLS)
    chosen = [rule for rule in rules if rule.get("shell", "any") in shells]
    return sorted(chosen, key=lambda rule: -int(rule.get("chats") or 0))[:limit]


# --- The hook ----------------------------------------------------------------------------------------------------

# A command one shell hands another: pwsh -NoProfile -Command '<inner>', pwsh -EncodedCommand <base64>, or
# bash -lc '<inner>', after options that may take a word (-ExecutionPolicy Bypass, -o pipefail).
_WRAPPED = re.compile(
    r"""^\s*(?:&\s*)?(?P<program>"[^"]*"|'[^']*'|\S+)(?:\s+-{1,2}[A-Za-z][\w-]*(?:\s+[A-Za-z]\w*)?)*?"""
    r"""\s+(?P<flag>-Command|-EncodedCommand|-e|-[A-Za-z]*c)\s+(?P<argument>'.*'|".*"|[A-Za-z0-9+/=]+)\s*$""",
    re.DOTALL | re.IGNORECASE)
_POWERSHELLS = ("pwsh", "powershell")
_POSIX_SHELLS = ("bash", "sh", "zsh")
# A single-quoted Bash word may close, add a quote, and open again: '\'' or '"'.
_BASH_QUOTE = re.compile(r"'\\''|'\"'\"'")


def _shell_inner(program: str, flag: str, argument: str, *, quoted: bool) -> tuple[str, str] | None:
    """What ``program flag argument`` runs, and in which shell; None when it is not one whole command for a shell.
    ``argument`` is as written when ``quoted`` (one quoted word), else one word of an argument vector."""

    shell = _base(program.strip("\"'"))
    if shell in _POWERSHELLS and len(flag) > 1 and (flag.casefold() == "-ec"
                                                    or "-encodedcommand".startswith(flag.casefold())):
        import binascii   # built in, as cheap as re

        try:
            return binascii.a2b_base64(argument.encode("ascii"), strict_mode=True).decode("utf-16-le"), "powershell"
        except (ValueError, UnicodeError):
            return None
    if shell in _POWERSHELLS and flag.casefold() in ("-c", "-command"):
        kind = "powershell"
    elif shell in _POSIX_SHELLS and re.fullmatch(r"-[a-z]*c", flag):
        kind = "bash"
    else:
        return None
    if not quoted:
        return argument, kind
    quote, inner = argument[:1], argument[1:-1]
    if quote not in ("'", '"') or len(argument) < 2:
        return None
    if kind == "powershell" and quote == "'":
        # In PowerShell a quote inside a single-quoted string is doubled; a lone one would end the string.
        return (inner.replace("''", "'"), kind) if "'" not in inner.replace("''", "") else None
    if kind == "powershell":
        escaped = re.compile(r'`.|""', re.DOTALL)
        return (escaped.sub(lambda match: match.group(0)[-1], inner), kind) if '"' not in escaped.sub("", inner) else None
    if quote == "'":
        return (_BASH_QUOTE.sub("'", inner), kind) if "'" not in _BASH_QUOTE.sub("", inner) else None
    # In Bash's double quotes a backslash escapes only $ ` " \ and a newline.
    if '"' in re.sub(r"\\.", "", inner, flags=re.DOTALL):
        return None
    return re.sub(r"\\([$`\"\\\n])", r"\1", inner), kind


def unwrap(command: str) -> tuple[str, str]:
    """The command a shell was asked to run, and which shell: ("<inner>", "powershell") for pwsh or
    powershell -Command '<inner>' (or -EncodedCommand), ("<inner>", "bash") for bash or sh -c '<inner>', else
    (command, ""). Only one whole quoted word is unwrapped: ``bash -c 'a' && bash -c 'b'`` is left as it is."""

    match = _WRAPPED.match(command or "")
    if match is None:
        return command, ""
    found = _shell_inner(match.group("program"), match.group("flag"), match.group("argument"),
                         quoted=match.group("argument")[:1] in ("'", '"'))
    return found if found is not None else (command, "")


def unwrap_argv(words: Sequence[str]) -> tuple[str, str]:
    """``unwrap`` for a command given as its argument vector, such as ["bash", "-lc", "git status"]: the last
    word is what the shell runs. Any other vector is joined with spaces."""

    if len(words) >= 3:
        found = _shell_inner(words[0], words[-2], words[-1], quoted=False)
        if found is not None:
            return found
    return " ".join(words), ""


def _output(payload: dict[str, Any]) -> str:
    """What the command printed: Claude Code gives ``tool_response`` after a command, ``error`` after a failure."""

    parts = []
    response = payload.get("tool_response")
    if isinstance(response, str):
        parts.append(response)
    elif isinstance(response, dict):
        parts += [str(response[name]) for name in ("stdout", "stderr", "output", "error", "result")
                  if isinstance(response.get(name), str)]
    if isinstance(payload.get("error"), str):
        parts.append(payload["error"])
    return "\n".join(parts)


def hint(payload: dict[str, Any], home: str, *, agent: str = "claude-code") -> str:
    """The context to give the agent after this shell command, or ''. Never raises."""

    try:
        event = str(payload.get("hook_event_name") or "")
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        wrapper = ""
        if isinstance(command, list) and all(isinstance(word, str) for word in command):
            command, wrapper = unwrap_argv(command)   # an engine that passes the argument vector
        if not isinstance(command, str) or event not in EVENTS:
            return ""
        try:
            with open(os.path.join(home, INDEX), encoding="utf-8") as stream:
                index = json.load(stream)
        except (OSError, ValueError):
            return ""
        if not isinstance(index, dict) or index.get("version") != INDEX_VERSION:
            return ""   # an index written before 0.11.1 holds lessons by command, not rules
        if agent == "codex":
            # Codex names every shell command "Bash" (codex-cli 0.162, 2026-10-15); its shell is PowerShell on
            # Windows. A command it wraps in pwsh -Command '...' or bash -lc '...' is matched inside.
            if not wrapper:
                command, wrapper = unwrap(command)
            shell = "powershell" if wrapper == "powershell" and not WINDOWS else "codex"
        else:
            shell = TOOL_SHELLS.get(str(payload.get("tool_name") or ""), "any")
        chat = _chat(payload)
        folder = os.path.join(home, HINT_STATE)
        state_path = os.path.join(folder, f"{chat}.json")
        try:
            with open(state_path, encoding="utf-8") as stream:
                state = json.load(stream)
        except (OSError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        # "given", not 0.11.0's "shown" (the command keys of its failure reminders), which a chat continuing
        # across the update still holds and which must not count toward this cap (review 2026-10-14, E-L8).
        given = state.setdefault("given", [])
        room = min(HINTS_PER_COMMAND, HINTS_PER_CHAT - len(given))
        if room <= 0:
            return ""
        # A rule says what its source lessons say: once it is given, they are not (review 2026-10-15).
        covered = [item for item in state.get("covered") or [] if isinstance(item, str)] if isinstance(
            state.get("covered"), list) else []
        chosen = pick(index, command=command, shell=shell, output=_output(payload), cwd=str(payload.get("cwd") or ""),
                      seen=[*given, *covered], limit=room + TOLD_SLACK)
        if chosen:   # the briefing's memories are read only when something would be given
            told = set(_told(home, payload))
            told |= _sources(rule for rule in index.get("rules") or []
                             if isinstance(rule, dict) and rule.get("id") in told)
            chosen = [(entry, why) for entry, why in chosen if entry["id"] not in told]
            sources = _sources(entry for entry, _ in chosen)
            chosen = [(entry, why) for entry, why in chosen if entry["id"] not in sources][:room]
        if not chosen:
            return ""
        given += [entry["id"] for entry, _ in chosen]
        state["covered"] = sorted({*covered, *_sources(entry for entry, _ in chosen)})
        os.makedirs(folder, exist_ok=True)
        _write_atomic(state_path, json.dumps(state))
        _log(home, agent, chat, event, chosen)
        return _message(chosen)
    except Exception:
        return ""


# Candidates beyond the room left, in case the chat's briefing already gave some of them.
TOLD_SLACK = 6


def _write_atomic(path: str, text: str) -> None:
    """Write through a temporary file, so a hook reading at the same moment never meets half a file."""

    import time

    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(text)
    try:
        # On Windows the replace fails while another hook of the same chat has the file open (review b, F-L3):
        # try again for up to about 50 ms, then write in place rather than lose the state.
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                time.sleep(0.005)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def _sources(entries: Iterable[dict[str, Any]]) -> set[str]:
    """The lessons the rules among ``entries`` were made from (``build_index``), which say what those rules say."""

    return {source for entry in entries if isinstance(entry.get("sources"), list)
            for source in entry["sources"] if isinstance(source, str)}


def _message(chosen: Sequence[tuple[dict[str, Any], str]]) -> str:
    lines = []
    for entry, why in chosen:
        if why == "error":
            lines.append(f"KnowItAll2: that output contains \"{entry['phrase']}\", which it knows about:")
        else:
            lines.append("KnowItAll2 tool rule for a command like that one:")
        lines.append(f"- [{entry['id']}] {entry['text']}")
    return "\n".join(lines)


def _chat(payload: dict[str, Any]) -> str:
    """A file name for the chat: its session id, or its transcript's name, in safe characters."""

    seed = str(payload.get("session_id") or os.path.basename(str(payload.get("transcript_path") or "")) or "unknown")
    return re.sub(r"[^\w-]", "_", seed)[:80]


def _told(home: str, payload: dict[str, Any]) -> list[str]:
    """The memories the chat's briefing gave (``chats``), so a rule it showed is not given again."""

    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return []
    try:   # built in: hashlib would load OpenSSL, 11 ms of each command (review 2026-10-14, E-L1)
        from _sha1 import sha1
    except ImportError:
        from hashlib import sha1
    # ``chats.chat_key`` for a session id (``learning.moments.key``), without importing either.
    name = "s" + sha1(os.path.normcase(session).encode("utf-8")).hexdigest()[:16]
    try:
        with open(os.path.join(home, "chats", f"{name}.json"), encoding="utf-8") as stream:
            told = json.load(stream).get("told")
    except (OSError, ValueError, AttributeError):
        return []
    return [item for item in told if isinstance(item, str)] if isinstance(told, list) else []


def _log(home: str, agent: str, chat: str, event: str, chosen: Sequence[tuple[dict[str, Any], str]]) -> None:
    import time

    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "agent": agent, "chat": chat, "event": event,
             "ids": [item["id"] for item, _ in chosen], "why": [why for _, why in chosen]}
    try:
        with open(os.path.join(home, HINT_LOG), "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def home() -> str:
    """``paths.data_home`` without ``pathlib``, which loads urllib and ipaddress (review 2026-10-14, E-L1)."""

    configured = os.environ.get("KNOWITALL2_HOME")
    return os.path.expanduser(configured) if configured else os.path.join(os.path.expanduser("~"), ".knowitall2")


KEPT_DAYS = 30


def prune(home: str, *, now: float | None = None, days: int = KEPT_DAYS) -> None:
    """Forget what chats older than ``days`` were given: their state files and their lines in the log. Never
    raises (review 2026-10-14, E-L8: both grew with every chat)."""

    import time

    moment = time.time() if now is None else now
    cutoff = moment - days * 86400
    folder = os.path.join(home, HINT_STATE)
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass
    log = os.path.join(home, HINT_LOG)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cutoff))
    try:
        with open(log, encoding="utf-8") as stream:
            lines = stream.readlines()
        kept = []
        for line in lines:
            try:
                if str(json.loads(line).get("at") or "") >= stamp:
                    kept.append(line)
            except (ValueError, AttributeError):
                continue
        if len(kept) != len(lines):
            _write_atomic(log, "".join(kept))
    except OSError:
        pass


def run_hook(agent: str) -> None:
    """The hook launcher's ``command`` event: read the agent's payload, print the context, never raise."""

    import sys

    try:
        output = main(agent, home(), sys.stdin.buffer.read())
        if output:
            sys.stdout.write(output)
            sys.stdout.flush()
    except Exception:
        pass


def main(agent: str, home: str, stdin_bytes: bytes) -> str:
    """What the hook prints: the JSON that adds the context, or nothing."""

    try:
        # utf-8-sig: a shell between the agent and the hook may start what it passes on with a byte order mark.
        payload = json.loads(stdin_bytes.decode("utf-8-sig", "replace") or "{}")
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    context = hint(payload, str(home), agent=agent)
    if not context:
        return ""
    event = str(payload.get("hook_event_name"))
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}, ensure_ascii=True)
