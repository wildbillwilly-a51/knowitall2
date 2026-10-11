"""Lessons at the moment of a failure: what KnowItAll2 knows about a command, shown when that command fails.

A lesson about how a command behaves here carries a tag ``command:<key>``: the program, and the subcommand
when the lesson is about one ("git archive", "rmm.py command", "sleep"), or "python heredoc"; with
``command:<key> @ <word>`` it applies only to a command containing that word (a host, a target, a path). The
learner writes the tags (``learning.command_tags``). The knowledge-file refresh writes ``commands.json`` in the
data home: each key and its newest lessons, a project's lesson with that project's folders. The agent's hook
(Claude Code: after a Bash or PowerShell command fails) reads only that file: for each key once in a chat, the
two newest lessons that apply in that folder and to that command.

Why: in five days of real use, a quarter of what agents needed was held and not delivered, mostly quirks met
while running a command, often the same quirk in several chats (2026-10-10 measurement). Replayed on those
chats: lessons matched by shared words, or by the commands they quote, fired on most commands and reached few
misses; tags chosen per lesson by the model, with the project and "where" conditions, reached 11 of 117 held
misses before a command (26 of 455 tool calls spent on them) at 4.5 reminders a chat, and 7 (20 calls) after
a failure at 1.0 a chat. After a failure is what this does: nearly the same help, a fifth of the reminders,
at the moment the agent is looking for an answer, and no cost to commands that succeed.

The hook imports only json, os, and re (and no SQLite, which only the refresh that writes the index needs).
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Iterable, Sequence

COMMAND_TAG = "command:"
INDEX = "commands.json"
HINT_LOG = "command_hints.jsonl"
HINT_STATE = "command_hints"         # a folder in the data home: per chat, the keys it was told about
LESSONS_PER_KEY = 2
TEXT_LIMIT = 500
HINTS_PER_CHAT = 8
FAILED_EVENT = "PostToolUseFailure"
HEREDOC = "python heredoc"
# Programs too general to be a key alone: a lesson must name the subcommand it is about.
GENERAL = frozenset({
    "git", "ssh", "python", "python3", "py", "docker", "bash", "sh", "zsh", "pwsh", "powershell", "cmd", "sudo",
    "cd", "ls", "cat", "grep", "sed", "awk", "echo", "find", "node", "npm", "npx", "go", "dotnet", "make", "pip",
    "java", "cargo", "env", "kubectl", "gh", "glab", "read", "diff", "head", "tail", "sort", "tee", "test", "rm",
    "cp", "mv", "mkdir", "touch", "chmod", "which", "where", "type", "printf", "remove-item", "copy-item",
    "move-item", "new-item", "get-childitem", "get-content", "set-content", "set-location", "write-output",
    "write-host", "select-object", "where-object", "foreach-object", "out-file", "out-null", "out-string",
})
# Lessons kept per command in the index; the hook shows the newest that apply here.
INDEXED_PER_KEY = 8
_SEGMENT = re.compile(r"&&|\|\||[;|\n(){}]|\$\(|`")
# Words that only run what follows them.
_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "exec", "then", "do", "else", "elif", "if", "while", "until",
                       "for", "!", "&", ".", "call", "start", "command", "builtin", "xargs"})
_KEY = re.compile(r"[a-z0-9][\w.+-]*(?: [a-z][\w.-]*)?")
# Options before a subcommand that take the next word as their value.
_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "-H", "-i", "-o", "-p", "-u", "-F", "-l", "--git-dir", "--work-tree",
                                 "--host", "--context", "--namespace", "--hostname", "-R", "--repo"})
_WORD = re.compile(r"[a-z][\w.-]*", re.IGNORECASE)


def keys(command: str) -> set[str]:
    """Each program a shell command runs, with its subcommand: 'git archive' and 'git' for 'git archive HEAD'."""

    found: set[str] = set()
    for segment in _SEGMENT.split(_HEREDOC_BODY.sub("", command or "")):
        words = [word.strip("\"'") for word in segment.split()]
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
        program = re.split(r"[\\/]", words[0])[-1].casefold()
        program = re.sub(r"\.(?:exe|cmd|bat)$", "", program)
        rest = words[1:]
        if program in ("python", "python3", "py") and rest and rest[0].casefold().endswith(".py"):
            program, rest = re.split(r"[\\/]", rest[0])[-1].casefold(), rest[1:]
        elif program in ("python", "python3", "py") and len(rest) > 1 and rest[0] == "-m":
            rest = rest[1:]   # python -m unittest: "python unittest"
        if not _KEY.fullmatch(program):
            continue
        found.add(program)
        while rest and rest[0].startswith("-"):   # git -C /repo status, ssh -o BatchMode=yes host
            rest = rest[2:] if rest[0] in _OPTIONS_WITH_VALUE else rest[1:]
        if rest and _WORD.fullmatch(rest[0]):
            found.add(f"{program} {rest[0].casefold()}")
    # A heredoc fed to Python here, not one inside a command run elsewhere (ssh host 'python3 - <<EOF').
    if re.search(r"^(?:(?!\b(?:ssh|wsl(?:\.exe)?|docker\s+exec)\b)[^\n])*\bpython[\d.]*(?:\.exe)?\b[^\n]*<<",
                 command or "", re.IGNORECASE | re.MULTILINE):
        found.add(HEREDOC)
    return found


# What a shell prints when a heredoc's quoting or escapes broke the script it carried.
_HEREDOC_FAILURE = re.compile(r"unexpected EOF|unterminated|SyntaxError|unicodeescape|invalid escape|"
                              r"matching [`'\"]", re.IGNORECASE)


def evidence(key: str, lessons: Sequence[dict[str, Any]], error: str) -> tuple[int, int]:
    """How plainly a failure's error points to ``key``, and how early: (strength, position).

    Strength 3: a heredoc's typical error. 2: the program named where the error begins, or failing its own
    way, and a lesson sharing a word with the error (a git lesson about another git problem is not it).
    1: the program named anywhere. 0: nothing. Position: where the error first names it.
    """

    text = error or ""
    if key == HEREDOC:
        found = _HEREDOC_FAILURE.search(text)
        return (3, found.start()) if found else (0, len(text))
    program = key.split()[0]
    named = re.compile(r"(?<![\w.-])" + re.escape(program) + r"(?![\w-])", re.IGNORECASE)
    head = "\n".join(line for line in text.splitlines()[:3] if not re.match(r"\s*Exit code \d+\s*$", line))[:300]
    signature = _SIGNATURES.get(program)
    spot = named.search(head) or (signature.search(text) if signature is not None else None)
    if spot and _shares_a_word(lessons, text, program):
        return 2, spot.start()
    anywhere = named.search(text)
    return (1, anywhere.start()) if anywhere else (0, len(text))


def ordered(lessons: Sequence[dict[str, Any]], error: str) -> list[dict[str, Any]]:
    """Lessons sharing the most words with the error first; otherwise newest first, as the index keeps them."""

    said = {word for word in _WORDS.findall((error or "").casefold()) if word not in _COMMON_WORDS}
    return sorted(lessons, key=lambda lesson: -len(said & set(_WORDS.findall(str(lesson.get("text", "")).casefold()))))


def _shares_a_word(lessons: Sequence[dict[str, Any]], text: str, program: str) -> bool:
    said = {word for word in _WORDS.findall(text.casefold()) if word not in _COMMON_WORDS} - {program}
    return any(word in said for lesson in lessons for word in _WORDS.findall(str(lesson.get("text", "")).casefold()))


_WORDS = re.compile(r"[a-z]{5,}")
_COMMON_WORDS = frozenset({"error", "errors", "failed", "fails", "failure", "while", "command", "could", "which",
                           "there", "their", "about", "after", "before", "other", "should", "would", "files", "using",
                           "value", "check", "first", "where", "these", "those", "lines", "string", "warning",
                           "found", "users", "local", "projects", "appdata", "program", "programs", "python",
                           "internal", "claude", "codex", "module", "return", "false", "print", "temp"})


# How some common programs fail, when the error does not name them.
_SIGNATURES = {
    "git": re.compile(r"^(?:fatal|error): ", re.MULTILINE),
    "go": re.compile(r"\.go:\d+:\d+"),
    "ssh": re.compile(r"Permission denied \(publickey|Host key verification failed|kex_exchange|Connection (?:refused|"
                      r"timed out|closed by remote host)"),
    "scp": re.compile(r"Connection closed|lost connection"),
    "docker": re.compile(r"Error response from daemon"),
    "npm": re.compile(r"^npm (?:ERR|error)", re.MULTILINE),
    "dotnet": re.compile(r"error (?:CS|MSB|NU)\d+"),
}


def choose(keys_found: dict[str, list[dict[str, Any]]], shown: Sequence[str], error: str) -> str | None:
    """The key to tell the agent about: the one the error points to most plainly, then the one it names
    first, then the most specific; None unless the error plainly points to one (strength 2 or more)."""

    scored = {key: evidence(key, lessons, error) for key, lessons in keys_found.items() if lessons and key not in shown}
    ranked = sorted(scored, key=lambda key: (-scored[key][0], scored[key][1], -len(key), key))
    if not ranked or scored[ranked[0]][0] < 2:
        return None
    return ranked[0]


# The body of a heredoc ("<<'EOF'" to the line "EOF") is input, not commands.
_HEREDOC_BODY = re.compile(r"(?<=<<)-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?(?:\n[ \t]*\2[ \t]*(?=\n|$)|$)", re.DOTALL)


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


# --- The index the hook reads ------------------------------------------------------------------------------------

def build_index(connection: Any) -> dict[str, list[dict[str, Any]]]:
    """Each key and its newest active lessons (redacted), with where each applies: the word its command must
    contain, and for a project's lesson, that project's folders."""

    from .secrets import redact

    folders: dict[str, list[str]] = {}
    for project_id, path in connection.execute("SELECT project_id, path FROM project_paths"):
        folders.setdefault(project_id, []).append(str(path))
    index: dict[str, list[dict[str, Any]]] = {}
    rows = connection.execute(
        "SELECT id, text, tags, scope, project_id FROM records WHERE status = 'active' AND tags LIKE ? "
        "ORDER BY created_at DESC, seq DESC", (f"%{COMMAND_TAG}%",),
    )
    for record_id, text, raw, scope, project_id in rows:
        try:
            tags = json.loads(raw or "[]")
        except ValueError:
            continue
        for key, where in tagged_keys(tags if isinstance(tags, list) else []):
            lessons = index.setdefault(key, [])
            if len(lessons) < INDEXED_PER_KEY:
                lesson: dict[str, Any] = {"id": record_id, "text": redact(" ".join(str(text).split()))[:TEXT_LIMIT]}
                if where:
                    lesson["where"] = where
                if scope == "project":
                    lesson["folders"] = folders.get(project_id, [])
                lessons.append(lesson)
    return index


def applies(lesson: dict[str, Any], command: str, cwd: str) -> bool:
    """Whether a lesson applies to this command in this folder."""

    where = lesson.get("where")
    if where and where not in command.casefold():
        return False
    folders = lesson.get("folders")
    if folders is not None:
        if not cwd:
            return False
        # A project's folders are kept as git found them; the agent's folder may be a short (8.3) name or a link.
        here = {os.path.normcase(os.path.abspath(cwd)), os.path.normcase(os.path.realpath(cwd))}
        return any(place == folder or place.startswith(folder.rstrip("\\/") + os.sep)
                   for place in here for folder in folders)
    return True


def write_index(connection: Any, home: Path) -> None:
    from .files import write_text_atomic

    write_text_atomic(home / INDEX, json.dumps(build_index(connection), ensure_ascii=True, sort_keys=True) + "\n")


# --- The hook ----------------------------------------------------------------------------------------------------

def hint(payload: dict[str, Any], home: str, *, agent: str = "claude-code") -> str:
    """The context to give the agent after this failed command, or ''. Never raises."""

    try:
        event = str(payload.get("hook_event_name") or "")
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str) or event != FAILED_EVENT:
            return ""
        try:
            with open(os.path.join(home, INDEX), encoding="utf-8") as stream:
                index = json.load(stream)
        except (OSError, ValueError):
            return ""
        cwd = str(payload.get("cwd") or "")
        error = str(payload.get("error") or "")
        found = {key: [lesson for lesson in index.get(key) or [] if applies(lesson, command, cwd)]
                 for key in keys(command)}
        if not any(found.values()):
            return ""
        chat = _chat(payload)
        folder = os.path.join(home, HINT_STATE)
        state_path = os.path.join(folder, f"{chat}.json")
        try:
            with open(state_path, encoding="utf-8") as stream:
                state = json.load(stream)
        except (OSError, ValueError):
            state = {}
        shown = state.setdefault("shown", [])
        if len(shown) >= HINTS_PER_CHAT:
            return ""
        # The part of the command the error points to, and only when it plainly does (``choose``).
        fresh = choose(found, shown, error)
        if fresh is None:
            return ""
        lessons = ordered(found[fresh], error)[:LESSONS_PER_KEY]
        shown.append(fresh)
        os.makedirs(folder, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
        _log(home, agent, chat, fresh, event, [item["id"] for item in lessons])
        return _message(fresh, lessons)
    except Exception:
        return ""


def _message(key: str, lessons: Sequence[dict[str, str]]) -> str:
    lead = f"KnowItAll2: that `{key}` command failed. What KnowItAll2 knows about it here:"
    return "\n".join([lead, *(f"- [{item['id']}] {item['text']}" for item in lessons)])


def _chat(payload: dict[str, Any]) -> str:
    """A file name for the chat: its session id, or its transcript's name, in safe characters."""

    seed = str(payload.get("session_id") or os.path.basename(str(payload.get("transcript_path") or "")) or "unknown")
    return re.sub(r"[^\w-]", "_", seed)[:80]


def _log(home: str, agent: str, chat: str, key: str, event: str, ids: Sequence[str]) -> None:
    import time

    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "agent": agent, "chat": chat, "key": key,
             "event": event, "ids": list(ids)}
    try:
        with open(os.path.join(home, HINT_LOG), "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def run_hook(agent: str) -> None:
    """The hook launcher's ``command`` event: read the agent's payload, print the context, never raise."""

    import sys

    from .paths import data_home

    try:
        output = main(agent, str(data_home()), sys.stdin.buffer.read())
        if output:
            sys.stdout.write(output)
            sys.stdout.flush()
    except Exception:
        pass


def main(agent: str, home: str, stdin_bytes: bytes) -> str:
    """What the hook prints: the JSON that adds the context, or nothing."""

    try:
        payload = json.loads(stdin_bytes.decode("utf-8", "replace") or "{}")
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    context = hint(payload, str(home), agent=agent)
    if not context:
        return ""
    event = str(payload.get("hook_event_name"))
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}, ensure_ascii=True)
