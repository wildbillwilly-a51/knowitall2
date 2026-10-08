"""Look up a system's missing parts in its project folder, right away, when the user asks.

The user presses "Ask an agent to find these out" on a system's profile. One
detached process starts one agent call in the project folder the system is
worked on in: it may read and search files there and do nothing else (Claude
Code gets only its file-reading tools; Codex runs in its read-only sandbox),
on the model learning uses. Each answer names the file it came from
and quotes it, and is saved only if the quote really is in that file and
says what the answer says, so a guess cannot become a memory. A file may be
out of date or wrong, so what is saved is unverified. What it cannot find is
left to the agents that work with the system later, as before. The call
counts toward the daily total but never waits for the limit: the user asked
for it, and pressing the button is the consent, even with learning off. The
agent reads the files itself, so nothing it reads is redacted first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import catalog, journal, review
from ..files import read_text, write_text_atomic
from ..memory import Memory, MemoryInputError
from ..paths import data_home
from ..quotes import quoted_in
from ..secrets import contains_secret, redact
from ..store import Store
from .extractor import ExtractionError
from .learner import _says_what_was_quoted

FINDER_AGENT = "finder"
STALE_MINUTES = 20
KNOWN_SHOWN = 30
MAX_FILE_BYTES = 5 * 1024 * 1024
MIN_TEXT, MAX_TEXT = 20, 600

FIND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "found": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "part": {"type": "string"},
                    "text": {"type": "string"},
                    "file": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["part", "text", "file", "quote"],
                "additionalProperties": False,
            },
        },
        "not_found": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["found", "not_found"],
    "additionalProperties": False,
}

FIND_PROMPT = """You look up missing facts about one system for KnowItAll2, the user's long-term memory for coding work. You are in a project folder that works with this system. Search its files briefly for each missing part listed: configuration, scripts, inventories, documentation, and notes. Read and search only: never change a file, run a program, or connect to anything.

For each part you find, give:
- part: the part's key as listed, such as p1;
- text: one self-contained sentence that names the system and states the fact, for example "vCenter runs at vcsa01.lab.local on port 443.";
- file: the path of the file that shows it, relative to the folder;
- quote: an exact passage copied from that file (one line or a few) that shows everything the text says.

Only report what a file states; never guess. Never report a password, token, key, or other secret: say where it is kept instead (a vault item, a credential store entry, or an environment variable name), and quote the line that names that place, not the secret. Put the keys of the parts you could not find in not_found. Be quick: a few searches per part."""


# What the page shows ---------------------------------------------------------


def status_path(system_id: str) -> Path:
    return data_home() / "learner" / "finding" / f"{system_id}.json"


def status(system_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The latest search for this system, or None; a search that stopped without a word counts as failed."""

    try:
        state = json.loads(read_text(status_path(system_id)))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    moment = now or datetime.now(timezone.utc)
    if state.get("status") == "looking" and _age(state.get("started_at"), moment) > timedelta(minutes=STALE_MINUTES):
        state.update({"status": "failed", "message": "The search stopped without finishing. Try again."})
    return state


def start(system_id: str, *, launcher: Callable[..., object] = subprocess.Popen) -> dict[str, Any]:
    """Start the search in its own process; returns what to tell the user."""

    current = status(system_id)
    if current and current.get("status") == "looking":
        return {"started": False, "message": "An agent is already looking."}
    _write(system_id, {"status": "looking", "started_at": _now()})
    from ..hooks import _detached_options, _learner_environment

    log_folder = data_home() / "learner"
    log_folder.mkdir(parents=True, exist_ok=True)
    log = open(log_folder / "last-find.log", "w", encoding="utf-8")
    try:
        launcher(
            [sys.executable, "-B", "-P", "-m", "knowitall2", "find-out", system_id],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(data_home()), env=_learner_environment(), close_fds=True, **_detached_options(),
        )
    except OSError as exc:
        _write(system_id, {"status": "failed", "finished_at": _now(), "message": f"The search could not start: {exc}"})
        raise
    finally:
        log.close()
    return {"started": True, "message": "An agent is looking now. This page shows what it finds."}


# The search ------------------------------------------------------------------


def wanted(shown: dict[str, Any]) -> list[dict[str, str]]:
    """What is missing from a profile: its empty parts, then the gaps its summary names."""

    items = [{"facet": item["facet"], "label": item["label"], "gap": False} for item in shown.get("missing") or []]
    for gap in shown.get("gaps") or []:
        label = " ".join(str(gap).split())
        if label:
            items.append({"facet": "howto" if label.casefold().startswith("how to") else "other", "label": label,
                          "gap": True})
    # The model answers with these short keys: they cannot be mistaken for a part's name.
    return [{**item, "key": f"p{index}"} for index, item in enumerate(items, 1)]


def folder_for(store: Store, system: dict[str, Any], shown: dict[str, Any]) -> Path | None:
    """The project folder on this computer that the system is most worked on in, or None."""

    counts: dict[str, int] = {}
    if shown.get("main_project"):
        counts[shown["main_project"]] = 1000
    for record in store.system_records(system["id"])[:60]:
        if record.get("project_id"):
            counts[record["project_id"]] = counts.get(record["project_id"], 0) + 1
        for event in store.events(kinds=["candidate", "remember"], record_id=record["id"], limit=3):
            if event["project_id"]:
                counts[event["project_id"]] = counts.get(event["project_id"], 0) + 1
    for project_id in sorted(counts, key=counts.get, reverse=True):
        for path in store.project_paths(project_id):
            if Path(path).is_dir():
                return Path(path)
    return None


def render(system: dict[str, Any], shown: dict[str, Any], items: list[dict[str, str]]) -> str:
    lines = [f"System: {system['name']} ({system['kind']})"]
    if system.get("aliases"):
        lines.append("Also called: " + ", ".join(system["aliases"]))
    if system.get("summary"):
        lines.append(f"Summary: {system['summary']}")
    known = [memory.get("headline") or memory.get("text") for facet in shown.get("facets") or []
             for memory in facet["memories"]][:KNOWN_SHOWN]
    known += [memory.get("headline") or memory.get("text") for memory in shown.get("elsewhere") or []][:KNOWN_SHOWN]
    if known:
        lines += ["", "Already known:"] + [f"- {item}" for item in known]
    lines += ["", "Find these missing parts (key: what):"]
    lines += [f"- {item['key']}: {item['label']}" for item in items]
    return "\n".join(lines)


def check(item: dict[str, Any], *, folder: Path, parts: dict[str, dict[str, str]],
          names: Sequence[str] = ()) -> tuple[dict[str, Any] | None, str | None]:
    """Deterministic checks of one answer: a real part, no secret, and a quote that is in the named file.

    The quote must also say what the answer says: most of the answer's
    significant words, apart from the system's ``names``, are in it.
    """

    key = str(item.get("part") or "").strip().casefold()
    text = " ".join(str(item.get("text") or "").split())
    quote = str(item.get("quote") or "")
    # First, so that an answer holding a secret is never kept or shown, whatever else is wrong with it.
    if contains_secret(" ".join([text, quote])):
        return None, "secret"
    if key not in parts:
        return None, "not one of the missing parts"
    if not MIN_TEXT <= len(text) <= MAX_TEXT:
        return None, "length"
    base = folder.resolve()
    path = Path(str(item.get("file") or ""))
    path = (path if path.is_absolute() else base / path).resolve()
    if path != base and base not in path.parents:
        return None, "the file is outside the project folder"
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None, "the file was not found"
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, "the file was not found"
    if not quoted_in(quote, [content]):
        return None, "the quote is not in that file"
    if not _says_what_was_quoted(text, " ".join([quote, *names])):
        # The answer is free text: without this, a harmless quote could carry anything the model wrote.
        return None, "the quote does not say that"
    return {"key": key, "facet": parts[key]["facet"], "label": parts[key]["label"], "text": text,
            "file": path.relative_to(base).as_posix()}, None


def run(system_id: str, *, engine: Any = None, store: Store | None = None) -> dict[str, Any]:
    """The search itself (in the detached process): look, check, save, and leave the rest to later agents."""

    from ..paths import database_path

    owned = store is None
    store = store or Store.open(database_path())
    try:
        result = _run(store, system_id, engine)
    except Exception as exc:
        result = {"status": "failed", "message": f"The search failed: {exc}"}
        journal.problem("find out", f"the search for {system_id} failed: {type(exc).__name__}: {exc}")
    finally:
        if owned:
            store.close()
    _write(system_id, {**result, "started_at": (status(system_id) or {}).get("started_at") or _now(),
                       "finished_at": _now()})
    return result


def _run(store: Store, system_id: str, engine: Any) -> dict[str, Any]:
    system = store.system(system_id)
    if system is None:
        return {"status": "failed", "message": "That system is no longer known."}
    shown = catalog.profile(store, system)
    items = wanted(shown)
    if not items:
        return {"status": "done", "found": [], "not_found": [], "message": "Nothing is missing."}
    memory = Memory(store, agent=FINDER_AGENT)
    folder = folder_for(store, system, shown)
    if folder is None:
        asked = _leave_for_agents(memory, system, items, shown)
        return {"status": "done", "found": [], "not_found": [item["label"] for item in items], "asked": asked,
                "message": f"No project folder for {system['name']} is known on this computer, so agents working "
                           "with it will be asked instead."}
    if engine is None:
        from .command import build_engine
        from .state import load_settings

        settings = load_settings()
        engine = build_engine(settings.backend, settings.model)
        if engine is None:
            return {"status": "failed", "folder": folder.name,
                    "message": "No Claude Code or Codex engine was found on this computer."}
    from .state import record_other_call

    try:
        payload = engine.explore(render(system, shown, items), folder=folder, schema=FIND_SCHEMA,
                                 system_prompt=FIND_PROMPT)
    except ExtractionError as exc:
        record_other_call("find out")
        return {"status": "failed", "folder": folder.name, "message": f"The agent could not look: {exc}"}
    record_other_call("find out")
    parts = {item["key"]: item for item in items}
    found, turned_down, filled = [], [], set()
    for answer in payload.get("found") or []:
        if not isinstance(answer, dict):
            continue
        checked, reason = check(answer, folder=folder, parts=parts, names=[system["name"], *system["aliases"]])
        if checked is None:
            part = parts.get(str(answer.get("part") or "").strip().casefold())
            # Shown on the page and kept in the status file: redacted, in case it was turned down for another reason.
            turned_down.append({"label": part["label"] if part else "a missing part", "reason": reason,
                                "text": "" if reason == "secret" else redact(str(answer.get("text") or ""))[:MAX_TEXT]})
            continue
        saved = _save(memory, system, checked)
        if saved is not None:
            filled.add(checked["key"])
            found.append({"label": checked["label"], "text": checked["text"],
                          "file": checked["file"], "id": saved})
    left = [item for item in items if item["key"] not in filled]
    filled_gaps = {item["label"] for item in items if item["key"] in filled and item["gap"]}
    if filled_gaps:
        # A filled gap leaves the profile now, not only when its summary is next rewritten. The gaps are read
        # again first: the catalog may have described the system while the agent looked.
        with store.transaction():
            current = store.system(system_id)
            if current is not None:
                store.set_system_gaps(system_id, gaps=[gap for gap in current["gaps"]
                                                       if " ".join(str(gap).split()) not in filled_gaps],
                                      now=memory.now())
    asked = _leave_for_agents(memory, system, left, shown)
    journal.record(
        store, "task", f"An agent looked in {folder.name} for {len(items)} missing parts of {system['name']}: "
        f"found {len(found)}", outcome="find out", agent=FINDER_AGENT, project_id=shown.get("main_project"),
        record_ids=[entry["id"] for entry in found],
        details={"system": system_id, "folder": folder.name, "found": len(found), "turned_down": len(turned_down),
                 "usage": getattr(engine, "last_usage", None) or {}},
    )
    return {"status": "done", "folder": folder.name, "found": found, "turned_down": turned_down,
            "not_found": [item["label"] for item in left], "asked": asked}


def _save(memory: Memory, system: dict[str, Any], checked: dict[str, Any]) -> str | None:
    try:
        # A file says so, which may be out of date or wrong: unverified, as a lead for agents to check.
        result = memory.remember(
            checked["text"], kind=catalog.FACET_KINDS.get(checked["facet"], "fact"), subjects=[system["name"]],
            scope="global", source="inferred", detect_project=False,
        )
    except MemoryInputError:
        return None
    now = memory.now()
    with memory.store.transaction():
        # A memory already saved keeps the note it has, such as one the user wrote, even while the agent looked.
        memory.store.add_note(result.record.id, headline=_short(checked["text"]), system_id=system["id"],
                              facet=checked["facet"], written_by=FINDER_AGENT, now=now)
        for task in memory.store.tasks(status="open", kind="find_out", system_id=system["id"]):
            if _asks_for(task, checked["label"]):
                memory.store.update_task(task["id"], status="done", result=result.record.id, now=now)
    return result.record.id


def _leave_for_agents(memory: Memory, system: dict[str, Any], items: list[dict[str, str]],
                      shown: dict[str, Any]) -> int:
    """Ask agents that work with the system later about what is still missing, once per part."""

    open_tasks = memory.store.tasks(status="open", kind="find_out", system_id=system["id"])
    asked = 0
    for item in items:
        if any(_asks_for(task, item["label"]) for task in open_tasks):
            continue
        review.ask_to_find_out(memory, system, item["facet"], item["label"], project_id=shown.get("main_project"))
        asked += 1
    return asked


def _asks_for(task: dict[str, Any], label: str) -> bool:
    """Whether a request to agents asks for this part (matched by what it asks, not how it was filed)."""

    return f"does not know {label.lower()} for " in str(task.get("prompt") or "").lower()


def _write(system_id: str, state: dict[str, Any]) -> None:
    write_text_atomic(status_path(system_id), json.dumps({**state, "pid": os.getpid()}) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(value: object, now: datetime) -> timedelta:
    try:
        return now - datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return timedelta(days=1)


def _short(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."
