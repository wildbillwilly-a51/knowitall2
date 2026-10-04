"""What the app shows and does: the JSON behind each screen, and the user's actions.

Each request opens the live database, reads what it needs, and closes it.
The app's own reading never counts as an agent using a memory. Changes go
through the same operations as the command line and the MCP tools, recorded
as the user's own, by agent "app".
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .. import __version__, catalog, journal, review
from ..agents import AGENT_NAMES, adapter_for, server_launch
from ..learning import finder, moments
from ..learning.extractor import find_claude_cli, find_codex_cli
from ..learning.state import LearnerState, RunLock, load_settings
from ..memory import KINDS, Memory, MemoryInputError, fts_query
from ..paths import data_home, database_path
from ..store import Store

APP_AGENT = "app"
KEPT_OUTCOMES = ("saved", "updated", "saved with a question")
CHART_DAYS = 14
RECENT_EVENTS = 8
# What each Activity filter shows. "all" leaves out single candidates, which a
# learning run summarizes; the Learning filter has them.
ACTIVITY_GROUPS: dict[str, tuple[str, ...]] = {
    "all": ("briefing", "recall", "remember", "forget", "restore", "confirm", "question", "answer", "settle", "task",
            "settings", "learning", "change", "maintenance", "catalog", "move"),
    "use": ("briefing", "recall"),
    "learning": ("learning", "candidate", "change", "maintenance", "catalog"),
    "changes": ("remember", "forget", "restore", "confirm", "question", "answer", "settle", "task", "settings",
                "change", "move"),
}
MEMORY_PAGE = 50
# The largest whole number the database takes; a page number or event number past it is refused.
SQLITE_INTEGER_MAX = 2 ** 63 - 1
MEMORY_FILTERS = {
    "status": ("active", "inactive"),
    "kind": KINDS,
    "verification": ("user_stated", "observed", "unverified"),
    "origin": ("user", "learner", "agent", "import"),
    "sort": ("recent", "used", "oldest", "changed", "unused"),
}
_ENGINE_NAMES = {"claude-cli": "Claude Code", "codex-cli": "Codex"}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class NotFound(LookupError):
    """The thing asked for does not exist (any more)."""


class _Cached:
    """A value that is slow to find (engine discovery runs programs), refreshed every few minutes."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._values: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any, compute) -> Any:
        with self._lock:
            found = self._values.get(key)
            if found is not None and time.monotonic() - found[0] < self.seconds:
                return found[1]
        value = compute()
        with self._lock:
            self._values[key] = (time.monotonic(), value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


_engines = _Cached(300)
_agents = _Cached(30)


@contextmanager
def open_store() -> Iterator[Store]:
    store = Store.open(database_path())
    try:
        yield store
    finally:
        store.close()


def ping(query, body, match) -> dict[str, Any]:
    return {"ok": True, "version": __version__}


def overview(query, body, match) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    shift = _shift_minutes(query)
    today = _local_midnight(now, shift)
    week = today - timedelta(days=6)
    settings = load_settings()
    engine = engine_path(settings.backend)
    with open_store() as store:
        stats = store.stats()
        usage = store.usage_summary(since=_iso(week))
        periods = {
            "today": _period(store, since=today),
            "week": _period(store, since=week),
        }
        last_runs = store.events(kinds=["learning"], limit=1)
        recent = store.events(kinds=ACTIVITY_GROUPS["all"], limit=RECENT_EVENTS)
        questions = len(review.for_user(store))
        looking = store.count_open_questions() - questions
        daily = _daily(store, now=now, shift=shift)
        filed = store.count_unnoted()
        systems = len([count for key, count in store.system_memory_counts().items() if key and count])
    state = LearnerState()
    problems_week = journal.read_problems(since=_iso(now - timedelta(days=7)))
    problems_day = [item for item in problems_week if item["at"] >= _iso(now - timedelta(days=1))]
    agents = agent_status()
    learning = {
        "enabled": settings.enabled, "backend": settings.backend,
        "engine": _ENGINE_NAMES.get(settings.backend, settings.backend), "model": settings.model or "",
        "engine_found": engine is not None, "running": RunLock().busy(),
        "last_run": last_runs[0] if last_runs else None, "last_start": _last_start(),
        "calls_today": state.calls_since(now - timedelta(days=1)), "calls_per_day": settings.max_calls_per_day,
        "now": _now_view(), "latest": next((_news_view(item) for item in moments.news()[:1]), None),
    }
    sharing = _sharing()
    health = _health(learning, agents, problems_day, problems_week, questions)
    _add_sharing_health(health, sharing)
    return {
        "version": __version__, "now": _iso(now),
        "health": health,
        "sharing": sharing,
        "learning": learning,
        "periods": periods,
        "memories": {
            "active": stats["active"], "superseded": stats["superseded"], "retired": stats["retired"],
            "by_kind": stats["by_kind"], "by_verification": stats["by_verification"],
            "projects": stats["projects"], "never_used": usage["never_used"], "top": usage["top"],
            "systems": systems, "unfiled": filed,
        },
        "questions": questions,
        "questions_in_progress": looking,
        "problems": problems_week[:5],
        "agents": agents,
        "recent": recent,
        "daily": daily,
    }


def activity(query, body, match) -> dict[str, Any]:
    group = _one(query, "group") or "all"
    if group not in ACTIVITY_GROUPS:
        raise MemoryInputError(f"Unknown activity filter {group!r}.")
    limit = min(max(_integer(query, "limit") or 60, 1), 200)
    with open_store() as store:
        events = store.events(
            kinds=() if _one(query, "run") or _one(query, "memory") else ACTIVITY_GROUPS[group],
            run_id=_one(query, "run"), record_id=_one(query, "memory"), before=_integer(query, "before"),
            limit=limit + 1,
        )
    more = len(events) > limit
    return {"events": events[:limit], "next": events[limit - 1]["seq"] if more else None}


def problems(query, body, match) -> dict[str, Any]:
    return {"problems": journal.read_problems(limit=200)}


def doctor(query, body, match) -> dict[str, Any]:
    """The full installation check, including a real MCP handshake; the user asks for it."""

    from ..doctor import run_checks

    checks = [asdict(check) for check in run_checks()]
    _agents.clear()
    return {"checks": checks, "ok": all(check["ok"] for check in checks)}


def memories(query, body, match) -> dict[str, Any]:
    """One page of memories, searched and filtered; reading here never counts as use."""

    chosen = {name: _one(query, name) for name in MEMORY_FILTERS}
    for name, value in chosen.items():
        if value is not None and value not in MEMORY_FILTERS[name]:
            raise MemoryInputError(f"Unknown {name} {value!r}.")
    text = " ".join((_one(query, "q") or "").split())[:200]
    project = _one(query, "project")
    sort = chosen["sort"] or "recent"
    offset = max(_integer(query, "offset") or 0, 0)
    filters = {
        "status": chosen["status"] or "active",
        "project_id": project if project not in (None, "global") else None,
        "global_only": project == "global",
        "kind": chosen["kind"], "verification": chosen["verification"], "origin": chosen["origin"],
        "unused": sort == "unused", "system": _one(query, "system"),
    }
    with open_store() as store:
        if text:
            items, total = store.browse(**filters, ids=_matching_ids(store, text, filters["status"]), order="ids",
                                        limit=MEMORY_PAGE, offset=offset)
        else:
            order = "recent" if sort == "unused" else sort
            items, total = store.browse(**filters, order=order, limit=MEMORY_PAGE, offset=offset)
        _add_notes(store, items)
    return {"items": items, "total": total, "offset": offset, "limit": MEMORY_PAGE}


def _add_notes(store: Store, items: list[dict[str, Any]]) -> None:
    """Each memory's plain summary and system, when the catalog has filed it."""

    notes = store.notes_for([item["id"] for item in items])
    for item in items:
        found = notes.get(item["id"]) or {}
        item["headline"] = found.get("headline")
        item["system_id"] = found.get("system_id")
        item["system_name"] = found.get("system_name")
        item["facet"] = found.get("facet")
        item["facet_label"] = catalog.FACETS.get(found.get("facet") or "", None)


def projects(query, body, match) -> dict[str, Any]:
    with open_store() as store:
        return {
            "projects": store.project_list(),
            "global": store.count_active(project_id=None, scope="global"),
        }


def memory_detail(query, body, match) -> dict[str, Any]:
    record_id = match.group("id")
    with open_store() as store:
        details = store.record_details(record_id)
        if details is None:
            raise NotFound(f"There is no memory [{record_id}].")
        replacement = store.get(details["superseded_by"]) if details["superseded_by"] else None
        questions = [item for item in store.open_questions(limit=500) if record_id in item["record_ids"]]
        events = store.events(kinds=_CHANGE_KINDS, record_id=record_id, limit=40)
        _add_notes(store, [details])
    return {
        "memory": details,
        "replaced_by": replacement.__dict__ if replacement else None,
        "questions": questions,
        "events": events,
    }


def memory_action(query, body, match) -> dict[str, Any]:
    """The user's own change to one memory, made exactly as the command line would."""

    record_id, action = match.group("id"), match.group("action")
    with open_store() as store:
        memory = Memory(store, agent=APP_AGENT)
        if store.get(record_id) is None:
            raise NotFound(f"There is no memory [{record_id}].")
        if action == "correct":
            result = memory.correct(record_id, _text(body, "text"))
            return {"message": f"Saved your correction as [{result.record.id}]; it replaces [{record_id}].",
                    "id": result.record.id}
        if action == "forget":
            message = memory.forget(record_id, reason=_text(body, "reason", required=False) or "forgotten in the app",
                                    by_user=True)
        elif action == "restore":
            message = memory.restore(record_id)
        else:
            message = memory.confirm(record_id)
    return {"message": message, "id": record_id}


_CHANGE_KINDS = tuple(kind for kind in journal.EVENT_KINDS if kind not in ("briefing", "recall"))


def _matching_ids(store: Store, text: str, status: str) -> list[str]:
    """Memories matching a search, best first; retired ones match by their words."""

    if status != "active":
        needle = text.casefold()
        return [row.id for row in store.list_inactive(limit=5000)
                if needle in row.text.casefold() or any(needle in subject.casefold() for subject in row.subjects)]
    match = fts_query(text)
    ids = [row.id for row, _ in store.search(match, project_id=None, scope="everywhere", limit=500)] if match else []
    if not ids and len(text) >= 2:
        ids = [row.id for row in store.substring_search(text, project_id=None, scope="everywhere", limit=500)]
    return ids


def _text(body: dict[str, Any], name: str, *, required: bool = True) -> str:
    value = body.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise MemoryInputError(f"'{name}' is required.")
    return value


def learning(query, body, match) -> dict[str, Any]:
    """Learning's settings, state, the last week's results, and its recent runs."""

    now = datetime.now(timezone.utc)
    week = _iso(now - timedelta(days=7))
    settings = load_settings()
    with open_store() as store:
        runs = store.events(kinds=["learning"], limit=40)
        summary = store.candidate_summary(since=week)
        week_runs = store.events(kinds=["learning"], since=week, limit=1000)
    totals = {"runs": len(week_runs), "sessions": 0, "calls": 0, "review_calls": 0,
              "tokens": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}}
    for event in week_runs:
        details = event["details"]
        totals["sessions"] += len(details.get("sessions") or [])
        totals["calls"] += int(details.get("calls") or 0)
        upkeep = details.get("maintenance") or {}
        totals["review_calls"] += int(upkeep.get("calls") or 0)
        for usage in (details.get("usage") or {}, upkeep.get("usage") or {}):
            for name in totals["tokens"]:
                value = usage.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals["tokens"][name] += value
    return {
        "settings": asdict(settings),
        "engines": {name: {"name": _ENGINE_NAMES[name], "found": engine_path(name) is not None,
                           "path": str(engine_path(name) or "")} for name in _ENGINE_NAMES},
        "running": RunLock().busy(),
        "last_start": _last_start(),
        "calls_today": LearnerState().calls_since(now - timedelta(days=1)),
        "now": _now_view(),
        "news": [_news_view(item) for item in moments.news()[:NEWS_SHOWN]],
        "waiting": _waiting(settings),
        "week": {**totals, "candidates": summary["outcomes"], "reasons": summary["reasons"]},
        "runs": runs,
    }


def learning_settings(query, body, match) -> dict[str, Any]:
    """The user's learning choices; turning it on is the user's consent."""

    from ..learning.command import BACKENDS, configure_learning
    from ..learning.state import save_settings

    settings = load_settings()
    before = asdict(settings)
    for name, low, high in (("max_calls_per_run", 1, 50), ("max_calls_per_day", 1, 500), ("idle_minutes", 5, 240)):
        if name in body:
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise MemoryInputError(f"{name.replace('_', ' ')} must be a whole number from {low} to {high}.")
            setattr(settings, name, value)
    backend = body.get("backend")
    if backend is not None and backend not in BACKENDS:
        raise MemoryInputError(f"Unknown engine {backend!r}.")
    model = body.get("model")
    if model is not None and (not isinstance(model, str) or len(model) > 80
                              or not all(character.isalnum() or character in "._:-" for character in model)):
        raise MemoryInputError("The model name may use letters, digits, and . _ : - only.")
    enable = body.get("enabled")
    if enable is not None and not isinstance(enable, bool):
        raise MemoryInputError("'enabled' must be true or false.")
    if enable or (enable is None and settings.enabled and (backend or model)):
        configure_learning(settings, enable=True, backend=backend, model=model or None, agent=APP_AGENT)
    elif enable is False and settings.enabled:
        configure_learning(settings, enable=False, agent=APP_AGENT)
    else:
        save_settings(settings)
    changed = [name.replace("_", " ") for name in ("max_calls_per_run", "max_calls_per_day", "idle_minutes")
               if before[name] != getattr(settings, name)]
    if changed:
        journal.record_standalone(
            "settings", "Learning limits changed: " + ", ".join(
                f"{name} {getattr(settings, name.replace(' ', '_'))}" for name in changed),
            outcome="limits", agent=APP_AGENT,
        )
    _engines.clear()
    _waiting_cache.clear()
    return {"settings": asdict(settings), "message": "Learning is on." if settings.enabled else "Learning is off."}


def learning_run(query, body, match) -> dict[str, Any]:
    """Start a background learning run now, instead of waiting for the next session."""

    from ..hooks import maybe_start_learner

    if not load_settings().enabled:
        raise MemoryInputError("Learning is off. Turn it on first.")
    if RunLock().busy():
        return {"started": False, "message": "A learning run is already in progress."}
    if not maybe_start_learner(force=True):
        raise MemoryInputError("The learning run could not be started.")
    _waiting_cache.clear()
    return {"started": True, "message": "Learning started. It only reads sessions that have been idle for a while."}


NEWS_SHOWN = 12


def _now_view() -> dict[str, Any] | None:
    """What the learner is doing right now, in plain words, or None."""

    state = moments.read_now()
    if state is None:
        return None
    reason = str(state.get("reason") or "")
    return {"since": state.get("since"), "doing": state.get("doing") or "learning",
            "why": moments.why(reason, str(state.get("detail") or "")) if reason and reason != "catch-up" else "",
            "folder": Path(str(state["folder"])).name if state.get("folder") else ""}


def _news_view(item: dict[str, Any]) -> dict[str, Any]:
    """One news entry for the app: what happened, in plain words, without the session files."""

    folders = [Path(folder).name for folder in item.get("folders") or [] if folder]
    return {
        "id": item.get("id"), "at": item.get("at"), "reason": item.get("reason"), "status": item.get("status"),
        "why": moments.why(str(item.get("reason") or ""), str(item.get("detail") or "")),
        "folders": list(dict.fromkeys(folders)), "saved": item.get("saved") or [], "updated": item.get("updated") or [],
        "already_known": item.get("already_known") or 0, "questions": item.get("questions") or 0,
        "turned_down": item.get("turned_down") or [], "partial": bool(item.get("partial")),
        "problem": item.get("problem") or "", "limit": item.get("limit"), "run": item.get("run") or "",
        "sessions": len(item.get("sessions_learned") or []),
        "can_learn_anyway": bool(item.get("requests")) and (item.get("status") == "limit" or bool(item.get("partial"))),
    }


def learning_anyway(query, body, match) -> dict[str, Any]:
    """Learn a session that waited for the daily limit now, without the limit (the user's choice)."""

    from ..hooks import maybe_start_learner

    if not load_settings().enabled:
        raise MemoryInputError("Learning is off. Turn it on first.")
    item = moments.find_news(match.group("id"))
    if item is None or not item.get("requests"):
        raise NotFound("That learning result is no longer available.")
    for request in item["requests"]:
        moments.request(transcript=request.get("transcript"), session_id=request.get("session_id"),
                        agent=request.get("agent") or "", cwd=request.get("cwd"),
                        reason=request.get("reason") or "asked", detail=request.get("detail") or "",
                        ignore_limit=True)
    started = maybe_start_learner(requests=True)
    return {"started": True, "message": "Learning it now." if started
            else "A learning run is in progress; it learns this before it stops."}


_waiting_cache = _Cached(60)


def _waiting(settings) -> dict[str, Any]:
    """How many session logs are ready to learn from, and how many are still active (a dry run, cached)."""

    def compute() -> dict[str, Any]:
        from ..learning.learner import learn
        from ..learning.transcripts import session_logs

        try:
            report = learn(logs=session_logs(), state=LearnerState(), settings=settings, memory_factory=None,
                           extractor=None, dry_run=True)
        except Exception as exc:
            return {"error": str(exc)}
        return {"logs": report.logs, "active": report.active, "ready": len(report.ready),
                "characters": sum(item.characters for item in report.ready)}

    return _waiting_cache.get("waiting", compute)


def questions(query, body, match) -> dict[str, Any]:
    """Questions waiting for the user (plain words, details on request), those being looked into, and the latest."""

    with open_store() as store:
        for_you = review.for_user(store)
        looking = review.in_progress(store)
        for item in [*for_you, *looking]:
            records = [store.get(record_id) for record_id in item["record_ids"]]
            item["memories"] = [record.__dict__ for record in records if record is not None]
            _add_notes(store, item["memories"])
        answered = store.answered_questions(limit=15)
    labels = {option["key"]: option["label"] for options in review.PLAIN_LABELS.values()
              for option in ({"key": key, "label": label} for key, label in options.items())}
    for item in answered:
        choice, _, rest = (item["answer"] or "").partition(" ")
        item["answer_label"] = labels.get(choice, item["answer"])
        item["settled_by"] = rest.strip("() ").removeprefix("settled by ") if rest.startswith("(settled by") else "you"
    return {"for_you": for_you, "in_progress": looking, "answered": answered}


def answer_question(query, body, match) -> dict[str, Any]:
    """The user's own choice, applied with the user's full authority; "not sure" changes nothing."""

    choice = _text(body, "choice").strip()
    with open_store() as store:
        question = store.question(match.group("id"))
        if question is None:
            raise NotFound(f"There is no question [{match.group('id')}].")
        if choice == "not_sure":
            choice = review.NOT_SURE.get(question["kind"], "")
        message = review.answer(Memory(store, agent=APP_AGENT), match.group("id"), choice)
        remaining = len(review.for_user(store))
    return {"message": message, "remaining": remaining}


def knowledge(query, body, match) -> dict[str, Any]:
    """Everything KnowItAll2 knows, by area and system."""

    with open_store() as store:
        found = catalog.overview(store)
        found["requests"] = len(store.tasks(status="open", kind="find_out"))
    return found


def knowledge_system(query, body, match) -> dict[str, Any]:
    with open_store() as store:
        system = store.system(match.group("id"))
        if system is None:
            raise NotFound("That system is not known.")
        shown = catalog.profile(store, system)
        shown["requests"] = store.tasks(status=None, kind="find_out", system_id=system["id"])[-10:]
        shown["facet_labels"] = catalog.FACETS
        project = store.project_list()
        names = {item["id"]: item["name"] for item in project}
        shown["main_project_name"] = names.get(shown["main_project"])
        folder = finder.folder_for(store, system, shown)
    settings = load_settings()
    shown["finding"] = finder.status(system["id"])
    shown["search"] = {"folder": folder.name if folder else "",
                       "engine": _ENGINE_NAMES.get(settings.backend, settings.backend)}
    return shown


def knowledge_find(query, body, match) -> dict[str, Any]:
    """Start one agent looking up the system's missing parts in its project folder, now (the user's choice)."""

    with open_store() as store:
        if store.system(match.group("id")) is None:
            raise NotFound("That system is not known.")
    return finder.start(match.group("id"))


def knowledge_tell(query, body, match) -> dict[str, Any]:
    """The user fills in part of a system's profile, in their own words."""

    facet = _text(body, "facet").strip()
    if facet not in catalog.FACETS:
        raise MemoryInputError("Choose which part of the profile this fills in.")
    with open_store() as store:
        system = store.system(match.group("id"))
        if system is None:
            raise NotFound("That system is not known.")
        result = catalog.tell(Memory(store, agent=APP_AGENT), system, facet, _text(body, "text"))
    return {"message": f"Saved as your own words: {catalog.FACETS[facet].lower()} for {system['name']}.",
            "id": result.record.id}


def knowledge_ask(query, body, match) -> dict[str, Any]:
    """Ask agents to find out a missing part of a system's profile."""

    facet = _text(body, "facet").strip()
    label = catalog.FACETS.get(facet) or _text(body, "label", required=False) or ""
    if facet not in catalog.FACETS and not label:
        raise MemoryInputError("Say what agents should find out.")
    with open_store() as store:
        system = store.system(match.group("id"))
        if system is None:
            raise NotFound("That system is not known.")
        shown = catalog.profile(store, system)
        detail = " ".join(_text(body, "detail", required=False).split())[:200]
        task_id = review.ask_to_find_out(Memory(store, agent=APP_AGENT), system, facet if facet in catalog.FACETS
                                         else "other", label, detail=detail, project_id=shown["main_project"])
    return {"message": f"Agents working with {system['name']} will be asked to find this out.", "id": task_id}


_catch_up_cache = _Cached(300)


def catch_up_list(query, body, match) -> dict[str, Any]:
    """Past sessions skipped as already covered, by folder; learning from them costs model calls."""

    from ..learning import catchup

    since = _date(query, "since")
    return {"groups": _catch_up_cache.get(("groups", since), lambda: catchup.groups(since=since)), "since": since}


def catch_up_estimate(query, body, match) -> dict[str, Any]:
    from ..learning import catchup

    return catchup.estimate(_text(body, "folder"), since=_body_date(body))


def catch_up_start(query, body, match) -> dict[str, Any]:
    """Mark a folder's skipped sessions to be learned, within the daily budget: the user's choice."""

    from ..learning import catchup

    folder, since = _text(body, "folder"), _body_date(body)
    marked = catchup.catch_up(folder, since=since)
    _catch_up_cache.clear()
    _waiting_cache.clear()
    journal.record_standalone("settings", f"Catching up on {marked} past sessions in {folder}", outcome="catch up",
                              agent=APP_AGENT, details={"folder": folder, "since": since, "sessions": marked})
    return {"marked": marked, "message": f"Learning will read {marked} past session(s) from {folder}, "
                                         "within its daily limit."}


def _date(query: dict[str, list[str]], name: str) -> str | None:
    value = _one(query, name)
    if value is not None and not _DATE.fullmatch(value):
        raise MemoryInputError(f"{name} must be a date like 2026-08-13.")
    return value


def _body_date(body: dict[str, Any]) -> str | None:
    value = body.get("since")
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise MemoryInputError("since must be a date like 2026-08-13.")
    return value


def engine_path(backend: str):
    return _engines.get(backend, lambda: find_codex_cli() if backend == "codex-cli" else find_claude_cli())


def agent_status() -> list[dict[str, Any]]:
    return _agents.get("all", _compute_agents)


def _compute_agents() -> list[dict[str, Any]]:
    launch = server_launch()
    result = []
    for name in AGENT_NAMES:
        adapter = adapter_for(name)
        if not adapter.installed():
            result.append({"name": adapter.display_name, "key": name, "installed": False, "ok": False, "checks": []})
            continue
        try:
            checks = [asdict(check) for check in adapter.checks(launch)]
        except Exception as exc:
            checks = [{"name": f"{adapter.display_name} settings", "ok": False, "detail": str(exc), "fix": None}]
        result.append({"name": adapter.display_name, "key": name, "installed": True,
                       "ok": all(check["ok"] for check in checks), "checks": checks})
    return result


def _period(store: Store, *, since: datetime) -> dict[str, Any]:
    moment = _iso(since)
    counts = store.event_counts(since=moment)
    usage = store.usage_counts(since=moment)
    recalls = usage.get("recall", {"count": 0, "with_results": 0})
    tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
    calls = 0
    for event in store.events(kinds=["learning"], since=moment, limit=1000):
        details = event["details"]
        calls += int(details.get("calls") or 0) + int((details.get("maintenance") or {}).get("calls") or 0)
        for usage_part in (details.get("usage") or {}, (details.get("maintenance") or {}).get("usage") or {}):
            for name in tokens:
                value = usage_part.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    tokens[name] += value
    return {
        "briefings": usage.get("briefing", {"count": 0})["count"],
        "recalls": recalls["count"], "recall_hits": recalls["with_results"],
        "saved": sum(count for (kind, outcome), count in counts.items()
                     if kind == "remember" and outcome in ("saved", "updated")),
        "learned": sum(count for (kind, outcome), count in counts.items()
                       if kind == "candidate" and outcome in KEPT_OUTCOMES),
        "rejected": counts.get(("candidate", "rejected"), 0),
        "changes": sum(count for (kind, _), count in counts.items() if kind == "change"),
        "runs": sum(count for (kind, _), count in counts.items() if kind == "learning"),
        "calls": calls,
        "tokens": tokens,
    }


def _daily(store: Store, *, now: datetime, shift: int) -> list[dict[str, Any]]:
    """The last two weeks, day by day: memories added and how often agents looked them up."""

    first = _local_midnight(now, shift) - timedelta(days=CHART_DAYS - 1)
    days: dict[str, dict[str, Any]] = {}
    for offset in range(CHART_DAYS):
        day = (first + timedelta(days=offset) + timedelta(minutes=shift)).date().isoformat()
        days[day] = {"day": day, "learner": 0, "agent": 0, "user": 0, "import": 0,
                     "briefings": 0, "recalls": 0, "recall_hits": 0}
    for day, origin, count in store.daily_additions(since=_iso(first), shift_minutes=shift):
        if day in days:
            days[day][origin] += count
    for day, operation, count, hits in store.daily_usage(since=_iso(first), shift_minutes=shift):
        if day in days:
            if operation == "briefing":
                days[day]["briefings"] += count
            else:
                days[day]["recalls"] += count
                days[day]["recall_hits"] += hits
    return list(days.values())


def _health(learning: dict, agents: list, problems_day: list, problems_week: list, questions: int) -> dict[str, Any]:
    """Whether KnowItAll2 is working, in plain words, with what to do about anything that is not."""

    items: list[dict[str, Any]] = []

    def add(level: str, text: str, action: dict | None = None) -> None:
        items.append({"level": level, "text": text, "action": action})

    if learning["enabled"] and not learning["engine_found"]:
        add("problem", f"Learning is on, but {learning['engine']} was not found on this computer, so nothing new "
                       "can be learned.", {"label": "Learning settings", "route": "#/learning"})
    last = learning["last_run"]
    if learning["enabled"] and last and last["outcome"] == "stopped":
        add("problem", f"The last learning run stopped: {last['summary'].removeprefix('Learning stopped: ')}",
            {"label": "See what happened", "route": "#/activity?group=learning"})
    for agent in agents:
        for check in agent["checks"]:
            if agent["installed"] and not check["ok"]:
                add("problem", f"{agent['name']} is not fully connected to KnowItAll2 ({check['name']}: "
                               f"{check['detail']}).",
                    {"label": "How to fix", "hint": check.get("fix")} if check.get("fix") else None)
    if problems_day:
        noun = "problem was" if len(problems_day) == 1 else "problems were"
        add("problem", f"{len(problems_day)} {noun} recorded in the last 24 hours.",
            {"label": "Show problems", "route": "#/activity?group=problems"})
    elif problems_week:
        add("attention", f"{len(problems_week)} problem(s) were recorded in the last 7 days.",
            {"label": "Show problems", "route": "#/activity?group=problems"})
    if not learning["enabled"]:
        add("attention", "Learning is off, so KnowItAll2 only knows what agents save on purpose.",
            {"label": "Learning settings", "route": "#/learning"})
    if questions:
        noun = "question is" if questions == 1 else "questions are"
        add("attention", f"{questions} {noun} waiting for you.", {"label": "Answer", "route": "#/questions"})
    if not any(agent["installed"] for agent in agents):
        add("attention", "No coding agent is connected to KnowItAll2 yet.", None)
    level = "problem" if any(item["level"] == "problem" for item in items) else (
        "attention" if items else "ok")
    headline = {"ok": "Everything is working", "attention": "Working, and a few things need you",
                "problem": "Something needs fixing"}[level]
    return {"level": level, "headline": headline, "items": items}


def _sharing() -> dict[str, Any] | None:
    """This computer's connection to a KnowItAll2 server, from its last sync (the page never waits on the server)."""

    from .. import connected

    connection = connected.load()
    if connection is None:
        return None
    status = connected.read_status()
    return {"address": connection.address, "agents": sorted(connection.keys),
            "last_success": status.get("last_success"), "problem": status.get("problem"),
            "problem_status": status.get("problem_status"), "unsent": bool(status.get("unsent"))}


def _add_sharing_health(health: dict[str, Any], sharing: dict[str, Any] | None) -> None:
    if not sharing:
        return
    if sharing["problem_status"] == 401 or not sharing["agents"]:
        item = {"level": "problem", "action": {"label": "How to fix", "hint": (
            "Make a new join code on the server's page (Add an agent), then run: knowitall2 server connect "
            "<agent> --join-code <code>")},
            "text": "The KnowItAll2 server no longer accepts this computer, so memories are not shared."}
    elif sharing["problem"]:
        item = {"level": "attention", "action": None,
                "text": f"The KnowItAll2 server at {sharing['address']} could not be reached at the last try. "
                        "Memories saved here wait and are sent when it is back."}
    else:
        return
    health["items"].append(item)
    order = {"ok": 0, "attention": 1, "problem": 2}
    level = max([health["level"], item["level"]], key=order.__getitem__)
    health["level"] = level
    health["headline"] = {"ok": "Everything is working", "attention": "Working, and a few things need you",
                          "problem": "Something needs fixing"}[level]


def _last_start() -> str | None:
    try:
        stamp = (data_home() / "learner" / "last-start").stat().st_mtime
    except OSError:
        return None
    return _iso(datetime.fromtimestamp(stamp, tz=timezone.utc))


def _shift_minutes(query: dict[str, list[str]]) -> int:
    """Minutes from UTC to the viewer's local time (the page sends getTimezoneOffset)."""

    offset = _integer(query, "tz") or 0
    return -max(-14 * 60, min(14 * 60, offset))


def _local_midnight(now: datetime, shift: int) -> datetime:
    local = now + timedelta(minutes=shift)
    return datetime(local.year, local.month, local.day, tzinfo=timezone.utc) - timedelta(minutes=shift)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values and values[0] else None


def _integer(query: dict[str, list[str]], name: str) -> int | None:
    value = _one(query, name)
    if value is None:
        return None
    try:
        number = int(value)
    except ValueError:
        raise MemoryInputError(f"{name} must be a whole number.") from None
    if abs(number) > SQLITE_INTEGER_MAX:
        raise MemoryInputError(f"{name} is too large.")
    return number


ROUTES: list[tuple[str, str, Any]] = [
    ("GET", r"/api/ping", ping),
    ("GET", r"/api/overview", overview),
    ("GET", r"/api/activity", activity),
    ("GET", r"/api/problems", problems),
    ("POST", r"/api/doctor", doctor),
    ("GET", r"/api/memories", memories),
    ("GET", r"/api/projects", projects),
    ("GET", r"/api/memories/(?P<id>[A-Za-z0-9_.:-]{1,64})", memory_detail),
    ("POST", r"/api/memories/(?P<id>[A-Za-z0-9_.:-]{1,64})/(?P<action>forget|restore|confirm|correct)", memory_action),
    ("GET", r"/api/learning", learning),
    ("POST", r"/api/learning/settings", learning_settings),
    ("POST", r"/api/learning/run", learning_run),
    ("GET", r"/api/questions", questions),
    ("POST", r"/api/questions/(?P<id>[A-Za-z0-9_-]{1,40})/answer", answer_question),
    ("GET", r"/api/knowledge", knowledge),
    ("GET", r"/api/knowledge/(?P<id>sys-[0-9a-f]{12})", knowledge_system),
    ("POST", r"/api/knowledge/(?P<id>sys-[0-9a-f]{12})/tell", knowledge_tell),
    ("POST", r"/api/knowledge/(?P<id>sys-[0-9a-f]{12})/ask", knowledge_ask),
    ("POST", r"/api/knowledge/(?P<id>sys-[0-9a-f]{12})/find-out", knowledge_find),
    ("GET", r"/api/learning/catch-up", catch_up_list),
    ("POST", r"/api/learning/catch-up/estimate", catch_up_estimate),
    ("POST", r"/api/learning/catch-up", catch_up_start),
    ("POST", r"/api/learning/news/(?P<id>n-[0-9a-f]{8})/anyway", learning_anyway),
]
