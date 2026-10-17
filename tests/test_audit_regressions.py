"""Regressions for the October 16 audit and its final-review edge cases."""

import json
import os
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from _support import Clock

from knowitall2 import sync
from knowitall2.learning.dossier import Dossier, build_dossiers, _trusted_output
from knowitall2.learning.extractor import ExtractionError
from knowitall2.learning.learner import publish, validate
from knowitall2.learning.learner import learn
from knowitall2.learning.state import LearnerSettings, LearnerState
from knowitall2.learning.transcripts import Event, Session, read_claude_code_session
from knowitall2.memory import Memory, MemoryInputError, same_statement
from knowitall2.secrets import contains_secret, redact
from knowitall2.server import exchange, open_store
from knowitall2.store import Store


class AuditTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {"KNOWITALL2_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = Store.in_memory()
        self.addCleanup(self.store.close)
        self.memory = Memory(self.store, agent="audit", clock=Clock(), record_events=False)

    def candidate(self, text, evidence, **options):
        return {"text": text, "evidence": evidence, "kind": "rule", "scope": "global",
                "subjects": ["deployment"], **options}

    def dossier(self, words):
        return Dossier("audit", "claude-code", str(self.root), None, 0,
                       text="[user] " + words, user_texts=[words])


class UserAuthorityTests(AuditTestCase):
    def test_explicit_rule_words_preserve_all_technical_case(self):
        from knowitall2.learning.learner import makes_a_rule

        instruction = "always use /srv/Production/Backups for database backups."
        words = "From now on, " + instruction
        self.assertTrue(makes_a_rule(words, instruction[0].upper() + instruction[1:]))
        self.assertFalse(makes_a_rule(words, instruction.replace("Production/Backups", "production/backups")))
        for actual, changed in (("--Mode=Strict", "--mode=strict"), ("ProdEndpoint", "prodendpoint")):
            original = f"Use {actual} for database backups."
            self.assertFalse(makes_a_rule(original + " Make that a rule.", original.replace(actual, changed)))

    def test_technical_case_and_unicode_spelling_never_transfer_authority(self):
        for actual, altered in (("/srv/Production/Backups", "/srv/production/backups"),
                                ("--Mode=Strict", "--mode=strict"),
                                ("ProdEndpoint", "prodendpoint"), ("ï¼¡lpha", "Alpha")):
            with self.subTest(value=actual):
                source = f"Always use {actual} for database backups."
                candidate = source.replace(actual, altered)
                checked, reason = validate(self.candidate(candidate, source), self.dossier(source))
                self.assertIsNotNone(checked, reason)
                if checked["source"] == "user":
                    self.assertEqual(source, checked["text"])
                else:
                    self.assertEqual("inferred", checked["source"])
                original = self.memory.remember(candidate, kind="note", detect_project=False).record
                rule = self.memory.remember(source, kind="rule", source="user", detect_project=False).record
                self.assertEqual(source, rule.text)
                self.assertNotEqual(original.id, rule.id)
                self.assertEqual("note", self.store.get(original.id).kind)

    def test_a_reversed_rule_never_replaces_the_users_original(self):
        original = ("Deployments must not proceed without explicit approval from me after the release notes "
                    "and validation results have been reviewed.")
        older = self.memory.remember(original, kind="rule", source="user", detect_project=False).record
        dossier = self.dossier(original)
        dossier.known = [older]
        reversed_rule = original.replace("must not", "must")
        candidate = self.candidate(reversed_rule, original, relation="updates", known_id=older.id)
        self.assertFalse(same_statement(older, kind="rule", text=reversed_rule))
        self.assertEqual("already known", publish(self.memory, candidate, dossier))
        self.assertEqual("active", self.store.get(older.id).status)
        self.assertEqual(original, self.store.get(older.id).text)

    def test_authority_cannot_come_from_omitted_or_reordered_words(self):
        for source, claimed, evidence in (
            ("Do not deploy application changes without approval.",
             "Deploy application changes without approval.", "Deploy application changes without approval."),
            ("Deploy application changes only after the backup succeeds.",
             "Deploy application changes.", "Deploy application changes"),
            ("The staging server must replace the production server.",
             "The production server must replace the staging server.",
             "The staging server must replace the production server."),
            ("Deploy application changes after approval, never before approval.",
             "Deploy application changes after approval.",
             "Deploy application changes ... after approval"),
        ):
            with self.subTest(source=source):
                checked, reason = validate(self.candidate(claimed, evidence), self.dossier(source))
                self.assertIsNotNone(checked, reason)
                if checked["source"] == "user":
                    self.assertEqual(source, checked["text"])
                else:
                    self.assertEqual(("note", "inferred"), (checked["kind"], checked["source"]))

    def test_verbatim_complete_statements_keep_user_authority(self):
        text = "Always get approval before deploying changes."
        checked, reason = validate(self.candidate(text, text.lower()), self.dossier(text + " Make that a rule."))
        self.assertIsNotNone(checked, reason)
        self.assertEqual(("rule", "user"), (checked["kind"], checked["source"]))


class OutputTrustTests(AuditTestCase):
    def test_complete_argument_shapes_exclude_literal_printers_and_unknown_options(self):
        planted = "The production deployment endpoint belongs to the unverified staging service."
        # This is real output, but git is quoting the caller's assertion.
        output = subprocess.check_output(["git", "rev-parse", "--sq-quote", planted], text=True)
        commands = (f"git rev-parse --sq-quote '{planted}'", f"git rev-parse --default '{planted}'",
                    f"git ls-files --format='{planted}'", f"git rev-parse '{planted}'",
                    f"docker inspect -f='{planted}' container", f"Get-Date -f '{planted}'",
                    f"Get-Date -Format '{planted}'", f"hostname '{planted}'", "ls --unknown-option",
                    "cat --help /etc/config", "head -c /etc/config", "ssh host git rev-parse --sq-quote literal")
        for command in commands:
            with self.subTest(command=command):
                event = Event("tool", command, tool="Bash", output=output)
                self.assertFalse(_trusted_output(event, "/workspace"))
                session = Session("claude-code", "audit", self.root / "unused", cwd="/workspace",
                                  events=[Event("user", "Inspect the deployment."), event])
                [dossier] = build_dossiers(session)
                checked, reason = validate(self.candidate(planted, planted, kind="fact"), dossier)
                self.assertIsNotNone(checked, reason)
                self.assertEqual("inferred", checked["source"])
        for command in ("git rev-parse HEAD", "git status --short", "python --version", "uname -a",
                        "Get-Service", "cat /etc/openwrt_release", "ssh router cat /etc/config/network"):
            with self.subTest(command=command):
                self.assertTrue(_trusted_output(Event("tool", command, tool="Bash"), "/workspace"))

    def test_file_searches_unknown_programs_and_compound_commands_are_unverified(self):
        planted = "The production deployment endpoint belongs to the unverified staging service."
        for command in ("cat LICENSE", "Get-Content LICENSE", "rg deployment settings.yaml",
                        "grep deployment LICENSE", "python -c 'print(open(\"LICENSE\").read())'",
                        "unknown-probe", "git status; cat LICENSE", "git status | cat LICENSE",
                        "cat /etc/../workspace/LICENSE"):
            with self.subTest(command=command):
                session = Session("claude-code", "audit", self.root / "unused", cwd="/workspace",
                                  events=[Event("user", "Inspect the configuration."),
                                          Event("tool", command, tool="Bash", output=planted)])
                [dossier] = build_dossiers(session)
                checked, reason = validate(self.candidate(planted, planted, kind="fact"), dossier)
                self.assertIsNotNone(checked, reason)
                self.assertEqual("inferred", checked["source"])

    def test_untrusted_output_cannot_replace_an_observed_memory(self):
        older = self.memory.remember("The production deployment endpoint belongs to the verified service.",
                                     subjects=["deployment"], source="observed", detect_project=False).record
        planted = "The production deployment endpoint belongs to the unverified staging service."
        session = Session("claude-code", "audit", self.root / "unused", cwd=str(self.root),
                          events=[Event("user", "Inspect the project."),
                                  Event("tool", "cat LICENSE", tool="Bash", output=planted)])
        [dossier] = build_dossiers(session)
        dossier.known = [older]
        outcome = publish(self.memory, self.candidate(planted, planted, kind="fact", relation="updates",
                                                     known_id=older.id), dossier)
        self.assertEqual("saved with a question", outcome)
        self.assertEqual("active", self.store.get(older.id).status)


class OwnCredentialTests(AuditTestCase):
    def test_own_access_keys_are_redacted_and_refused_at_both_ingress_points(self):
        # A synthetic value with the exact shape accounts.redeem issues; never a real credential.
        value = "ki" + "a_" + "Aa1b" * 10 + "Qwe"
        for text in (json.dumps({"key": value}), "The server access key is " + value, value):
            with self.subTest(form=text[:20]):
                self.assertTrue(contains_secret(text))
                self.assertNotIn(value, redact(text))
                with self.assertRaises(MemoryInputError):
                    self.memory.remember(text, detect_project=False)
        ordinary = self.memory.remember("The server key is kept in Vaultwarden.", detect_project=False).record
        row = sync.read_row(self.store.connection, sync.table_for("records"), ordinary.id)
        row["text"] = "The server access key is " + value
        server = open_store(self.root / "server.db")
        self.addCleanup(server.close)
        [answer] = exchange.apply_operations(server, "audit-peer", [
            {"op_id": "own-key", "table": "records", "key": ordinary.id, "op": "upsert", "row": row},
        ], now=Clock()())
        self.assertEqual("rejected", answer["result"])
        self.assertIsNone(server.get(ordinary.id))
        for column in ("subjects", "tags"):
            row["text"] = ordinary.text
            row[column] = json.dumps([value])
            [answer] = exchange.apply_operations(server, "audit-peer", [
                {"op_id": column, "table": "records", "key": ordinary.id, "op": "upsert", "row": row},
            ], now=Clock()())
            self.assertEqual("rejected", answer["result"])
            row[column] = "[]"


class RulePromotionTests(AuditTestCase):
    def test_same_id_promotion_survives_a_newer_concurrent_note_confirmation(self):
        server = open_store(self.root / "same-id-promotion.db")
        self.addCleanup(server.close)
        record = self.memory.remember("Require explicit approval before deploying production changes.",
                                      kind="note", detect_project=False).record
        row = sync.read_row(self.store.connection, sync.table_for("records"), record.id)
        operation = {"op_id": "note", "table": "records", "key": record.id, "op": "upsert", "row": row}
        exchange.apply_operations(server, "first", [operation], now="2026-10-17T12:00:00Z")
        later_note = {**row, "updated_at": "2026-10-17T11:30:00Z", "tags": '["retained"]'}
        exchange.apply_operations(server, "first", [{**operation, "op_id": "later-note", "row": later_note}],
                                  now="2026-10-17T12:00:00Z")
        earlier_promotion = {**row, "kind": "rule", "verification": "user_stated", "source_kind": "user",
                             "updated_at": "2026-10-17T11:00:00Z", "tags": '["everywhere"]'}
        exchange.apply_operations(server, "offline", [{**operation, "op_id": "promotion", "row": earlier_promotion}],
                                  now="2026-10-17T12:00:00Z")
        kept = server.get(record.id)
        self.assertEqual(("rule", "user_stated", "user"), (kept.kind, kept.verification, kept.source_kind))
        self.assertEqual({"retained", "everywhere"}, set(kept.tags))

    def test_duplicate_metadata_merge_cannot_publish_an_oversized_wire_row(self):
        server = open_store(self.root / "metadata-limit.db")
        self.addCleanup(server.close)
        record = self.memory.remember("Require approval before deploying production changes.",
                                      kind="note", detect_project=False).record
        row = sync.read_row(self.store.connection, sync.table_for("records"), record.id)
        row["tags"] = json.dumps(["a" * 10_000])
        operation = {"op_id": "first", "table": "records", "key": record.id, "op": "upsert", "row": row}
        exchange.apply_operations(server, "first", [operation], now=Clock()())
        promoted = {**row, "id": "k-offline", "kind": "rule", "verification": "user_stated",
                    "source_kind": "user", "tags": json.dumps(["b" * 10_000])}
        [result] = exchange.apply_operations(server, "offline", [
            {**operation, "op_id": "second", "key": "k-offline", "row": promoted}], now=Clock()())
        self.assertEqual("rejected", result["result"])
        self.assertEqual("note", server.get(record.id).kind)
        self.assertIsNone(server.get("k-offline"))

    def test_server_returns_normalized_authority_even_when_no_new_change_is_needed(self):
        server = open_store(self.root / "normalized-server.db")
        self.addCleanup(server.close)
        record = self.memory.remember("Require explicit approval before deploying production changes.",
                                      kind="rule", source="user", detect_project=False).record
        row = sync.read_row(self.store.connection, sync.table_for("records"), record.id)
        operation = {"op_id": "canonical", "table": "records", "key": record.id, "op": "upsert", "row": row}
        exchange.apply_operations(server, "peer", [operation], now=Clock()())
        before = sync.latest_change(server)
        stale = {**row, "kind": "note", "verification": "unverified", "source_kind": "inferred"}
        [result] = exchange.apply_operations(server, "peer", [{**operation, "op_id": "stale", "row": stale}],
                                             now=Clock()())
        self.assertEqual("applied", result["result"])
        self.assertEqual(before, sync.latest_change(server))
        self.assertEqual(("rule", "user_stated", "user"),
                         tuple(result["row"][column] for column in ("kind", "verification", "source_kind")))
        # Repeating a note can strengthen verification without changing its old
        # source_kind; that snapshot must not rewrite a rule's provenance/tags.
        stale.update(verification="user_stated", source_kind="agent", tags='["stale"]')
        [result] = exchange.apply_operations(server, "peer", [{**operation, "op_id": "stale-confirmed", "row": stale}],
                                             now=Clock()())
        self.assertEqual(("rule", "user_stated", "user", ()),
                         (server.get(record.id).kind, server.get(record.id).verification,
                          server.get(record.id).source_kind, server.get(record.id).tags))

    def test_new_id_promotion_and_replacement_cannot_form_a_duplicate_cycle(self):
        server = open_store(self.root / "replacement-server.db")
        self.addCleanup(server.close)
        text = "Require explicit approval before deploying production changes."
        note = self.memory.remember(text, kind="note", detect_project=False).record
        table = sync.table_for("records")

        def send(record, ident):
            return exchange.apply_operations(server, "peer", [
                {"op_id": ident, "table": "records", "key": record.id, "op": "upsert",
                 "row": sync.read_row(self.store.connection, table, record.id)}], now=Clock()())[0]

        send(note, "note")
        rule = self.memory.promote_rule(note, text)
        self.assertEqual("duplicate", send(rule, "rule")["result"])
        send(note, "replacement")
        self.assertEqual(("rule", "user_stated", "active", None),
                         (server.get(note.id).kind, server.get(note.id).verification,
                          server.get(note.id).status, server.get(note.id).superseded_by))
        self.assertEqual(note.id, server.get(rule.id).superseded_by)

    def test_offline_duplicate_rule_promotions_converge_before_or_after_alias_upload(self):
        for upload_note_first in (False, True):
            with self.subTest(upload_note_first=upload_note_first):
                first, second = Store.in_memory(), Store.in_memory()
                self.addCleanup(first.close)
                self.addCleanup(second.close)
                server = open_store(self.root / f"offline-{upload_note_first}.db")
                self.addCleanup(server.close)
                a = Memory(first, agent="a", clock=Clock(), record_events=False)
                b = Memory(second, agent="b", clock=Clock(), record_events=False)
                text = "Require my explicit approval before deploying changes to production."
                canonical = a.remember(text, kind="note", tags=["original"], detect_project=False).record
                alias = b.remember(text, kind="note", tags=["offline"], detect_project=False).record
                table = sync.table_for("records")

                def send(store, key, operation_id):
                    [answer] = exchange.apply_operations(server, operation_id, [
                        {"op_id": operation_id, "table": "records", "key": key, "op": "upsert",
                         "row": sync.read_row(store.connection, table, key)}], now=Clock()())
                    self.assertNotEqual("rejected", answer["result"], answer)
                    return answer

                send(first, canonical.id, "first")
                if upload_note_first:
                    self.assertEqual("duplicate", send(second, alias.id, "alias-note")["result"])
                b.remember(text, kind="rule", source="user", tags=["everywhere"], detect_project=False)
                self.assertEqual("duplicate", send(second, alias.id, "alias-rule")["result"])
                kept = server.get(canonical.id)
                self.assertEqual(("rule", "user_stated", "user", "active"),
                                 (kept.kind, kept.verification, kept.source_kind, kept.status))
                self.assertEqual({"original", "offline", "everywhere"}, set(kept.tags))
                self.assertEqual("superseded", server.get(alias.id).status)
                # A stale confirmation on the canonical id cannot demote the rule.
                send(first, canonical.id, "stale-note")
                self.assertEqual(("rule", "user_stated"),
                                 (server.get(canonical.id).kind, server.get(canonical.id).verification))
                for client in (first, second):
                    sync.apply_pulled(client, sync.changes_since(server, 0)["changes"])
                    self.assertEqual("rule", client.get(canonical.id).kind)
                    self.assertEqual("superseded", client.get(alias.id).status)
                with server.transaction():
                    server.retire(canonical.id, reason="user retired", now=Clock()())
                # A later replay of an offline active alias cannot revive a retired canonical memory.
                with second.transaction():
                    second.restore(alias.id, verification="user_stated", now=Clock()())
                send(second, alias.id, "late-alias")
                self.assertEqual("retired", server.get(canonical.id).status)
                self.assertEqual("superseded", server.get(alias.id).status)

    def test_casefold_hash_collisions_are_not_server_duplicates(self):
        server = open_store(self.root / "case-server.db")
        self.addCleanup(server.close)
        for path in ("/srv/Production/Backups", "/srv/production/backups"):
            record = self.memory.remember(f"Always use {path} for database backups.",
                                          kind="rule", source="user", detect_project=False).record
            row = sync.read_row(self.store.connection, sync.table_for("records"), record.id)
            [answer] = exchange.apply_operations(server, "peer", [
                {"op_id": path, "table": "records", "key": record.id, "op": "upsert", "row": row}], now=Clock()())
            self.assertEqual("applied", answer["result"])
            self.assertEqual("active", server.get(record.id).status)
        self.assertEqual(2, server.count_active(project_id=None, scope="global", kinds=["rule"]))

    def test_explicit_rule_promotion_preserves_identity_and_syncs(self):
        sync.set_tracking(self.store, True)
        text = "Require my explicit approval before deploying changes to production."
        first = self.memory.remember(text, kind="note", tags=["original"], detect_project=False).record
        server = open_store(self.root / "server.db")
        self.addCleanup(server.close)
        operations = sync.pending_operations(self.store)
        exchange.apply_operations(server, "audit-peer", operations, now=Clock()())
        sync.acknowledge(self.store, operations)
        promoted = self.memory.remember(text, kind="rule", source="user", tags=["everywhere"], detect_project=False)
        self.assertEqual(first.id, promoted.record.id)
        self.assertEqual(("rule", "user_stated", "user"),
                         (promoted.record.kind, promoted.record.verification, promoted.record.source_kind))
        self.assertEqual({"original", "everywhere"}, set(promoted.record.tags))
        self.assertIn(first.id, self.memory.briefing(project_path=self.root))
        operations = sync.pending_operations(self.store)
        answers = exchange.apply_operations(server, "audit-peer", operations, now=Clock()())
        self.assertEqual(["applied"], [item["result"] for item in answers])
        self.assertEqual(("rule", "active"), (server.get(first.id).kind, server.get(first.id).status))
        again = self.memory.remember(text, kind="rule", source="user", detect_project=False)
        self.assertEqual("already_known", again.status)
        # A later inferred note neither demotes the rule nor changes its authority.
        self.memory.remember(text, kind="note", detect_project=False)
        self.assertEqual(("rule", "user_stated"),
                         (self.store.get(first.id).kind, self.store.get(first.id).verification))


class SyncSchemaTests(AuditTestCase):
    def test_question_kind_is_rechecked_under_the_answer_transaction(self):
        from knowitall2.review import OPTIONS, answer, settle

        old = self.memory.remember("The old deployment service uses host alpha.", detect_project=False).record
        new = self.memory.remember("The new deployment service uses host beta.", detect_project=False).record
        self.store.add_question(question_id="q-race", kind="conflict", prompt="Which is right?",
                                record_ids=[old.id, new.id], options=OPTIONS["conflict"], now=Clock()())
        read = self.store.question
        original = read("q-race")
        calls = []

        def changed_between_reads(question_id):
            if calls:
                self.store.connection.execute(
                    "UPDATE questions SET kind = 'still_true', record_ids = ?, options = ? WHERE id = ?",
                    (json.dumps([old.id]), json.dumps(OPTIONS["still_true"]), question_id))
            calls.append(question_id)
            return read(question_id)

        with mock.patch.object(self.store, "question", side_effect=changed_between_reads):
            with self.assertRaisesRegex(MemoryInputError, "Unknown choice"):
                answer(self.memory, "q-race", "keep_mine")
        # The failed answer rolled back even the simulated concurrent edit.
        with self.store.transaction():
            self.store.connection.execute(
                "UPDATE questions SET kind = 'still_true', record_ids = ?, options = ? WHERE id = 'q-race'",
                (json.dumps([old.id]), json.dumps(OPTIONS["still_true"])))
        self.assertIsNone(settle(self.memory, original, "keep_mine", by="audit", evidence="Checked service state."))
        self.assertEqual(("active", "active"), (self.store.get(old.id).status, self.store.get(new.id).status))
        self.assertEqual("open", read("q-race")["status"])

    def test_noncanonical_times_are_rejected_and_legacy_versions_compare_chronologically(self):
        table = sync.table_for("records")
        for value in ("2026-9-01T12:00:00Z", "2026-09-1T12:00:00Z", "2026-09-01T2:00:00Z",
                      "2026-09-01T12:0:00Z", "2026-09-01T12:00:0Z", "2026-09-01T12:00:00+00:00"):
            with self.subTest(value=value):
                with self.assertRaises(sync.SyncError):
                    sync.check_row(table, "example", {"updated_at": value})
        record = self.memory.remember("The audit deployment service runs in a container.", detect_project=False).record
        row = sync.read_row(self.store.connection, table, record.id)
        server = open_store(self.root / "time-server.db")
        self.addCleanup(server.close)
        row.update(status="retired", retired_reason="retired in October", updated_at="2026-10-15T12:00:00Z")
        operation = {"op_id": "october", "table": "records", "key": record.id, "op": "upsert", "row": row}
        exchange.apply_operations(server, "a", [operation], now="2026-10-17T12:00:00Z")
        stale = {**row, "status": "active", "retired_reason": None, "updated_at": "2026-9-01T12:00:00Z"}
        [answer] = exchange.apply_operations(server, "b", [{**operation, "op_id": "stale", "row": stale}],
                                             now="2026-10-17T12:00:00Z")
        self.assertEqual("rejected", answer["result"])
        with self.assertRaises(sync.SyncError):
            sync.apply_pulled(self.store, [{"seq": 9, "table": "records", "key": record.id,
                                          "op": "upsert", "row": stale}], cursor=9)
        self.assertEqual(0, sync.cursor(self.store))
        self.assertEqual("retired", server.get(record.id).status)
        # Legacy bad padding already in a server database also cannot bias ordering.
        with server.transaction():
            server.connection.execute("UPDATE records SET updated_at = '2026-9-01T12:00:00Z' WHERE id = ?", (record.id,))
        [answer] = exchange.apply_operations(server, "b", [{**operation, "op_id": "october-update"}],
                                             now="2026-10-17T12:00:00Z")
        self.assertEqual("applied", answer["result"])
        self.assertEqual("2026-10-15T12:00:00Z", server.get(record.id).updated_at)
        self.assertLess(exchange._no_later_than("2026-9-01T12:00:00Z", "2026-10-17T12:00:00Z"),
                        exchange._no_later_than("2026-10-01T12:00:00Z", "2026-10-17T12:00:00Z"))

    def test_question_cardinality_and_choices_validate_at_both_sync_boundaries(self):
        from knowitall2.review import OPTIONS, answer

        self.store.add_question(question_id="q-shape", kind="conflict", prompt="Which memory is right?",
                                record_ids=["k-old", "k-new"], options=OPTIONS["conflict"], now=Clock()())
        table = sync.table_for("questions")
        original = sync.read_row(self.store.connection, table, "q-shape")
        server = open_store(self.root / "question-server.db")
        self.addCleanup(server.close)
        cases = [({"record_ids": json.dumps(ids)}) for ids in ([], ["k-old"], ["k-old", "k-old"],
                                                                 ["k-old", ""], ["one", "two", "three"])]
        cases += [{"kind": "still_true"}, {"kind": "confirm_rule"}, {"kind": "future-kind"},
                  {"options": "[]"}, {"options": json.dumps([OPTIONS["conflict"][0]] * 3)},
                  {"options": json.dumps([{"key": "erase", "label": "Erase"}])}]
        for number, changed in enumerate(cases):
            row = {**original, **changed}
            with self.subTest(changed=changed):
                operation = {"op_id": f"invalid-{number}", "table": "questions", "key": "q-shape",
                             "op": "upsert", "row": row}
                [result] = exchange.apply_operations(server, "peer", [operation], now=Clock()())
                self.assertEqual("rejected", result["result"])
                with self.assertRaises(sync.SyncError):
                    sync.apply_pulled(self.store, [{"seq": 10, **operation}], cursor=10)
                self.assertEqual(0, sync.cursor(self.store))
                self.assertEqual(original, sync.read_row(self.store.connection, table, "q-shape"))
        # Check cross-field invariants after merging partial updates, too.
        sync.write_row(server.connection, table, original)
        [valid] = exchange.apply_operations(server, "peer", [
            {"op_id": "valid-partial", "table": "questions", "key": "q-shape", "op": "upsert",
             "base": sync.latest_change(server),
             "row": {"prompt": "A revised question prompt"}}], now=Clock()())
        self.assertEqual("applied", valid["result"])
        for changed in ({"record_ids": "[]"}, {"kind": "still_true"}):
            [result] = exchange.apply_operations(server, "peer", [
                {"op_id": str(changed), "table": "questions", "key": "q-shape", "op": "upsert",
                 "row": changed}], now=Clock()())
            self.assertEqual("rejected", result["result"])
            with self.assertRaises(sync.SyncError):
                sync.apply_pulled(self.store, [{"seq": 11, "table": "questions", "key": "q-shape",
                                              "op": "upsert", "row": changed}], cursor=11)
        # Existing poisoned rows fail with a useful input error rather than tuple-unpacking or any mutation.
        for changed in ({"record_ids": "[]"}, {"record_ids": "null"}, {"options": "null"}, {"options": "{}"}):
            with self.store.transaction():
                self.store.connection.execute("UPDATE questions SET record_ids = ?, options = ? WHERE id = 'q-shape'",
                                              (changed.get("record_ids", original["record_ids"]),
                                               changed.get("options", original["options"])))
            with self.assertRaisesRegex(MemoryInputError, "Invalid saved question"):
                answer(self.memory, "q-shape", "keep_mine")
        self.assertEqual("open", self.store.question("q-shape")["status"])

    def test_json_fields_require_the_structure_their_readers_use(self):
        shapes = {
            "records": {"subjects": ("[]", ["null", "42", "{}", '[1]', '[true]', '[["x"]]']),
                        "tags": ('["tag"]', ["null", '"text"'])},
            "systems": {"aliases": ('["alias"]', ["null", "{}", '[1]']),
                        "gaps": ('["missing"]', ["null", '[{}]'])},
            "questions": {"record_ids": ('["k-one"]', ["null", "{}", '[1]']),
                          "options": ('[{"key":"keep","label":"Keep it"}]',
                                      ["null", '["keep"]', '[{}]', '[{"key":1,"label":"Keep"}]'])},
            "question_stages": {"labels": ('{"keep":"Keep it"}', ["null", "[]", '{"keep":1}']),
                                "findings": ('["Checked it"]', ["null", "{}", '[1]'])},
        }
        for name, columns in shapes.items():
            table = sync.table_for(name)
            for column, (valid, invalid) in columns.items():
                self.assertEqual(valid, sync.check_row(table, "example", {column: valid})[column])
                for value in invalid:
                    with self.subTest(table=name, column=column, value=value):
                        with self.assertRaises(sync.SyncError):
                            sync.check_row(table, "example", {column: value})

    def test_scalar_fields_have_types_nullability_and_valid_times(self):
        for column, value in (("text", 42), ("text", None), ("recall_count", "2"),
                              ("recall_count", -1), ("recall_count", 1.5), ("updated_at", "tomorrow")):
            with self.subTest(column=column, value=value):
                with self.assertRaises(sync.SyncError):
                    sync.check_row(sync.table_for("records"), "k-one", {column: value})
        self.assertIsNone(sync.check_row(sync.table_for("records"), "k-one", {"last_used_at": None})["last_used_at"])

    def test_a_poisoned_row_is_rejected_by_server_and_client_without_advancing(self):
        original = self.memory.remember("The audit service runs in a dedicated container.",
                                        detect_project=False).record
        row = sync.read_row(self.store.connection, sync.table_for("records"), original.id)
        row["subjects"] = "null"
        server = open_store(self.root / "server.db")
        self.addCleanup(server.close)
        [answer] = exchange.apply_operations(server, "audit-peer", [
            {"op_id": "malformed", "table": "records", "key": original.id, "op": "upsert", "row": row},
        ], now=Clock()())
        self.assertEqual("rejected", answer["result"])
        self.assertEqual(0, sync.latest_change(server))
        with self.assertRaises(sync.SyncError):
            sync.apply_pulled(self.store, [{"seq": 8, "table": "records", "key": original.id,
                                          "op": "upsert", "row": row}], cursor=8)
        self.assertEqual(0, sync.cursor(self.store))
        self.assertEqual((), self.store.get(original.id).subjects)
        self.assertIn(original.id, self.memory.recall("audit service", project_path=self.root))

    def test_dependency_inspection_validates_foreign_keys_before_using_them(self):
        with self.assertRaises(sync.SyncError):
            sync.without_parents(self.store, [{"seq": 1, "table": "records", "key": "k-one", "op": "upsert",
                                               "row": {"project_id": []}}])

    def test_newer_peer_columns_are_ignored_but_known_fields_still_validate(self):
        record = self.memory.remember("The audit service uses a container.", detect_project=False).record
        row = sync.read_row(self.store.connection, sync.table_for("records"), record.id)
        row["future_column"] = {"shape": "unknown here"}
        change = {"seq": 9, "table": "records", "key": record.id, "op": "upsert", "row": row}
        self.assertEqual([], sync.without_parents(self.store, [change]))
        sync.apply_pulled(self.store, [change], cursor=9)
        self.assertEqual(9, sync.cursor(self.store))

    def test_a_deep_json_value_is_a_sync_error(self):
        with self.assertRaises(sync.SyncError):
            sync.check_row(sync.table_for("records"), "k-one", {"subjects": "[" * 2000 + "]" * 2000})


class TranscriptResumeTests(AuditTestCase):
    def test_catch_up_restores_an_existing_within_record_cursor(self):
        from knowitall2.learning import catchup

        old = [self.record("user", "Retain the earlier service version details."),
               self.record("assistant", [{"type": "text", "text": "Earlier service details."}])]
        records = old + [self.call("first"), self.call("second")]
        records.append(self.record("user", [self.result("first", "FIRST_RESULT")["message"]["content"][0],
                                             self.result("second", "SECOND_RESULT")["message"]["content"][0]]))
        path = self.log(records)
        session, _ = read_claude_code_session(path)
        record_start = sum(len(line) for line in path.read_bytes().splitlines(keepends=True)[:-1])
        boundary = session.events[1].end
        state = LearnerState()
        state.entry(path).update(offset=record_start, event_index=1, status="partial", baseline_to=boundary)
        state.save()
        with mock.patch.object(catchup, "session_logs", return_value=[path]):
            self.assertEqual(1, catchup.catch_up(str(self.root)))
        texts = []
        engine = mock.Mock()
        engine.last_usage = None
        engine.extract.side_effect = lambda dossier: texts.append(dossier.text) or []
        settings = LearnerSettings(enabled=True, max_calls_per_run=1, max_calls_per_day=50, settle_minutes=0)
        state = LearnerState()
        self.assertEqual(0, state.entry(path)["event_index"])
        learn(logs=[path], state=state, settings=settings, memory_factory=lambda: self.memory,
              extractor=engine, dry_run=False, right_away=True, quick=True)
        self.assertEqual((record_start, 1), (state.entry(path)["offset"], state.entry(path)["event_index"]))
        learn(logs=[path], state=LearnerState(), settings=settings, memory_factory=lambda: self.memory,
              extractor=engine, dry_run=False, right_away=True, quick=True)
        self.assertIn("Earlier service details", texts[0])
        self.assertNotIn("FIRST_RESULT", "\n".join(texts))
        self.assertIn("SECOND_RESULT", texts[1])

    def test_final_pass_slices_resume_sibling_results_independently_of_the_quick_pass(self):
        from datetime import datetime, timedelta, timezone
        from knowitall2.learning.learner import SETTLED, SETTLED_EVENT_INDEX
        from test_learning import FakeEngine

        records = [self.record("user", "Inspect every service version and retain useful information.")]
        calls = [{"type": "tool_use", "id": f"final-{i}", "name": "Bash",
                  "input": {"command": "service --version"}} for i in range(160)]
        results = [{"type": "tool_result", "tool_use_id": f"final-{i}",
                    "content": f"FINAL_RESULT_{i:03d} " + "detail " * 250} for i in range(160)]
        path = self.log(records + [self.record("assistant", calls), self.record("user", results)])
        state = LearnerState(self.root / "final-state.json")
        state.entry(path).update(offset=path.stat().st_size, event_index=0, settled=0)
        settings = LearnerSettings(enabled=True, max_calls_per_run=2, max_calls_per_day=2, settle_minutes=0)
        engine = FakeEngine()
        today = datetime.now(timezone.utc)
        first = learn(logs=[path], state=state, settings=settings, memory_factory=lambda: self.memory,
                      extractor=engine, dry_run=False, right_away=True, now=today)
        self.assertIsNone(first.blocked)
        self.assertLess(state.entry(path)[SETTLED], path.stat().st_size)
        self.assertGreater(state.entry(path)[SETTLED_EVENT_INDEX], 0)
        self.assertEqual(path.stat().st_size, state.entry(path)["offset"])
        second = learn(logs=[path], state=LearnerState(state.path), settings=settings,
                       memory_factory=lambda: self.memory, extractor=engine, dry_run=False,
                       right_away=True, now=today + timedelta(days=1))
        self.assertIsNone(second.blocked)
        self.assertEqual(path.stat().st_size, LearnerState(state.path).entry(path)[SETTLED])
        sent = "\n".join(text for step, text in engine.inputs if step == "final")
        for i in range(160):
            self.assertEqual(1, sent.count(f"FINAL_RESULT_{i:03d} "))

    def test_same_record_results_survive_bounded_chunks_restarts_and_a_failure(self):
        records = [self.record("user", "Inspect all service versions and retain useful details.")]
        calls = [{"type": "tool_use", "id": f"parallel-{i}", "name": "Bash",
                  "input": {"command": "service --version"}} for i in range(70)]
        results = [{"type": "tool_result", "tool_use_id": f"parallel-{i}",
                    "content": f"RESULT_{i:03d} " + "details " * 220} for i in range(70)]
        records += [self.record("assistant", calls), self.record("user", results)]
        path = self.log(records)
        session, _ = read_claude_code_session(path)
        dossiers = build_dossiers(session)
        self.assertGreater(len(dossiers), 2)
        self.assertTrue(all(len(dossier.text) <= 48_000 for dossier in dossiers))
        self.assertLess(dossiers[0].end_offset, path.stat().st_size)

        class Extractor:
            def __init__(self):
                self.texts = []
                self.fail_next = False

            def extract(self, dossier):
                if self.fail_next:
                    self.fail_next = False
                    raise ExtractionError("temporary extraction failure")
                self.texts.append(dossier.text)
                return []

        engine = Extractor()
        state_path = self.root / "sibling-state.json"
        settings = LearnerSettings(enabled=True, max_calls_per_run=1, max_calls_per_day=50, settle_minutes=0)
        for run in range(8):
            state = LearnerState(state_path)  # simulate a fresh process each time
            if run == 1:
                engine.fail_next = True
            report = learn(logs=[path], state=state, settings=settings, memory_factory=lambda: self.memory,
                           extractor=engine, dry_run=False, right_away=True, quick=True)
            self.assertIsNone(report.blocked)
            if state.entry(path)["status"] == "done":
                break
        self.assertEqual("done", state.entry(path)["status"])
        self.assertEqual((path.stat().st_size, 0), (state.entry(path)["offset"], state.entry(path)["event_index"]))
        text = "\n".join(engine.texts)
        for i in range(70):
            self.assertEqual(1, text.count(f"RESULT_{i:03d} "))
        self.assertNotIn("service --version", state_path.read_text(encoding="utf-8"))

    def record(self, kind, content):
        return {"type": kind, "cwd": str(self.root), "sessionId": "audit-resume", "message": {"content": content}}

    def log(self, records):
        path = self.root / "session.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        return path

    def call(self, ident, description="Inspecting the service."):
        return self.record("assistant", [{"type": "text", "text": description},
                                        {"type": "tool_use", "id": ident, "name": "Bash",
                                         "input": {"command": "service --version"}}])

    def result(self, ident, output):
        return self.record("user", [{"type": "tool_result", "tool_use_id": ident, "content": output}])

    def test_incremental_read_recovers_a_tool_call_before_the_offset(self):
        path = self.log([self.call("version")])
        _, offset = read_claude_code_session(path)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.result("version", "Service version is 3.2.1.")) + "\n")
        resumed, end = read_claude_code_session(path, start=offset)
        self.assertEqual([("Bash", "service --version", "Service version is 3.2.1.")],
                         [(event.tool, event.text, event.output) for event in resumed.events])
        self.assertEqual(path.stat().st_size, end)
        self.assertEqual(1, len(build_dossiers(resumed, require_user=False)))

    def test_completed_and_parallel_calls_are_not_replayed_or_mispaired(self):
        path = self.log([self.call("done"), self.result("done", "Already read."),
                         self.call("first"), self.call("second")])
        _, offset = read_claude_code_session(path)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.result("second", "Second result.")) + "\n")
            stream.write(json.dumps(self.result("first", "First result.")) + "\n")
        resumed, _ = read_claude_code_session(path, start=offset)
        self.assertEqual(["Second result.", "First result."], [event.output for event in resumed.events])

    def test_a_budget_checkpoint_never_discards_the_next_chunks_tool_result(self):
        records = [self.record("user", "Inspect the service version and retain useful information.")]
        records += [self.record("assistant", [{"type": "text", "text": ("Ordinary analysis " + str(i) + " ") * 100}])
                    for i in range(31)]
        records += [self.call("version"), self.result("version", "Service version is 3.2.1. " + "Detail " * 300)]
        path = self.log(records)

        class Extractor:
            def __init__(self):
                self.texts = []

            def extract(self, dossier):
                self.texts.append(dossier.text)
                return []

        engine = Extractor()
        state = LearnerState(self.root / "state.json")
        settings = LearnerSettings(enabled=True, max_calls_per_run=1, max_calls_per_day=50, settle_minutes=0)
        arguments = dict(logs=[path], state=state, settings=settings, memory_factory=lambda: self.memory,
                         extractor=engine, dry_run=False, right_away=True, quick=True)
        first = learn(**arguments)
        self.assertEqual((1, 1), (first.calls, first.deferred))
        self.assertNotIn("Service version is", engine.texts[0])
        second = learn(**arguments)
        self.assertEqual(1, second.calls)
        self.assertIn("Service version is 3.2.1.", engine.texts[1])
        self.assertNotIn("Ordinary analysis 0 ", engine.texts[1])
        self.assertEqual(("done", path.stat().st_size), (state.entry(path)["status"], state.entry(path)["offset"]))
