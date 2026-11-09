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
from contextlib import closing
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
BRANCH_FORMAT = "history-repair-physical-v1"
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


def _alias_group(state, session_id, transcript=None):
    names = [name for name, entry in state.logs.items() if entry.get("session_id") == session_id]
    if transcript is not None:
        names = [name for name in names if os.path.samefile(transcript, state.logs[name].get("log_path", name))]
    if not names:
        raise ValueError("session is not indexed")
    established = set(state.log_aliases.values())
    names.sort(key=lambda name: (name not in established, name.startswith("\\\\?\\"), "~" in name, name))
    path = Path(state.logs[names[0]].get("log_path", names[0]))
    if any(not os.path.samefile(path, state.logs[name].get("log_path", name)) for name in names[1:]):
        raise ValueError("session has distinct physical histories")
    return path, names


def prepare_alias_conflicts(store, state, *, session_id, output, transcript=None,
                            max_calls=32, max_bytes=256 * 1024 * 1024):
    """Seal conflicting quick progress for a bounded re-review from the beginning."""
    if output.exists():
        raise ValueError("history manifest already exists")
    if type(max_calls) is not int or not 2 <= max_calls <= 100:
        raise ValueError("history audit call cap must be between 2 and 100")
    if transcript is not None and not Path(transcript).is_absolute():
        raise ValueError("the selected transcript path must be absolute")
    path, names = _alias_group(state, session_id, transcript)
    if len(names) < 2 and transcript is None:
        raise ValueError("session has no competing transcript checkpoints")
    entries = json.loads(json.dumps({name: state.logs[name] for name in names}))
    siblings = {name: entry for name, entry in state.logs.items()
                if entry.get("session_id") == session_id and name not in entries}
    sources = [path]
    for name, entry in siblings.items():
        other = Path(entry.get("log_path", name))
        if not any(os.path.samefile(other, existing) for existing in sources):
            sources.append(other)
    end = line_boundary(path)
    if not 0 < end <= max_bytes:
        raise ValueError("transcript is empty or exceeds the approved snapshot byte cap")
    with path.open("rb") as source:
        for name, entry in entries.items():
            pending = entry.get("pending_reviews", {})
            phases = {"quick", "final"} if transcript is not None else {"quick"}
            if set(pending) - phases or (entry.get("final_failures") and "final" not in pending) or any(entry.get(key) for key in (
                    "recovery_ranges", "learn_to", "resume_at", "set_aside_to",
                    "recovery_failures", "event_index", "settled_event_index")):
                raise ValueError("unsupported checkpoint or pending review phase")
            if hold := entry.get("history_hold"):
                if name != names[0] or not isinstance(hold, dict) or hold.get("kind") != "alias-conflicts":
                    raise ValueError("transcript is held by another kind of history repair")
                if _receipt(store, hold["plan"], "forward"):
                    raise ValueError("the prior alias review committed; resume that plan to finish saving progress")
            boundaries = [entry.get("offset", 0), entry.get("settled", 0)]
            for review in pending.values():
                if review.get("event_index") or review.get("end_event_index"):
                    raise ValueError("pending work must have complete-line boundaries")
                lower, upper = review["start"], review["end"]
                if type(lower) is not int or type(upper) is not int or not 0 <= lower < upper <= end:
                    raise ValueError("invalid pending review range")
                boundaries.extend((lower, upper))
            for boundary in boundaries:
                if type(boundary) is not int or not 0 <= boundary <= end:
                    raise ValueError("checkpoint is outside the transcript snapshot")
                if boundary:
                    source.seek(boundary - 1)
                    if source.read(1) != b"\n":
                        raise ValueError("checkpoint is not on a complete transcript line")
    sha = prefix_hash(path, end)
    session, read_end = read_session(path, stop=end)
    if session.session_id != session_id or read_end != end:
        raise ValueError("snapshot session identity or boundary differs")
    parts = build_dossiers(session, budget=PART_CHARACTERS)
    memory = Memory(store, agent=LEARNER_AGENT, record_events=False)
    cache, bindings, unresolved, pending_ids = {}, [], [], []
    for record in store.tagged_active(PROVISIONAL_TAG, session=session_id, limit=100_000):
        pending_ids.append(record.id)
        matches = [source for candidate in sources
                   if (source := provenance.locate_legacy(memory, record, candidate, cache, persist=False))
                   is not None and os.path.samefile(source["path"], candidate)]
        if len(matches) != 1:
            unresolved.append(record.id)
            continue
        source = matches[0]
        if not os.path.samefile(source["path"], path):
            continue
        if provenance.owned(source, parts):
            bindings.append({"id": record.id, "seal": record_seal(store, record.id), "source": source})
        else:
            unresolved.append(record.id)
    if unresolved:
        raise ValueError(f"unresolved source bindings remain: {', '.join(unresolved[:10])}")
    if len(names) < 2 and not bindings and not entries[names[0]].get("pending_reviews"):
        raise ValueError("the selected transcript has no pending work or competing checkpoints")
    # Preserve the canonical final checkpoint when every outstanding source
    # is entirely covered by its suffix. Never use an alias's quick cursor
    # to infer completed final work.
    start = 0
    settled = entries[names[0]].get("settled", 0)
    if (settled and all(row["source"]["start"][0] >= settled for row in bindings)
            and all(review["start"] >= settled for entry in entries.values()
                    for review in entry.get("pending_reviews", {}).values())):
        suffix, _ = read_session(path, start=settled, stop=end)
        suffix_parts = build_dossiers(suffix, budget=PART_CHARACTERS, require_user=False)
        if all(provenance.owned(row["source"], suffix_parts) for row in bindings):
            start, parts = settled, suffix_parts
    required = len(parts) + 1
    if required > max_calls:
        raise ValueError("bounded final review exceeds the approved call cap")
    snapshot = output.parent / (output.stem + ".source") / path.name
    if _snapshot(path, snapshot, end) != sha:
        raise ValueError("transcript prefix changed while planning")
    manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                "session": session_id, "source": {"path": str(path), "snapshot": str(snapshot.resolve()),
                                                   "end": end, "sha256": sha},
                "alias_entries": entries, "canonical": names[0], "aliases": names,
                "alias_routes": {alias: target for alias, target in state.log_aliases.items() if target in entries},
                "state": entries[names[0]], "state_seal": state_seal(entries[names[0]]),
                "bindings": bindings, "records": [], "selected_ids": [], "forward_start": start,
                "forward_end": end, "audit_parts": 0, "max_calls": max_calls, "required_calls": required}
    if transcript is not None:
        manifest["format"] = BRANCH_FORMAT
        manifest["branch_pending"] = pending_ids
        manifest["sibling_entries"] = json.loads(json.dumps(siblings))
        manifest["sibling_routes"] = {alias: target for alias, target in state.log_aliases.items() if target in siblings}
    manifest["id"] = digest(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(output, json.dumps(manifest, indent=2) + "\n")
    return manifest


def _alias_hold(manifest):
    return {"plan": manifest["id"], "kind": "alias-conflicts"}


def _verify_alias_entries(state, manifest, *, check_siblings=True):
    selected = manifest["source"]["path"] if "branch_pending" in manifest else None
    path, names = _alias_group(state, manifest["session"], selected)
    if set(names) != set(manifest["aliases"]) or not same_log(path, manifest["source"]["path"]):
        raise ValueError("transcript aliases changed since history planning")
    for name, original in manifest["alias_entries"].items():
        current = state.logs[name]
        # The hold belongs to this plan. Conflict flags and the most recent
        # path spelling are derived while reading state and carry no progress.
        derived = {"history_hold", "identity_conflicts", "log_path"}
        if ({k: v for k, v in current.items() if k not in derived} !=
                {k: v for k, v in original.items() if k not in derived}):
            raise ValueError("alias checkpoint changed since history planning")
        allowed_hold = _alias_hold(manifest) if name == manifest["canonical"] else None
        if current.get("history_hold") not in (original.get("history_hold"), allowed_hold):
            raise ValueError("transcript alias is held by another history plan")
    if selected is not None:
        siblings = {name: entry for name, entry in state.logs.items()
                    if entry.get("session_id") == manifest["session"] and name not in names}
        sibling_keys = {path_key(spelling) for name, entry in siblings.items()
                        for spelling in (name, entry.get("log_path", name))}
        selected_keys = {path_key(spelling) for name in names
                         for spelling in (name, state.logs[name].get("log_path", name))}
        if (any(state.log_aliases.get(key) in names for key in sibling_keys)
                or any(state.log_aliases.get(key) in siblings for key in selected_keys)):
            raise ValueError("a route crosses physical transcripts")
        routes = {alias: target for alias, target in state.log_aliases.items() if target in siblings}
        if check_siblings and (siblings != manifest["sibling_entries"] or routes != manifest["sibling_routes"]):
            raise ValueError("sibling transcript progress or routes changed since history planning")


def _verify_branch_pending(store, manifest):
    if "branch_pending" in manifest:
        current = store.tagged_active(PROVISIONAL_TAG, session=manifest["session"], limit=100_000)
        if not {record.id for record in current} <= set(manifest["branch_pending"]):
            raise ValueError("new provisional records need physical source verification")


def validate(manifest):
    unsigned = {k: v for k, v in manifest.items() if k != "id"}
    expected_format = BRANCH_FORMAT if "branch_pending" in manifest else FORMAT
    if manifest.get("format") != expected_format or manifest.get("id") != digest(unsigned):
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
    done = _receipt(store, manifest["id"], "complete")
    if done:
        return done
    audit_done = _receipt(store, manifest["id"], "audit")
    forward_done = _receipt(store, manifest["id"], "forward")
    alias_entries = manifest.get("alias_entries")
    aliases_applied = False
    if alias_entries:
        path = Path(manifest["source"]["path"])
        canonical = manifest["canonical"]
        aliases = manifest["aliases"]
        entry = state.logs[canonical]
        aliases_applied = (forward_done is not None
                           and not any(name in state.logs for name in aliases if name != canonical)
                           and (len(aliases) > 1 or entry.get("history_hold") != _alias_hold(manifest)))
        if aliases_applied:
            if (entry.get("settled", 0), entry.get("settled_event_index", 0)) < (
                    forward_done["final_end"], forward_done["end_event_index"]):
                raise ValueError("completed alias review conflicts with current final progress")
        else:
            _verify_alias_entries(state, manifest, check_siblings=forward_done is None)
    else:
        path, entry, aliases = _canonical(state, manifest["session"])
        if not same_log(path, manifest["source"]["path"]):
            raise ValueError("canonical transcript changed")
    if not forward_done and state_seal(entry) != manifest["state_seal"]:
        raise ValueError("session progress changed; prepare a fresh history plan")
    if not forward_done:
        _verify_branch_pending(store, manifest)
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
    planned_hold = _alias_hold(manifest) if alias_entries else {"plan": manifest["id"]}
    if hold and hold not in (manifest["state"].get("history_hold"), planned_hold):
        raise ValueError("session is held by another history repair")
    if not forward_done:
        with SyncLock(), store.transaction():
            previous = manifest["state"].get("history_hold")
            if alias_entries and previous:
                if _receipt(store, previous["plan"], "forward"):
                    raise ValueError("the prior alias review committed; resume that plan")
                if not _receipt(store, manifest["id"], "supersedes"):
                    store.set_meta(RECEIPTS + manifest["id"] + ".supersedes", json.dumps(previous))
            for row in manifest["bindings"]:
                if row["id"] not in audited:
                    provenance.put(store, row["id"], row["source"])
            for record_id in manifest["selected_ids"]:
                record = store.get(record_id)
                if record and record.status == "active" and PROVISIONAL_TAG in record.tags:
                    provenance.hold(store, record, "approved source-aware history audit pending")
        entry["history_hold"] = planned_hold
        state.save()
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
        reviewed = set(selected) if selected is not None else None
        if phase == "forward" and "branch_pending" in manifest:
            reviewed = {row["id"] for row in manifest["bindings"]}

        def before():
            validate(manifest)
            if alias_entries:
                _verify_alias_entries(state, manifest)
            _verify_branch_pending(store, manifest)
            if state_seal(entry) != manifest["state_seal"]:
                raise ValueError("session progress changed during history audit")
            for record_id, seal in expected.items():
                if record_seal(store, record_id) != seal:
                    raise ValueError(f"record changed during history audit: {record_id}")
            allowed = reviewed if reviewed is not None else {
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
                if alias_entries:
                    if any((row := store.get(item["id"])) is None or (
                            row.status == "active" and PROVISIONAL_TAG in row.tags)
                           for item in manifest["bindings"]):
                        raise ValueError("original provisional records remain unresolved; checkpoints are held")
                    info["original_alias_entries"] = alias_entries
                    info["original_alias_routes"] = manifest.get("alias_routes", {})
            store.set_meta(RECEIPTS + manifest["id"] + "." + phase, json.dumps(info))
            journal.record(store, "learning", f"Completed bounded history {phase} review",
                           outcome="ok", session=manifest["session"], run=manifest["id"] + "-" + phase,
                           details={"history_plan": manifest["id"], "selected": sorted(reviewed or []),
                                    "outcomes": info["outcomes"], "start": start, "end": read_end})

        if selected is not None and not selected:
            from .settle import Settled
            with SyncLock(), store.transaction():
                before()
                after(Settled())
        else:
            settle(memory, adapter, parts, session=session, run=manifest["id"] + "-" + phase,
                   selected_ids=reviewed,
                   extract_new=selected is None, review_start=(start, 0) if selected is None else None,
                   before_apply=before, after_apply=after, apply_lock=SyncLock())
        return _receipt(store, manifest["id"], phase)

    result["audit"] = step("audit", selected=manifest["selected_ids"], end=manifest["source"]["end"])
    still_pending = [rid for rid in manifest["selected_ids"] if (row := store.get(rid))
                     and row.status == "active" and PROVISIONAL_TAG in row.tags]
    if still_pending:
        raise ValueError("selected historical records remain provisional; forward review remains held")
    result["forward"] = step("forward", start=manifest.get("forward_start", manifest["state"].get("settled", 0)),
                             end=manifest["forward_end"])
    # The receipt is committed with the actual review. If a process stops
    # between that transaction and this state-file write, acknowledge exactly
    # the same completed range on restart; never merge alias cursors.
    if alias_entries:
        if not aliases_applied:
            _verify_alias_entries(state, manifest, check_siblings=False)
            forward = result["forward"]
            _finish(entry, manifest["session"], forward["final_end"], datetime.now(timezone.utc),
                    settled=True, event_index=forward["end_event_index"])
            for name in aliases:
                if name != canonical:
                    del state.logs[name]
                state.log_aliases[path_key(name)] = canonical
            for alias, target in state.log_aliases.items():
                if target in aliases:
                    state.log_aliases[alias] = canonical
            entry.pop("identity_conflicts", None)
            entry.pop("history_hold", None)
            state.save()
    elif state_seal(entry) == manifest["state_seal"]:
        forward = result["forward"]
        _finish(entry, manifest["session"], forward["final_end"], datetime.now(timezone.utc),
                settled=True, event_index=forward["end_event_index"])
        state.save()
    elif (entry.get("settled", 0), entry.get("settled_event_index", 0)) < (
            result["forward"]["final_end"], result["forward"]["end_event_index"]):
        raise ValueError("completed history receipt conflicts with current final progress")
    for alias in [] if alias_entries else list(state.logs):
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
            options = {"session_id": arguments.session, "output": arguments.history_plan,
                       "max_calls": 32 if arguments.max_calls is None else arguments.max_calls}
            with closing(Store.open_read_only(database_path())) as store:
                if getattr(arguments, "alias_conflicts", False):
                    manifest = prepare_alias_conflicts(store, LearnerState(),
                                                       transcript=getattr(arguments, "transcript", None), **options)
                else:
                    if getattr(arguments, "transcript", None) is not None:
                        raise ValueError("--transcript requires --alias-conflicts")
                    manifest = prepare(store, LearnerState(), replay_runs=arguments.replay_run or [], **options)
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
