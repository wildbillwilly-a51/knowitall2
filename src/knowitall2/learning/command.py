"""The ``knowitall2 learn`` and ``knowitall2 maintain`` commands."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .. import journal
from ..connected import maintenance_turn, renew_maintenance_turn, share_quietly
from ..memory import Memory
from ..paths import database_path
from ..store import Store, StoreError
from . import moments
from .dossier import build_dossiers
from .extractor import (
    ClaudeCliExtractor,
    CodexCliExtractor,
    ExtractionError,
    codex_default_model,
    codex_listed_models,
    find_claude_cli,
    find_codex_cli,
)
from .cataloguer import CATALOG_AGENT, CatalogReport, run_catalog
from .cataloguer import plan as catalog_plan
from .learner import LEARNER_AGENT, LearnReport, learn
from .maintenance import MAINTENANCE_AGENT, EngineReviewer, MaintenanceReport, maintain
from .state import (
    CLAUDE_MODEL, FORMER_CLAUDE_MODELS, LearnerSettings, LearnerState, LockBusy, RunLock, day_ago, learner_lock,
    load_settings, save_settings,
)
from .transcripts import claude_code_logs, codex_logs, is_codex_log, read_session, session_folder, session_logs

BACKENDS = ("claude-cli", "codex-cli")
_ENGINE_NAMES = {"claude-cli": "Claude Code", "codex-cli": "Codex"}


def run_learn(arguments: argparse.Namespace) -> int:
    settings = load_settings()
    if arguments.enable or arguments.disable:
        return _set_learning(settings, arguments)
    if arguments.status:
        print(_status(settings))
        return 0
    if arguments.show:
        return _show(arguments.show)
    if arguments.start_from_now:
        return _start_from_now(arguments.start_from_now)
    if arguments.catch_up is not None:
        return _catch_up(arguments)
    if getattr(arguments, "documents", None) is not None:
        return _documents(arguments, settings)
    state = LearnerState()
    if arguments.dry_run:
        report = learn(
            logs=session_logs(), state=state, settings=settings,
            memory_factory=None, extractor=None, dry_run=True,
        )
        print(report.describe(dry_run=True))
        return 0
    if not settings.enabled:
        print(
            "Learning is off. Review what it would send with `knowitall2 learn --dry-run` and "
            "`knowitall2 learn --show <session>`, then turn it on with `knowitall2 learn --enable`."
        )
        return 0
    extractor = build_engine(settings.backend, arguments.model or settings.model)
    if extractor is None:
        message = f"no {_engine_name(settings.backend)} engine was found, so nothing can be learned yet."
        journal.problem("learning", message)
        _tell_waiting_requests(settings, f"no {_engine_name(settings.backend)} engine was found on this computer")
        print(f"knowitall2: {message}", file=sys.stderr)
        return 1
    if getattr(arguments, "ignore_daily_limit", False):
        # The user chose to learn what is waiting now: one run without the daily limit.
        settings = work_limits(settings)
    try:
        sweep = not getattr(arguments, "requests", False)
        for _ in range(_REQUEST_ROUNDS):
            with RunLock():
                try:
                    _learn_once(settings, extractor, sweep=sweep)
                finally:
                    moments.set_now(None)
            # A request that arrived while the lock was held is learned before stopping.
            if not moments.pending_requests():
                break
            sweep = False
    except LockBusy:
        print("Another learning run is in progress; it learns waiting requests before it stops.")
        return 0
    except (StoreError, sqlite3.Error, ExtractionError) as exc:
        journal.problem("learning", f"learning stopped: {exc}")
        print(f"knowitall2: learning stopped: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        journal.problem("learning", f"learning failed unexpectedly: {type(exc).__name__}: {exc}")
        raise
    return 0


_REQUEST_ROUNDS = 20
# When several requests name one session, the most telling reason is shown.
_REASON_ORDER = ("asked", "commit", "finished", "session end")


def _learn_once(settings: LearnerSettings, extractor, *, sweep: bool) -> None:
    """Learn the requested sessions; then, for ``sweep``, finished sessions, maintenance, and the catalog."""

    state = LearnerState()
    store = Store.open(database_path())
    try:
        # When connected to a server: learn knowing what other computers already learned.
        share_quietly(store)
        requested = learn_requests(store, state, settings, extractor)
        if not sweep or requested.blocked:
            if requested.saved and not requested.blocked:
                # File what was just learned, so "What it knows" shows it.
                moments.set_now({"since": moments.now_iso(), "doing": "filing what it learned"})
                run = new_run_id()
                filing = run_catalog(memory=Memory(store, agent=CATALOG_AGENT), engine=extractor, state=state,
                                     run=run, budget=min(2, _remaining_budget(work_limits(settings), state, used=0)))
                if filing.calls:
                    journal.record(store, "catalog", filing.describe(), outcome="stopped" if filing.blocked else "ok",
                                   agent=CATALOG_AGENT, run=run, details=filing.details())
            journal.prune(store)
            share_quietly(store)
            return
        run = new_run_id()
        started = time.monotonic()
        moments.set_now({"since": moments.now_iso(), "doing": "learning from sessions that ended without being learned",
                         "reason": "catch-up"})
        report = learn(
            logs=session_logs(), state=state, settings=settings,
            memory_factory=lambda: Memory(store, agent=LEARNER_AGENT, record_events=False),
            extractor=extractor, dry_run=False, run=run,
        )
        print(report.describe(dry_run=False))
        if report.calls or report.blocked:
            moments.add_news(news_entry(report, settings, reason="catch-up", run=run, requests=[]))
        upkeep = filing = None
        if not report.blocked:
            share_quietly(store)
            with maintenance_turn() as ours:
                if ours:
                    # Maintenance, then the catalog, follow learning with whatever the budget has left.
                    moments.set_now({"since": moments.now_iso(), "doing": "tidying up memories"})
                    upkeep = maintain(
                        memory=Memory(store, agent=MAINTENANCE_AGENT), reviewer=EngineReviewer(extractor),
                        state=state, budget=_remaining_budget(settings, state, used=report.calls), dry_run=False,
                        run=run,
                    )
                    print(upkeep.describe(dry_run=False))
                    # Maintenance can take long: hold the turn again for the catalogue, or leave it for later.
                    if not upkeep.blocked and renew_maintenance_turn():
                        moments.set_now({"since": moments.now_iso(), "doing": "filing memories under systems"})
                        filing = run_catalog(
                            memory=Memory(store, agent=CATALOG_AGENT), engine=extractor, state=state, run=run,
                            budget=_remaining_budget(settings, state, used=report.calls + upkeep.calls),
                        )
                        print(filing.describe())
                else:
                    print("Another computer sharing this memory is tidying it up, or the server cannot be "
                          "reached; tidying up is left for later.")
        record_learning_run(store, run, report, upkeep, engine=extractor, seconds=time.monotonic() - started,
                            filing=filing)
        journal.prune(store)
        moments.prune_shown()
        share_quietly(store)
    finally:
        store.close()


def _tell_waiting_requests(settings: LearnerSettings, problem: str) -> None:
    """Waiting requests that cannot be learned now become news, so the user is not left guessing."""

    try:
        for group in moments.by_session(moments.pending_requests()):
            first = group[0]
            moments.add_news(news_entry(LearnReport(blocked=problem), settings, reason=first["reason"],
                                        detail=first.get("detail") or "", run="", requests=group))
            moments.done_with(group)
    except OSError:
        pass


# Learning from the user's work in one go: a commit, a session's end, the user asking, or a finished task.
WORK_CALLS_PER_RUN = 30


def work_limits(settings: LearnerSettings) -> LearnerSettings:
    """The limits for learning from the user's own work.

    The daily limit holds back only background learning (the regular pass over
    quiet sessions, catching up, tidying up): what the user's work asks for is
    learned right away. Its calls still count toward the day's total, so
    background learning gets what is left of it.
    """

    return replace(settings, max_calls_per_day=_UNLIMITED,
                   max_calls_per_run=max(settings.max_calls_per_run, WORK_CALLS_PER_RUN))


_UNLIMITED = 1_000_000


class RequestsReport:
    def __init__(self) -> None:
        self.calls = 0
        self.saved = 0
        self.blocked: str | None = None


def learn_requests(store, state: LearnerState, settings: LearnerSettings, extractor) -> RequestsReport:
    """Learn each requested session right away, and write news of each for the user.

    When the engine itself is unusable, every waiting request gets news of
    that and is dropped; its session is learned by a later run instead.
    """

    summary = RequestsReport()
    for group in moments.by_session(moments.pending_requests()):
        chosen = min(group, key=lambda item: _REASON_ORDER.index(item["reason"])
                     if item.get("reason") in _REASON_ORDER else len(_REASON_ORDER))
        detail = chosen.get("detail") or ""
        if summary.blocked:
            report = LearnReport(blocked=summary.blocked)
            moments.add_news(news_entry(report, settings, reason=chosen["reason"], detail=detail, run="",
                                        requests=group))
            moments.done_with(group)
            continue
        limits = work_limits(settings)
        run = new_run_id()
        started = time.monotonic()
        moments.set_now({"since": moments.now_iso(), "doing": "learning from a session", "reason": chosen["reason"],
                         "detail": detail, "folder": chosen.get("cwd") or "", "agent": chosen.get("agent") or ""})
        report = learn(
            logs=[Path(chosen["transcript"])], state=state, settings=limits,
            memory_factory=lambda: Memory(store, agent=LEARNER_AGENT, record_events=False),
            extractor=extractor, dry_run=False, run=run, right_away=True,
        )
        print(report.describe(dry_run=False))
        entry = moments.add_news(news_entry(report, settings, reason=chosen["reason"], detail=detail, run=run,
                                            requests=group))
        record_learning_run(store, run, report, None, engine=extractor, seconds=time.monotonic() - started,
                            trigger={"reason": chosen["reason"], "detail": detail, "news": entry["id"]})
        moments.done_with(group)
        summary.calls += report.calls
        summary.saved += len(entry["saved"]) + len(entry["updated"])
        summary.blocked = report.blocked
    return summary


def news_entry(report: LearnReport, settings: LearnerSettings, *, reason: str, run: str,
               requests: list[dict], detail: str = "") -> dict:
    """What the user is told about one learning run, in the session and in the app."""

    saved, updated, turned_down = [], [], []
    known = questions = 0
    for result in report.results:
        outcome = result["outcome"]
        item = {"id": (result.get("ids") or [None])[0], "text": result.get("text") or ""}
        if outcome in ("saved", "saved with a question"):
            saved.append(item)
            questions += outcome == "saved with a question"
        elif outcome == "updated":
            updated.append(item)
        elif outcome == "already known":
            known += 1
        elif outcome.startswith("rejected"):
            turned_down.append({"text": item["text"], "reason": outcome[len("rejected ("):-1]})
    if report.blocked:
        status = "stopped"
    elif not report.calls and report.deferred:
        status = "limit"
    elif report.failed or report.skipped:
        status = "stopped"
    else:
        status = "done"
    sessions, folders, learned = [], [], []
    for item in requests:
        sessions += moments.session_keys(item.get("transcript"), item.get("session_id"))
        if item.get("cwd"):
            folders.append(item["cwd"])
    for ready in report.ready:
        learned.append(ready.session_id)
        if not requests:
            sessions += moments.session_keys(str(ready.log), None)
            folder = session_folder(ready.log)
            if folder:
                folders.append(folder)
    entry = {
        "reason": reason, "detail": detail, "status": status, "run": run, "calls": report.calls,
        "sessions": list(dict.fromkeys(sessions)), "folders": list(dict.fromkeys(folders)),
        "sessions_learned": learned, "saved": saved, "updated": updated, "already_known": known,
        "questions": questions, "turned_down": turned_down,
        "partial": bool(report.calls and report.deferred), "limit": settings.max_calls_per_day,
        "requests": [{name: item.get(name) for name in ("transcript", "session_id", "agent", "cwd", "reason", "detail")}
                     for item in requests],
    }
    if report.blocked:
        entry["problem"] = report.blocked
    elif report.failed or report.skipped:
        entry["problem"] = "a learning call failed; it will be tried again"
    return entry


def new_run_id() -> str:
    return "r-" + uuid.uuid4().hex[:8]


_KEPT = ("saved", "updated", "saved with a question")


def record_learning_run(store, run: str, report: LearnReport, upkeep: MaintenanceReport | None, *, engine,
                        seconds: float, filing: CatalogReport | None = None, trigger: dict | None = None) -> None:
    """One journal entry per learning run, with its numbers and the maintenance that followed."""

    kept = sum(count for name, count in report.outcomes.items() if name in _KEPT)
    rejected = sum(count for name, count in report.outcomes.items() if name.startswith("rejected"))
    sessions = len(report.ready)
    if report.blocked:
        outcome, summary = "stopped", f"Learning stopped: {report.blocked}"
        journal.problem("learning", f"learning stopped: {report.blocked}")
    elif not sessions and not report.deferred:
        outcome = "nothing new"
        summary = f"Nothing new to learn from ({report.logs} session logs, {report.active} still active)"
    elif not report.calls and report.deferred:
        outcome = "waiting for budget"
        summary = f"{report.deferred} session(s) ready, waiting for the call budget to allow more calls"
    else:
        outcome = "partly failed" if report.failed or report.skipped else "ok"
        noun = "session" if sessions == 1 else "sessions"
        summary = (f"Learned from {sessions} {noun}: {report.calls} model calls, {kept} memories kept, "
                   f"{rejected} candidates rejected")
        if report.deferred:
            summary += f"; {report.deferred} waiting for the call budget"
    details = {
        "logs": report.logs, "active": report.active, "calls": report.calls, "deferred": report.deferred,
        "sessions": [{"session": item.session_id, "dossiers": item.dossiers, "characters": item.characters}
                     for item in report.ready],
        "outcomes": dict(report.outcomes), "failed": report.failed, "skipped": report.skipped,
        "blocked": report.blocked, "engine": engine.name, "model": getattr(engine, "model", None) or "",
        "seconds": round(seconds, 1), "usage": dict(report.usage),
    }
    if trigger is not None:
        details["trigger"] = trigger
    if upkeep is not None:
        details["maintenance"] = maintenance_details(upkeep)
        if upkeep.blocked:
            journal.problem("maintenance", f"maintenance stopped: {upkeep.blocked}")
    if filing is not None:
        details["catalog"] = filing.details()
        if filing.blocked:
            journal.problem("catalog", f"the catalog stopped: {filing.blocked}")
    journal.record(store, "learning", summary, outcome=outcome, agent=LEARNER_AGENT, run=run, details=details)


def run_catalog_command(arguments: argparse.Namespace) -> int:
    """File memories under systems and review questions now, instead of waiting for the next background run."""

    settings = load_settings()
    if arguments.dry_run:
        store = Store.open(database_path())
        try:
            pending = catalog_plan(Memory(store, agent=CATALOG_AGENT))
        finally:
            store.close()
        print(f"Catalog: {pending['unfiled']} memories to file, {pending['systems_to_describe']} systems to describe, "
              f"{pending['questions_to_review']} questions to review. Dry run: no model calls were made.")
        return 0
    if not settings.enabled:
        print("Learning is off, and the catalog uses the same model calls. Preview it with "
              "`knowitall2 catalog --dry-run`, or turn learning on with `knowitall2 learn --enable`.")
        return 0
    engine = build_engine(settings.backend, settings.model)
    if engine is None:
        print(f"knowitall2: no {_engine_name(settings.backend)} engine was found.", file=sys.stderr)
        return 1
    run = new_run_id()
    try:
        with maintenance_turn() as ours:
            if not ours:
                print("Another computer sharing this memory is tidying it up, or the server cannot be reached; "
                      "try again later.")
                return 0
            with learner_lock():
                state = LearnerState()
                store = Store.open(database_path())
                try:
                    share_quietly(store)
                    report = run_catalog(memory=Memory(store, agent=CATALOG_AGENT), engine=engine, state=state,
                                         run=run, budget=_remaining_budget(settings, state, used=0))
                    journal.record(store, "catalog", report.describe(), outcome="stopped" if report.blocked else "ok",
                                   agent=CATALOG_AGENT, run=run, details=report.details())
                    share_quietly(store)
                finally:
                    store.close()
    except LockBusy:
        print("A learning run is in progress; it updates the catalog when it finishes.")
        return 0
    except (StoreError, sqlite3.Error, ExtractionError) as exc:
        journal.problem("catalog", f"the catalog stopped: {exc}")
        print(f"knowitall2: the catalog stopped: {exc}", file=sys.stderr)
        return 1
    print(report.describe())
    return 0


def maintenance_details(report: MaintenanceReport) -> dict:
    return {
        "groups": dict(report.statuses()), "calls": report.calls, "deferred": report.deferred,
        "failed": report.failed, "changes": dict(report.changes), "blocked": report.blocked,
        "usage": dict(report.usage),
    }


def run_maintain(arguments: argparse.Namespace) -> int:
    """Review existing memories now, instead of waiting for the next background run."""

    settings = load_settings()
    if not arguments.dry_run and not settings.enabled:
        print(
            "Learning is off, and maintenance uses the same model calls. Preview it with "
            "`knowitall2 maintain --dry-run`, or turn learning on with `knowitall2 learn --enable`."
        )
        return 0
    engine = None if arguments.dry_run else build_engine(settings.backend, arguments.model or settings.model)
    if not arguments.dry_run and engine is None:
        print(f"knowitall2: no {_engine_name(settings.backend)} engine was found, so no review can run yet.",
              file=sys.stderr)
        return 1
    try:
        if arguments.dry_run:
            return _maintain_once(None, None, settings, dry_run=True)
        with learner_lock():
            return _maintain_once(EngineReviewer(engine), LearnerState(), settings, dry_run=False)
    except LockBusy:
        print("A learning run is in progress; it reviews memories when it finishes.")
        return 0
    except (StoreError, sqlite3.Error, ExtractionError) as exc:
        journal.problem("maintenance", f"maintenance stopped: {exc}")
        print(f"knowitall2: maintenance stopped: {exc}", file=sys.stderr)
        return 1


def build_engine(backend: str, model: str | None) -> ClaudeCliExtractor | CodexCliExtractor | None:
    """The model engine for ``backend`` on this computer, or None when it is not installed."""

    if backend == "codex-cli":
        executable = find_codex_cli()
        return CodexCliExtractor(executable, model=_listed_codex_model(executable, model) or None) if executable else None
    executable = find_claude_cli()
    return ClaudeCliExtractor(executable, model=model or CLAUDE_MODEL) if executable else None


def adopt_current_model() -> str | None:
    """Move learning off a former default model, once, after an update; returns the new model, if moved."""

    settings = load_settings()
    if settings.backend == "claude-cli" and settings.model in FORMER_CLAUDE_MODELS:
        before, settings.model = settings.model, CLAUDE_MODEL
    elif settings.backend == "codex-cli" and settings.model:
        executable = find_codex_cli()
        current = codex_default_model(executable) if executable else None
        if not current or current == settings.model or not _is_codex_light(executable, settings.model):
            return None
        before, settings.model = settings.model, current
    else:
        return None
    save_settings(settings)
    journal.record_standalone("settings", f"Learning model changed from {before} to {settings.model} by the update",
                              outcome="limits", agent="update")
    return settings.model


def _is_codex_light(executable, model: str) -> bool:
    """Whether Codex describes ``model`` as its fast model: the former automatic choice."""

    return any(item["slug"] == model and any(word in str(item.get("description", "")).casefold()
                                              for word in ("affordable", "fast"))
               for item in codex_listed_models(executable) or [])


def _listed_codex_model(executable, model: str | None) -> str | None:
    """``model``, or Codex's current everyday model when Codex no longer lists it (a renamed or retired model).

    A replacement is saved as the learning model, so the choice stays visible in settings.
    """

    if not model:
        return model
    listed = codex_listed_models(executable)
    if listed is None or any(item["slug"] == model for item in listed):
        return model
    replacement = codex_default_model(executable)
    settings = load_settings()
    if settings.backend == "codex-cli" and settings.model == model:
        settings.model = replacement or ""
        save_settings(settings)
        journal.record_standalone(
            "settings", f"Codex no longer offers {model}; learning now uses {replacement or 'Codex default model'}",
            outcome="limits", agent="learner",
        )
    return replacement


def _set_learning(settings: LearnerSettings, arguments: argparse.Namespace) -> int:
    """Record the user's consent (or its withdrawal), the engine, and the model."""

    if arguments.disable:
        configure_learning(settings, enable=False, agent="cli")
        print("Learning is off.")
        return 0
    executable = configure_learning(settings, enable=True, backend=arguments.backend, model=arguments.model,
                                    agent="cli")
    print(f"Learning is on: backend {settings.backend}, model {settings.model or 'the engine default'}.")
    if executable:
        print(f"{_engine_name(settings.backend)} engine: {executable}")
    else:
        print(f"Warning: no {_engine_name(settings.backend)} engine was found on this machine.")
    return 0


def configure_learning(
    settings: LearnerSettings, *, enable: bool, backend: str | None = None, model: str | None = None,
    agent: str = "cli",
):
    """Turn learning on (the user's consent) or off, and save; returns the engine found, if any.

    Without a chosen backend, the current one is kept when it is installed,
    and otherwise one that is installed is used. Without a chosen model, a
    new backend gets its default model.
    """

    if not enable:
        settings.enabled = False
        save_settings(settings)
        journal.record_standalone("settings", "Learning turned off", outcome="learning off", agent=agent)
        return None
    chosen = backend or settings.backend
    if not backend and _engine_path(chosen) is None:
        chosen = next((name for name in BACKENDS if _engine_path(name) is not None), chosen)
    executable = _engine_path(chosen)
    if model:
        settings.model = model
    elif chosen != settings.backend or not settings.model:
        settings.model = _default_model(chosen, executable)
    settings.backend = chosen
    settings.enabled = True
    save_settings(settings)
    journal.record_standalone(
        "settings", f"Learning turned on: engine {chosen}, model {settings.model or 'the engine default'}",
        outcome="learning on", agent=agent,
    )
    return executable


def _engine_path(backend: str):
    return find_codex_cli() if backend == "codex-cli" else find_claude_cli()


def _default_model(backend: str, executable) -> str:
    if backend == "codex-cli":
        return (codex_default_model(executable) if executable else None) or ""
    return CLAUDE_MODEL


def _engine_name(backend: str) -> str:
    return _ENGINE_NAMES.get(backend, backend)


def _maintain_once(reviewer, state, settings: LearnerSettings, *, dry_run: bool) -> int:
    if not dry_run:
        with maintenance_turn() as ours:
            if not ours:
                print("Another computer sharing this memory is tidying it up, or the server cannot be reached; "
                      "try again later.")
                return 0
            return _maintain_now(reviewer, state, settings, dry_run=False)
    return _maintain_now(reviewer, state, settings, dry_run=True)


def _maintain_now(reviewer, state, settings: LearnerSettings, *, dry_run: bool) -> int:
    store = Store.open(database_path())
    try:
        if not dry_run:
            share_quietly(store)
        budget = 0 if state is None else _remaining_budget(settings, state, used=0)
        run = None if dry_run else new_run_id()
        report = maintain(
            memory=Memory(store, agent=MAINTENANCE_AGENT), reviewer=reviewer, state=state, budget=budget,
            dry_run=dry_run, run=run,
        )
        if run is not None:
            changed = sum(report.changes.get(name, 0) for name in ("merged duplicate", "replaced outdated",
                                                                    "retired snapshot"))
            outcome = "stopped" if report.blocked else ("ok" if report.calls else "nothing to review")
            summary = (f"Maintenance stopped: {report.blocked}" if report.blocked
                       else f"Reviewed {report.calls} group(s) of memories: {changed} changed")
            journal.record(store, "maintenance", summary, outcome=outcome, agent=MAINTENANCE_AGENT, run=run,
                           details=maintenance_details(report))
            if report.blocked:
                journal.problem("maintenance", f"maintenance stopped: {report.blocked}")
            share_quietly(store)
    finally:
        store.close()
    print(report.describe(dry_run=dry_run))
    return 0


def _remaining_budget(settings: LearnerSettings, state: LearnerState, *, used: int) -> int:
    """Calls left in this run, counting ``used`` so far, and today (``state`` already counts them)."""

    today = state.calls_since(day_ago(datetime.now(timezone.utc)))
    return max(0, min(settings.max_calls_per_run - used, settings.max_calls_per_day - today))


def _start_from_now(agent: str) -> int:
    """Mark an agent's existing session logs as read, so only new activity is learned.

    For history already captured elsewhere, such as knowledge imported from
    another system. A log that grows later is learned from this point on.
    """

    logs = codex_logs() if agent == "codex" else claude_code_logs()
    moment = datetime.now(timezone.utc).isoformat()
    marked = 0
    try:
        with learner_lock():
            state = LearnerState()  # read under the lock, so a run that just saved is not undone
            for log in logs:
                try:
                    boundary = _line_boundary(log)
                except OSError:
                    continue
                entry = state.entry(log)
                if int(entry.get("offset", 0)) < boundary:
                    entry.update({"offset": boundary, "status": "baseline", "baseline_to": boundary,
                                  "updated_at": moment})
                    marked += 1
            state.save()
    except LockBusy:
        print("A learning run is in progress; try again when it finishes.")
        return 1
    print(f"Marked {marked} of {len(logs)} {agent} session log(s) as already read; only new activity will be learned.")
    return 0


def _catch_up(arguments: argparse.Namespace) -> int:
    from . import catchup

    since = arguments.since
    if since is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        print("knowitall2: --since takes a date like 2026-08-13.", file=sys.stderr)
        return 1
    if not arguments.catch_up:
        found = catchup.groups(since=since)
        if not found:
            print("No past sessions were skipped as already covered.")
            return 0
        print("Past sessions skipped as already covered, by folder (learn one with --catch-up FOLDER):")
        for group in found:
            print(f"  {group['sessions']:4} sessions, last active {group['last_active'][:10]}  {group['folder']}")
        return 0
    if arguments.estimate:
        sized = catchup.estimate(arguments.catch_up, since=since)
        print(f"{sized['sessions']} sessions under {arguments.catch_up}, about {sized['calls']} model calls.")
        return 0
    try:
        marked = catchup.catch_up(arguments.catch_up, since=since, max_calls=getattr(arguments, "max_calls", None))
    except LockBusy:
        print("A learning run is in progress; try again when it finishes.")
        return 1
    journal.record_standalone(
        "settings", f"Catching up on {marked} past sessions in {arguments.catch_up}", outcome="catch up", agent="cli",
        details={"folder": arguments.catch_up, "since": since, "sessions": marked},
    )
    print(f"Marked {marked} past session(s) under {arguments.catch_up} to be learned; learning reads them within "
          "its daily budget.")
    return 0


def _documents(arguments: argparse.Namespace, settings: LearnerSettings) -> int:
    """Learn from projects' own writing; the user starts it, so it waits for no daily limit."""

    import os

    from . import documents

    store = Store.open(database_path())
    try:
        chosen = documents.projects(store)
        if arguments.documents:
            wanted = os.path.normcase(os.path.normpath(arguments.documents))
            chosen = [(project, folder) for project, folder in chosen
                      if os.path.normcase(os.path.normpath(str(folder))) == wanted
                      or project.name.casefold() == arguments.documents.casefold()]
            if not chosen:
                print(f"knowitall2: no known project is called or kept in {arguments.documents}.", file=sys.stderr)
                return 1
        state = LearnerState()
        plans = []
        for project, folder in chosen:
            dossiers = documents.build(project, folder, documents.project_documents(folder),
                                       notes=documents.memory_notes(folder))
            plans.append((project, folder, dossiers, sum(1 for item in dossiers if not state.seen(item.fingerprint))))
        if arguments.estimate:
            for project, folder, dossiers, new in plans:
                print(f"  {new:3} calls  {project.name} ({folder})")
            print(f"About {sum(plan[3] for plan in plans)} model calls in all.")
            return 0
        if not settings.enabled:
            print("Learning is off; turn it on with `knowitall2 learn --enable`.")
            return 0
        extractor = build_engine(settings.backend, arguments.model or settings.model)
        if extractor is None:
            print(f"knowitall2: no {_engine_name(settings.backend)} engine was found.", file=sys.stderr)
            return 1
        limit = arguments.max_calls or WORK_CALLS_PER_RUN
        total = 0
        with learner_lock():
            state = LearnerState()
            for project, folder, dossiers, new in plans:
                if not new:
                    continue
                run = new_run_id()
                started = time.monotonic()
                moments.set_now({"since": moments.now_iso(), "doing": f"reading the documents of project {project.name}",
                                 "reason": "documents"})
                try:
                    report = documents.learn(Memory(store, agent=LEARNER_AGENT, record_events=False), state,
                                             extractor, dossiers, max_calls=limit, run=run)
                finally:
                    moments.set_now(None)
                record_learning_run(store, run, report, None, engine=extractor, seconds=time.monotonic() - started,
                                    trigger={"reason": "documents", "detail": project.name})
                summary = documents.describe(report)
                total += report.calls
                print(f"{project.name}: {summary['calls']} calls, {summary['kept']} memories kept"
                      + (f", {summary['deferred']} parts left for another run" if summary["deferred"] else ""))
                if report.blocked:
                    print(f"Stopped: {report.blocked}")
                    break
            if total:
                # File what was learned, so "What it knows" and briefings show it by system.
                run = new_run_id()
                filing = run_catalog(memory=Memory(store, agent=CATALOG_AGENT), engine=extractor, state=state,
                                     run=run, budget=min(10, max(1, total // 2)))
                if filing.calls:
                    journal.record(store, "catalog", filing.describe(), outcome="stopped" if filing.blocked else "ok",
                                   agent=CATALOG_AGENT, run=run, details=filing.details())
        return 0
    except LockBusy:
        print("A learning run is in progress; try again when it finishes.")
        return 1
    finally:
        store.close()


def _line_boundary(path, *, window: int = 1 << 16) -> int:
    """The offset just past the last complete line, so a later read starts on a record."""

    end = path.stat().st_size
    with path.open("rb") as stream:
        while end > 0:
            start = max(0, end - window)
            stream.seek(start)
            index = stream.read(end - start).rfind(b"\n")
            if index >= 0:
                return start + index + 1
            end = start
    return 0


def _session_key(log) -> str:
    """What names a session: the id at the end of a Codex rollout name, else the file stem."""

    return "-".join(log.stem.split("-")[-5:]) if is_codex_log(log) else log.stem


def _show(prefix: str) -> int:
    matches = [log for log in session_logs() if _session_key(log).startswith(prefix)]
    if len(matches) != 1:
        print(
            f"knowitall2: {len(matches)} session logs match {prefix!r}; give a longer session id.", file=sys.stderr,
        )
        return 1
    session, _ = read_session(matches[0])
    dossiers = build_dossiers(session)
    if not dossiers:
        print(f"Session {session.session_id} has nothing worth learning from.")
        return 0
    for dossier in dossiers:
        print(f"===== dossier {dossier.chunk + 1} of {len(dossiers)}: {len(dossier.text):,} characters =====")
        print(dossier.text)
    return 0


def _status(settings) -> str:
    state = LearnerState()
    counts: dict[str, int] = {}
    for entry in state.logs.values():
        counts[str(entry.get("status"))] = counts.get(str(entry.get("status")), 0) + 1
    progress = ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) or "none yet"
    recent = state.calls_since(day_ago(datetime.now(timezone.utc)))
    return "\n".join([
        f"Learning: {'on' if settings.enabled else 'off'} "
        f"(backend {settings.backend}, model {settings.model or 'the engine default'})",
        f"Background budget: {settings.max_calls_per_run} calls per run, {settings.max_calls_per_day} per day "
        f"(learning from your work has no daily limit); "
        f"{recent} calls in the last 24 hours",
        f"Sessions: {progress}",
    ])
