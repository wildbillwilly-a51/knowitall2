"""Prepare and execute a sealed, bounded repair of alias-replayed learning.

Preparation reads the database and writes review artifacts only. Execution
holds the learner lock, uses the ordinary source-aware final reviewer, and
commits its receipts with the memory changes. Ordinary cursors are never
merged; a saved receipt can acknowledge only an actually completed range.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import journal
from ..connected import SyncLock
from ..files import write_text_atomic
from ..memory import Memory, PROVISIONAL_TAG
from ..paths import database_path
from ..secrets import contains_secret
from ..store import Store, StoreError
from . import provenance
from .dossier import build_dossiers
from .extractor import ExtractionError
from .learner import LEARNER_AGENT, _finish
from .log_identity import path_key, same_log
from .recovery import prefix_hash
from .settle import PART_CHARACTERS, settle
from .state import LearnerState, RunLock, LockBusy, load_settings
from .transcripts import line_boundary, read_session

FORMAT = "history-repair-v1"
RECEIPTS = "learning.history-repair."
STATE_FIELDS = ("offset", "event_index", "settled", "settled_event_index", "pending_reviews",
                "failures", "final_failures", "recovery_ranges", "history_hold", "learn_to", "resume_at")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_seal(store, record_id: str) -> str | None:
    row = store.connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    if row is None:
        return None
    return digest({k: row[k] for k in row.keys() if k not in ("seq", "recall_count", "last_used_at")})


def state_seal(entry) -> str:
    return digest({key: entry.get(key) for key in STATE_FIELDS if key != "history_hold"})


def _canonical(state, session_id):
    names = [name for name, entry in state.logs.items() if entry.get("session_id") == session_id]
    if not names:
        raise ValueError("session is not indexed")
    names.sort(key=lambda name: (not bool(state.logs[name].get("pending_reviews", {}).get("final")),
                                 name.startswith("\\\\?\\"), "~" in name, name))
    path = Path(state.logs[names[0]].get("log_path", names[0]))
    if any(not same_log(path, state.logs[other].get("log_path", other)) for other in names):
        raise ValueError("session has distinct physical histories; select and review those separately")
    entry = state.entry(path)
    if entry.get("identity_conflicts"):
        raise ValueError("aliases have independent pending work; this repair cannot merge it")
    return path, entry, names


def _snapshot(path: Path, target: Path, end: int) -> str:
    expected = prefix_hash(path, end)
    if target.exists():
        if target.stat().st_size != end or prefix_hash(target, end) != expected:
            raise ValueError("snapshot destination already contains different bytes")
        return expected
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + "." + uuid.uuid4().hex[:8] + ".partial")
    with path.open("rb") as source, partial.open("xb") as output:
        left = end
        while left:
            block = source.read(min(left, 1024 * 1024))
            if not block:
                raise ValueError("transcript shortened while snapshotting")
            output.write(block)
            left -= len(block)
        output.flush()
        os.fsync(output.fileno())
    if prefix_hash(partial, end) != expected or prefix_hash(path, end) != expected:
        raise ValueError("transcript prefix changed while snapshotting")
    partial.rename(target)
    return expected


def _prepare(store, state, *, session_id: str, replay_runs: list[str], output: Path,
            max_calls: int = 32, max_bytes: int = 256 * 1024 * 1024) -> dict:
    """Read-only database preparation. Exact source bindings are part of the manifest."""
    if output.exists():
        raise ValueError("history manifest already exists")
    if type(max_calls) is not int or not 2 <= max_calls <= 100:
        raise ValueError("history audit call cap must be between 2 and 100")
    path, entry, aliases = _canonical(state, session_id)
    if entry.get("pending_reviews", {}).get("quick") or entry.get("learn_to") or entry.get("resume_at"):
        raise ValueError("finish independent quick/catch-up work before planning this repair")
    end = line_boundary(path)
    if not 0 < end <= max_bytes:
        raise ValueError("transcript is empty or exceeds the approved snapshot byte cap")
    snapshot = output.parent / (output.stem + ".source") / path.name
    sha = _snapshot(path, snapshot, end)
    session, _ = read_session(snapshot, stop=end)
    if session.session_id != session_id:
        raise ValueError("snapshot session identity differs")
    session.path = path
    parts = build_dossiers(session, budget=PART_CHARACTERS)
    replay_ids = set()
    for run in replay_runs:
        rows = store.connection.execute(
            "SELECT record_ids FROM events WHERE kind='candidate' AND run_id=? AND outcome LIKE 'saved%'", (run,))
        for row in rows:
            ids = json.loads(row[0])
            if ids:
                replay_ids.add(ids[0])
    if not replay_ids:
        raise ValueError("no saved replay records found for the selected runs")
    memory = Memory(store, agent=LEARNER_AGENT, record_events=False)
    candidates = store.tagged_active(PROVISIONAL_TAG, session=session_id, limit=100_000)
    cache, bindings, unresolved = {}, [], []
    for record in candidates:
        source = provenance.locate_legacy(memory, record, path, cache, persist=False)
        if source is None:
            unresolved.append(record.id)
        else:
            bindings.append({"id": record.id, "seal": record_seal(store, record.id), "source": source})
    if unresolved:
        raise ValueError(f"unresolved source bindings remain: {', '.join(unresolved[:10])}")
    bound = {item["id"]: item for item in bindings}
    start = (int(entry.get("settled", 0)), int(entry.get("settled_event_index", 0)))
    records, selected = [], []
    for record_id in sorted(replay_ids):
        record = store.get(record_id)
        if record is None or record.source_session != session_id or record.source_agent != LEARNER_AGENT:
            raise ValueError("replay run references a missing or differently owned record")
        active = record.status == "active" and PROVISIONAL_TAG in record.tags
        records.append({"id": record_id, "seal": record_seal(store, record_id), "active": active})
        if active and tuple(bound[record_id]["source"]["start"]) < start:
            # Include coarse source windows crossing the established cursor;
            # they also require their complete original context.
            selected.append(record_id)
    pending = entry.get("pending_reviews", {}).get("final")
    if not pending or pending["start"] != start[0] or not start[0] < pending["end"] <= end:
        raise ValueError("the selected session has no exact pending final range at its established cursor")
    if entry.get("settled_event_index", 0) or pending.get("end_event_index", 0):
        raise ValueError("history repair currently requires complete-line final boundaries")
    forward, _ = read_session(snapshot, start=start[0], stop=pending["end"])
    forward.path = path
    forward_parts = build_dossiers(forward, budget=PART_CHARACTERS, require_user=start == (0, 0))
    # Include every source that a forward boundary would strand, not only
    # records from the alias replay. They share the same verified session.
    for binding in bindings:
        source = binding["source"]
        if tuple(source["start"]) < (pending["end"], 0) and not provenance.owned(source, forward_parts):
            if binding["id"] not in selected:
                selected.append(binding["id"])
            if not any(row["id"] == binding["id"] for row in records):
                records.append({"id": binding["id"], "seal": binding["seal"], "active": True,
                                "reason": "source crosses approved review boundaries"})
    required = (len(parts) + 1 if selected else 0) + len(forward_parts) + (1 if forward_parts else 0)
    if required > max_calls:
        raise ValueError("snapshot audit plus bounded final review exceeds the approved call cap")
    manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                "session": session_id, "source": {"path": str(path), "snapshot": str(snapshot.resolve()),
                                                   "end": end, "sha256": sha},
                "aliases": aliases, "state_seal": state_seal(entry),
                "state": json.loads(json.dumps({k: entry.get(k) for k in STATE_FIELDS})),
                "replay_runs": replay_runs, "records": records, "bindings": bindings,
                "selected_ids": selected, "forward_end": pending["end"], "max_calls": max_calls,
                "audit_parts": len(parts) if selected else 0, "required_calls": required}
    manifest["id"] = digest(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(output, json.dumps(manifest, indent=2) + "\n")
    return manifest


def prepare(store, state, **options):
    started = not store.connection.in_transaction
    if started:
        store.connection.execute("BEGIN")
    try:
        return _prepare(store, state, **options)
    finally:
        if started:
            store.connection.execute("ROLLBACK")


def validate(manifest):
    unsigned = {k: v for k, v in manifest.items() if k != "id"}
    if manifest.get("format") != FORMAT or manifest.get("id") != digest(unsigned):
        raise ValueError("history manifest identity does not match")
    source = manifest["source"]
    for name in ("path", "snapshot"):
        path = Path(source[name])
        if not path.is_absolute() or prefix_hash(path, source["end"]) != source["sha256"]:
            raise ValueError("approved transcript prefix changed")
        session, _ = read_session(path, stop=min(source["end"], 8192))
        if session.session_id != manifest["session"]:
            raise ValueError("approved transcript session changed")
    if Path(source["snapshot"]).stat().st_size != source["end"]:
        raise ValueError("snapshot has changed size")
    for binding in manifest["bindings"]:
        if (not provenance.valid(binding["source"]) or binding["source"]["session"] != manifest["session"]
                or not same_log(binding["source"]["path"], source["path"])):
            raise ValueError("invalid source binding in history manifest")


class CachedEngine:
    """Resume completed model steps without charging or repeating them."""
    def __init__(self, engine, path, manifest, state):
        self.engine, self.path, self.manifest, self.state = engine, path, manifest, state
        self.name, self.model = engine.name, getattr(engine, "model", "default")
        self.last_usage = {}
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "manifest": manifest["id"], "calls": 0, "responses": {}}
        if self.data.get("manifest") != manifest["id"]:
            raise ValueError("model checkpoint belongs to another history plan")

    def run(self, text, *, schema, system_prompt):
        key = digest({"text": text, "schema": schema, "prompt": system_prompt, "backend": self.name, "model": self.model})
        if key in self.data["responses"]:
            self.last_usage = {}
            return self.data["responses"][key]
        if self.data["calls"] >= self.manifest["max_calls"]:
            raise ExtractionError("history repair reached its approved model-call cap")
        # Reserve before launch, including failed attempts; interruption cannot
        # silently reset the repair's usage limit.
        self.data["calls"] += 1
        self._save()
        try:
            result = self.engine.run(text, schema=schema, system_prompt=system_prompt)
        except ExtractionError as exc:
            if not exc.attempted:
                self.data["calls"] -= 1
                self._save()
            raise
        self.last_usage = getattr(self.engine, "last_usage", None) or {}
        if contains_secret(json.dumps(result)):
            raise ExtractionError("history model response contained a secret; no response was cached")
        self.data["responses"][key] = result
        self._save()
        self.state.record_call(at=datetime.now(timezone.utc), session=self.manifest["session"],
                               outcome="ok", origin="user")
        self.state.save()
        print(f"History model call {self.data['calls']} of {self.manifest['max_calls']} completed.", flush=True)
        return result

    def _save(self):
        write_text_atomic(self.path, json.dumps(self.data, indent=2) + "\n")


def _receipt(store, plan_id, phase):
    raw = store.get_meta(RECEIPTS + plan_id + "." + phase)
    return json.loads(raw) if raw else None


def execute(store, state, manifest, engine, *, checkpoint: Path) -> dict:
    """Caller holds RunLock. Each application and its receipt share a transaction."""
    validate(manifest)
    path, entry, aliases = _canonical(state, manifest["session"])
    if not same_log(path, manifest["source"]["path"]):
        raise ValueError("canonical transcript changed")
    done = _receipt(store, manifest["id"], "complete")
    if done:
        return done
    audit_done = _receipt(store, manifest["id"], "audit")
    forward_done = _receipt(store, manifest["id"], "forward")
    if not forward_done and state_seal(entry) != manifest["state_seal"]:
        raise ValueError("session progress changed; prepare a fresh history plan")
    known_after = (audit_done or {}).get("post_seals", {})
    audited = set(manifest["selected_ids"]) if audit_done else set()
    for row in manifest["records"] + manifest["bindings"]:
        expected = known_after.get(row["id"], row["seal"])
        if not forward_done and row["id"] not in audited and record_seal(store, row["id"]) != expected:
            raise ValueError(f"record changed since history planning: {row['id']}")
        current = store.get(row["id"])
        if current and row["id"] not in audited and (current.source_session != manifest["session"] or current.source_agent != LEARNER_AGENT):
            raise ValueError("history plan includes a differently owned record")
    hold = entry.get("history_hold")
    if hold and hold != manifest["state"].get("history_hold") and hold != {"plan": manifest["id"]}:
        raise ValueError("session is held by another history repair")
    entry["history_hold"] = {"plan": manifest["id"]}
    state.save()
    with SyncLock(), store.transaction():
        for row in manifest["bindings"]:
            if row["id"] not in audited:
                provenance.put(store, row["id"], row["source"])
        for record_id in manifest["selected_ids"]:
            record = store.get(record_id)
            if record and record.status == "active" and PROVISIONAL_TAG in record.tags:
                provenance.hold(store, record, "approved source-aware history audit pending")
    adapter = CachedEngine(engine, checkpoint, manifest, state)
    memory = Memory(store, agent=LEARNER_AGENT, record_events=False)
    snapshot = Path(manifest["source"]["snapshot"])
    result = {"manifest": manifest["id"], "session": manifest["session"]}

    def step(phase, *, selected=None, start=0, end=None):
        existing = _receipt(store, manifest["id"], phase)
        if existing:
            return existing
        session, read_end = read_session(snapshot, start=start, stop=end)
        session.path = path
        parts = build_dossiers(session, budget=PART_CHARACTERS, require_user=start == 0)
        expected = {r["id"]: record_seal(store, r["id"]) for r in manifest["bindings"] + manifest["records"]}
        outside = {}

        def before():
            validate(manifest)
            if state_seal(entry) != manifest["state_seal"]:
                raise ValueError("session progress changed during history audit")
            for record_id, seal in expected.items():
                if record_seal(store, record_id) != seal:
                    raise ValueError(f"record changed during history audit: {record_id}")
            allowed = set(selected) if selected is not None else {
                r.id for r in store.tagged_active(PROVISIONAL_TAG, session=manifest["session"], limit=100_000)
                if (source := provenance.get(store, r.id)) is not None
                and tuple(source["start"]) >= (start, 0) and provenance.owned(source, parts)}
            # The central gates may legitimately touch a known non-provisional
            # target, including duplicate confirmation and conflict tags. Protect
            # only this session's other still-provisional source work.
            for row in store.tagged_active(PROVISIONAL_TAG, session=manifest["session"], limit=100_000):
                if row.id not in allowed:
                    outside[row.id] = record_seal(store, row.id)

        def after(report):
            for record_id, seal in outside.items():
                if record_seal(store, record_id) != seal:
                    raise ValueError(f"history review attempted to modify an unselected existing record: {record_id}")
            info = {"outcomes": dict(report.outcomes), "results": report.results,
                    "post_seals": {r["id"]: record_seal(store, r["id"])
                                   for r in manifest["records"] + manifest["bindings"]},
                    "applied_at": datetime.now(timezone.utc).isoformat()}
            if phase == "forward":
                info["final_end"] = read_end
                info["end_event_index"] = parts[-1].end_event_index if parts else 0
            store.set_meta(RECEIPTS + manifest["id"] + "." + phase, json.dumps(info))
            journal.record(store, "learning", f"Completed bounded history {phase} review",
                           outcome="ok", session=manifest["session"], run=manifest["id"] + "-" + phase,
                           details={"history_plan": manifest["id"], "selected": sorted(selected or []),
                                    "outcomes": info["outcomes"], "start": start, "end": read_end})

        if selected is not None and not selected:
            from .settle import Settled
            with SyncLock(), store.transaction():
                before()
                after(Settled())
        else:
            settle(memory, adapter, parts, session=session, run=manifest["id"] + "-" + phase,
                   selected_ids=set(selected) if selected is not None else None,
                   extract_new=selected is None, review_start=(start, 0) if selected is None else None,
                   before_apply=before, after_apply=after, apply_lock=SyncLock())
        return _receipt(store, manifest["id"], phase)

    result["audit"] = step("audit", selected=manifest["selected_ids"], end=manifest["source"]["end"])
    still_pending = [rid for rid in manifest["selected_ids"] if (row := store.get(rid))
                     and row.status == "active" and PROVISIONAL_TAG in row.tags]
    if still_pending:
        raise ValueError("selected historical records remain provisional; forward review remains held")
    result["forward"] = step("forward", start=manifest["state"]["settled"], end=manifest["forward_end"])
    # The receipt is committed with the actual review. If a process stops
    # between that transaction and this state-file write, acknowledge exactly
    # the same completed range on restart; never merge alias cursors.
    if state_seal(entry) == manifest["state_seal"]:
        forward = result["forward"]
        _finish(entry, manifest["session"], forward["final_end"], datetime.now(timezone.utc),
                settled=True, event_index=forward["end_event_index"])
        state.save()
    elif (entry.get("settled", 0), entry.get("settled_event_index", 0)) < (
            result["forward"]["final_end"], result["forward"]["end_event_index"]):
        raise ValueError("completed history receipt conflicts with current final progress")
    for alias in list(state.logs):
        if alias in aliases and state.logs[alias] is not entry:
            other = state.logs[alias]
            if other.get("pending_reviews") or other.get("recovery_ranges") or any(
                    other.get(k) for k in ("failures", "final_failures", "recovery_failures")):
                raise ValueError("alias acquired independent review work; leave it for explicit investigation")
            state.log_aliases[path_key(alias)] = next(k for k, v in state.logs.items() if v is entry)
            with store.transaction():
                store.set_meta(RECEIPTS + manifest["id"] + ".alias." + digest(alias),
                               json.dumps({"path": alias, "entry": other, "canonical": str(path),
                                           "retired_at": datetime.now(timezone.utc).isoformat()}))
            del state.logs[alias]
    if entry.get("history_hold") == {"plan": manifest["id"]}:
        entry.pop("history_hold", None)
    state.save()
    result["calls"] = adapter.data["calls"]
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    with SyncLock(), store.transaction():
        store.set_meta(RECEIPTS + manifest["id"] + ".complete", json.dumps(result))
        provenance.waiting(store)
    return result


def run(arguments):
    from .command import build_engine
    try:
        if arguments.history_plan:
            store = Store.open_read_only(database_path())
            try:
                manifest = prepare(store, LearnerState(), session_id=arguments.session,
                                   replay_runs=arguments.replay_run or [], output=arguments.history_plan,
                                   max_calls=32 if arguments.max_calls is None else arguments.max_calls)
            finally:
                store.close()
            print(f"Prepared sealed history plan: {len(manifest['selected_ids'])} selected records; "
                  f"{manifest['audit_parts']} audit parts; cap {manifest['max_calls']} calls. No learning changed.")
            return 0
        manifest = json.loads(arguments.repair_history.read_text(encoding="utf-8"))
        settings = load_settings()
        engine = build_engine(settings.backend, settings.model)
        if engine is None:
            raise ValueError("the selected learning backend is unavailable")
        with RunLock():
            store = Store.open(database_path())
            try:
                result = execute(store, LearnerState(), manifest, engine,
                                 checkpoint=arguments.repair_history.with_suffix(".model-checkpoint.json"))
            finally:
                store.close()
        write_text_atomic(arguments.repair_history.with_suffix(".result.json"), json.dumps(result, indent=2) + "\n")
        print(f"History repair completed with {result['calls']} model calls. Established quick progress was preserved.")
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, StoreError, LockBusy, ExtractionError) as exc:
        print(f"History repair remains pending: {exc}")
        return 1
