"""Tool rules: one standing rule for each quirk two chats learned, written from their lessons (0.11.1)."""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_commands import CommandTestCase, FakeEngine

from knowitall2 import commands, known
from knowitall2.learning import tool_rules
from knowitall2.learning.extractor import ExtractionError
from knowitall2.learning.state import LearnerState
from knowitall2.memory import Memory

HEREDOC_A = ("Python heredocs run through the Bash tool on Windows mangle backslashes in string literals; write the "
             "script with the Write tool instead.")
HEREDOC_B = "A heredoc in the Bash tool collapsed \\\\ into \\ in a node script; write the file with the Write tool."
HEREDOC_C = "Writing Python edit scripts via a Bash heredoc that contain f-strings with \\n breaks them."
PIPE_A = "In PowerShell, piping from a foreach statement gives 'An empty pipe element is not allowed'; assign it first."
PIPE_B = ("PowerShell refused `foreach ($r in $rows) { $r } | Format-Table` with 'An empty pipe element is not "
          "allowed'; use $out = foreach ... then pipe $out.")
INCIDENT = "govc failed at 10:42 because the vCenter password had expired; the user reset it."


class RuleTestCase(CommandTestCase):
    def heredoc_lessons(self) -> list[str]:
        return [self.lesson(HEREDOC_A, "python heredoc", session="chat-a"),
                self.lesson(HEREDOC_B, "python heredoc", session="chat-b", source="inferred"),
                self.lesson(HEREDOC_C, "python heredoc", session="chat-b", source="inferred")]

    def write(self, *answers, budget: int = 5, state=None):
        engine = FakeEngine(*answers)
        report = tool_rules.write_rules(self.store, engine, budget=budget, now="2026-10-14T12:00:00Z", state=state,
                                        memory=self.memory)
        return report, engine


class GroupTests(RuleTestCase):
    def test_a_quirk_two_chats_learned_one_lesson_checked_makes_a_group(self) -> None:
        heredoc = self.heredoc_lessons()
        self.lesson(INCIDENT, "govc", session="chat-a")                       # one chat: no rule from it
        for chat in ("chat-g", "chat-h"):                                       # one host's, not this computer's
            self.lesson(f"On the RMM host, git status in /rmm fails with dubious ownership ({chat}).", "git status",
                        where="/rmm", session=chat)
        self.lesson("Sleep blocked again.", "sleep", session="chat-c", source="inferred")
        self.lesson("Sleep N then a command is blocked.", "sleep", session="chat-d", source="inferred")   # none checked
        pipe = [self.lesson(PIPE_A, session="chat-e"), self.lesson(PIPE_B, session="chat-f")]   # untagged: by error
        found = {group.name: sorted(record.id for record in group.members) for group in tool_rules.groups(self.store)}
        self.assertEqual({"command `python heredoc`": sorted(heredoc),
                          'error "an empty pipe element is not allowed"': sorted(pipe)}, found)
        self.assertEqual(["command `python heredoc`", 'error "an empty pipe element is not allowed"'],
                         [group.name for group in tool_rules.waiting(self.store)])


class SessionTests(RuleTestCase):
    def test_lessons_saved_without_a_chat_count_no_chat(self) -> None:
        # Review 2026-10-14, E-M2: two lessons an agent saved by hand in one chat (no session) made a rule
        # "learned in 2 chats". Chats count as Store.said_in_sessions counts them: real sessions only.
        first = self.lesson("glab release create fails with 'tag name is already taken'; delete the tag first.",
                            "glab release")
        second = self.lesson("Retrying glab release create gave 'tag name is already taken'; the tag remained.",
                             "glab release")
        self.assertEqual([], tool_rules.groups(self.store))
        third = self.lesson("glab release create: 'tag name is already taken' once more.", "glab release",
                            session="chat-q", source="inferred")
        self.assertEqual([], tool_rules.groups(self.store))   # one real chat
        group = tool_rules.Group("command `glab release`", [self.store.get(item) for item in (first, second, third)],
                                 key="glab release")
        proposal = {"group": "G1", "shell": "any", "programs": ["glab release"], "pattern": "",
                    "error": "tag name is already taken", "advice": "Delete the remote tag before releasing again.",
                    "sources": [first, second, third]}
        self.assertIsNone(tool_rules.proposed_rule(group, proposal))


class WritingTests(RuleTestCase):
    def test_the_model_writes_one_rule_that_keeps_its_sources_and_names_them(self) -> None:
        heredoc = self.heredoc_lessons()
        pipe = [self.lesson(PIPE_A, session="chat-e"), self.lesson(PIPE_B, session="chat-f")]
        state = LearnerState(self.home / "learner" / "state.json")
        report, engine = self.write({"rules": [
            {"group": "G1", "shell": "bash", "programs": [], "pattern": "heredoc-escapes", "error": "",
             "advice": "Write the script with the Write tool and run the file, instead of a heredoc.",
             "sources": heredoc},
            {"group": "G2", "shell": "powershell", "programs": [], "pattern": "foreach-pipe",
             "error": "An empty pipe element is not allowed", "advice": "Assign the foreach output to a variable "
             "and pipe that.", "sources": [f"[{pipe[0]}]", pipe[1]]},
        ]}, state=state)
        self.assertEqual((1, 2, 2), (report.calls, report.groups, report.written))
        self.assertIn("chat c1, checked", engine.inputs[0])
        self.assertIn("G2: error \"an empty pipe element is not allowed\", 2 lessons from 2 chats", engine.inputs[0])
        self.assertEqual(["commands"], [call.get("origin") for call in LearnerState(state.path).calls])
        rule = self.store.get(report.rules[0])
        self.assertEqual(("lesson", "observed", "global"), (rule.kind, rule.verification, rule.scope))
        self.assertEqual(["tool-rule", "shell:bash", "pattern:heredoc-escapes", "chats:2"], list(rule.tags))
        self.assertTrue(all(record_id in rule.text for record_id in heredoc))
        self.assertIn("Learned in 2 chats", rule.text)
        pipe_rule = self.store.get(report.rules[1])
        self.assertIn("error:an empty pipe element is not allowed", pipe_rule.tags)
        # The lessons stay, and recall shows the rule with them.
        self.assertTrue(all(self.store.get(record_id).status == "active" for record_id in heredoc + pipe))
        found = self.memory.recall("heredoc Write tool")
        self.assertIn(rule.id, found)
        self.assertIn(heredoc[0], found)
        # The knowledge-file refresh puts both in the index the hook reads.
        known.refresh(self.store._connection)
        index = json.loads((self.home / commands.INDEX).read_text(encoding="utf-8"))
        self.assertEqual({rule.id, pipe_rule.id}, {item["id"] for item in index["rules"]})
        self.assertNotIn("Learned in", index["rules"][0]["text"])

    def test_the_gates_keep_out_what_two_chats_did_not_learn_and_what_the_lessons_do_not_say(self) -> None:
        heredoc = self.heredoc_lessons()
        good = {"group": "G1", "shell": "bash", "programs": [], "pattern": "heredoc-escapes", "error": "",
                "advice": "Write the script with the Write tool and run the file.", "sources": heredoc}
        for change in ({"sources": heredoc[1:]},                         # one chat, and none checked
                       {"sources": ["k-0123456789", heredoc[0]]},        # an id from no lesson of the group
                       {"pattern": "", "programs": ["git"]},            # a general program alone
                       {"pattern": "made-up", "programs": []},
                       {"advice": "Use it."},
                       {"group": "G9"}):
            with self.subTest(change=change):
                report, _ = self.write({"rules": [{**good, **change}]})
                self.assertEqual(0, report.written)
                self.store._connection.execute("DELETE FROM reviews")
        # An error text no lesson quotes is dropped; the rule stands on its pattern.
        report, _ = self.write({"rules": [{**good, "error": "SyntaxError: unterminated string literal detected"}]})
        self.assertEqual(1, report.written)
        self.assertEqual("", commands.rule_fields(self.store.get(report.rules[0]).tags)["error"])

    def test_a_group_is_asked_about_again_only_when_its_lessons_change_with_its_rules_shown(self) -> None:
        heredoc = self.heredoc_lessons()
        answer = {"rules": [{"group": "G1", "shell": "bash", "programs": [], "pattern": "heredoc-escapes",
                             "error": "", "advice": "Write the script with the Write tool and run the file.",
                             "sources": heredoc[:2]}]}
        report, _ = self.write(answer)
        self.assertEqual(1, report.written)
        self.assertEqual([], tool_rules.waiting(self.store))
        self.lesson("A cat heredoc in the Bash tool lost the backslashes of a regex.", "python heredoc",
                    session="chat-c")
        report, engine = self.write(answer)   # the model says the same rule again: kept once
        self.assertEqual((1, 0), (report.calls, report.written))
        self.assertIn("Rules already written for this group:", engine.inputs[0])
        self.assertEqual(1, len(tool_rules.existing_rules(self.store)))

    def test_a_group_reviewed_again_replaces_its_rule_instead_of_adding_one(self) -> None:
        # Review 2026-10-14, E-M1: a reworded rule was saved beside the first, and both were given at once.
        heredoc = self.heredoc_lessons()
        first, _ = self.write({"rules": [{"group": "G1", "shell": "bash", "programs": [], "pattern": "heredoc-escapes",
                                          "error": "", "advice": "Write the script with the Write tool, not a heredoc.",
                                          "sources": heredoc}]})
        more = self.lesson("Heredoc again ate the backslashes in a regex; use the Write tool.", "python heredoc",
                           session="chat-z")
        second, engine = self.write({"rules": [{"group": "G1", "shell": "bash", "programs": ["python heredoc"],
                                                "pattern": "", "error": "",
                                                "advice": "Save code to a file first instead of a heredoc.",
                                                "sources": [heredoc[0], more]}]})
        self.assertIn("replaces", engine.inputs[0] + tool_rules.SYSTEM_PROMPT)
        self.assertEqual(second.rules, [record.id for record in tool_rules.existing_rules(self.store)])
        old = self.store.get(first.rules[0])
        self.assertEqual(("superseded", second.rules[0]), (old.status, old.superseded_by))
        new = self.store.get(second.rules[0])
        self.assertTrue(all(record_id in new.text for record_id in [*heredoc, more]))   # its sources kept
        # A rule the model says is about another quirk of the group stands beside it.
        self.lesson("One more heredoc lesson.", "python heredoc", session="chat-y")
        self.write({"rules": [{"group": "G1", "shell": "bash", "programs": [], "pattern": "sleep-chain", "error": "",
                               "advice": "Another quirk of heredocs, kept beside it.", "sources": heredoc[:2],
                               "replaces": "none"}]})
        self.assertEqual(2, len(tool_rules.existing_rules(self.store)))

    def test_a_rule_whose_lessons_are_gone_is_retired(self) -> None:
        # Review 2026-10-14, E-M1: every source forgotten, and the rule stayed active and indexed.
        heredoc = self.heredoc_lessons()
        report, _ = self.write({"rules": [{"group": "G1", "shell": "bash", "programs": [],
                                           "pattern": "heredoc-escapes", "error": "",
                                           "advice": "Write the script with the Write tool.", "sources": heredoc}]})
        for record_id in heredoc[1:]:
            self.memory.forget(record_id, reason="found wrong")
        known.refresh(self.store._connection)
        index = json.loads((self.home / commands.INDEX).read_text(encoding="utf-8"))
        self.assertEqual([], index["rules"])   # one chat left: not given, even before upkeep retires it
        with mock.patch("sys.stdout"):
            from knowitall2.learning.command import write_waiting_rules

            write_waiting_rules(self.store, FakeEngine(), state=LearnerState(self.home / "s.json"), run="r-1",
                                budget=0)
        rule = self.store.get(report.rules[0])
        self.assertEqual("retired", rule.status)
        self.assertIn("lessons", rule.retired_reason)

    def test_a_rule_the_store_refuses_is_logged_and_its_group_tried_again(self) -> None:
        # Review 2026-10-14, E-L8: a refused save was dropped silently and its group marked reviewed.
        heredoc = self.heredoc_lessons()
        long_program = "x" * 90
        report, _ = self.write({"rules": [{"group": "G1", "shell": "bash", "programs": [long_program],
                                           "pattern": "heredoc-escapes", "error": "",
                                           "advice": "Write the script with the Write tool.", "sources": heredoc}]})
        self.assertEqual(1, report.written)   # the program too long for a tag is left out, not the rule
        self.assertTrue(all(len(tag) <= 80 for tag in self.store.get(report.rules[0]).tags))
        self.store._connection.execute("DELETE FROM reviews")
        with mock.patch.object(tool_rules.Memory, "remember", side_effect=tool_rules.MemoryInputError("a secret")):
            report, _ = self.write({"rules": [{"group": "G1", "shell": "bash", "programs": ["python heredoc"],
                                               "pattern": "", "error": "",
                                               "advice": "Use the Write tool for scripts.", "sources": heredoc}]})
        self.assertEqual((0, 1), (report.written, report.refused))
        self.assertEqual(1, len(tool_rules.waiting(self.store)))   # tried again
        from knowitall2 import journal

        self.assertTrue(any("a secret" in str(item.get("message")) for item in journal.read_problems()))

    def test_the_budget_and_a_failing_engine_leave_groups_for_later(self) -> None:
        self.heredoc_lessons()
        self.lesson(PIPE_A, session="chat-e")
        self.lesson(PIPE_B, session="chat-f")
        with mock.patch.object(tool_rules, "GROUPS_PER_CALL", 1):
            report, _ = self.write({"rules": []}, budget=1)
            self.assertEqual((1, 1), (report.calls, report.deferred))
            report, _ = self.write(ExtractionError("signed out", blocking=True))
            self.assertEqual((0, "signed out"), (report.calls, report.blocked))
            self.assertEqual(1, len(tool_rules.waiting(self.store)))
            refusal = ExtractionError("the model refused")
            report, _ = self.write(refusal)
            self.assertEqual((1, 1), (report.calls, report.refused))
            self.assertEqual(1, len(tool_rules.waiting(self.store)))   # tried once more
            self.write(refusal)
        self.assertEqual([], tool_rules.waiting(self.store))           # refused twice: left out

    def test_maintenance_never_reviews_a_rule_with_the_lessons_it_came_from(self) -> None:
        from knowitall2.learning.maintenance import build_groups

        heredoc = self.heredoc_lessons()
        report, _ = self.write({"rules": [{"group": "G1", "shell": "bash", "programs": [],
                                           "pattern": "heredoc-escapes", "error": "",
                                           "advice": "Write the script with the Write tool.", "sources": heredoc}]})
        members = {record.id for group in build_groups(self.memory) for record in group.members}
        self.assertTrue(set(heredoc) <= members)
        self.assertNotIn(report.rules[0], members)


class LifecycleTests(RuleTestCase):
    """Review 2026-10-14 (b): a rule lasts while its lessons stand, wherever maintenance moved them."""

    def setUp(self) -> None:
        super().setUp()
        self.sources = self.heredoc_lessons()          # chat-a (checked), chat-b, chat-b
        report, _ = self.write({"rules": [{"group": "G1", "shell": "bash", "programs": [],
                                           "pattern": "heredoc-escapes", "error": "",
                                           "advice": "Write the script with the Write tool, not a heredoc.",
                                           "sources": self.sources, "replaces": ""}]})
        self.rule = report.rules[0]

    def upkeep(self) -> None:
        from knowitall2.learning.command import write_waiting_rules

        with mock.patch("sys.stdout"):
            write_waiting_rules(self.store, FakeEngine(), state=LearnerState(self.home / "s.json"), run="r", budget=0)

    def indexed(self) -> list[str]:
        known.refresh(self.store._connection, force=True)
        return [rule["id"] for rule in json.loads((self.home / commands.INDEX).read_text(encoding="utf-8"))["rules"]]

    def test_a_rule_whose_lessons_maintenance_merged_as_duplicates_stays(self) -> None:
        # F-H1: chat-b's lessons merged into chat-a's still count as chat-b's.
        from knowitall2.learning.maintenance import REASON_PREFIX

        a, b, c = self.sources
        for item in (b, c):
            self.store.supersede(item, a, now=self.memory.now(), reason=f"{REASON_PREFIX}duplicate of [{a}]")
        self.upkeep()
        self.assertEqual("active", self.store.get(self.rule).status)
        self.assertEqual([self.rule], self.indexed())
        self.assertEqual(["command `python heredoc`"], [group.name for group in tool_rules.groups(self.store)])

    def test_a_rule_whose_lessons_maintenance_combined_stays(self) -> None:
        # F-H1: a combined memory has no session of its own; it carries the chats of what it holds.
        from knowitall2.learning.maintenance import REASON_PREFIX

        holder = self.lesson("Heredocs in the Bash tool mangle backslashes and quotes in scripts; write the file "
                             "with the Write tool instead.", "python heredoc")
        for item in self.sources:
            self.store.supersede(item, holder, now=self.memory.now(), reason=f"{REASON_PREFIX}combined into [{holder}]")
        self.upkeep()
        self.assertEqual("active", self.store.get(self.rule).status)
        self.assertEqual([self.rule], self.indexed())
        [group] = tool_rules.groups(self.store)
        self.assertEqual(2, group.chats)

    def test_a_disputed_lesson_holds_its_rule_back_and_never_retires_it(self) -> None:
        # F-M1: while chat-a's lesson is disputed the rule is not given; settled, it is given again.
        from knowitall2.memory import DISPUTED_TAG

        a = self.sources[0]
        record = self.store.get(a)
        self.store.set_tags(a, [*record.tags, DISPUTED_TAG], now=self.memory.now())
        self.upkeep()
        self.assertEqual("active", self.store.get(self.rule).status)
        self.assertEqual([], self.indexed())
        self.store.set_tags(a, record.tags, now=self.memory.now())
        self.assertEqual([self.rule], self.indexed())

    def test_a_rule_merged_away_by_an_older_computer_brings_its_group_back(self) -> None:
        # F-M2: a 0.11.0 computer's maintenance keeps rules in its groups and may merge one into a lesson.
        self.assertEqual([], tool_rules.waiting(self.store))
        self.clock.value = "2026-10-15T08:00:00Z"
        maintenance = Memory(self.store, agent="maintenance", clock=self.clock)
        maintenance.store.supersede(self.rule, self.sources[0], now=maintenance.now(),
                                    reason=f"maintenance: duplicate of [{self.sources[0]}]")
        self.assertEqual(["command `python heredoc`"], [group.name for group in tool_rules.waiting(self.store)])
        # Looked at again, it is not due again.
        tool_rules.write_rules(self.store, FakeEngine({"rules": []}), budget=1, now="2026-10-16T00:00:00Z",
                               memory=self.memory)
        self.assertEqual([], tool_rules.waiting(self.store))

    def test_a_rule_the_user_restored_stays_and_a_rule_the_user_corrected_stays_a_rule(self) -> None:
        # F-L4.
        for item in self.sources[1:]:
            self.memory.forget(item, reason="found wrong")
        self.upkeep()
        self.assertEqual("retired", self.store.get(self.rule).status)
        self.memory.restore(self.rule)
        self.upkeep()
        self.assertEqual(("active", "user_stated"), (self.store.get(self.rule).status,
                                                     self.store.get(self.rule).verification))
        self.assertEqual([self.rule], self.indexed())
        result = self.memory.correct(self.rule, "Write scripts with the Write tool; never pass them through a heredoc.")
        corrected = result.record
        self.assertEqual((commands.RULES_AGENT, "user_stated"), (corrected.source_agent, corrected.verification))
        self.assertIn(commands.RULE_TAG, corrected.tags)
        self.assertIn("pattern:heredoc-escapes", corrected.tags)
        self.assertEqual([corrected.id], self.indexed())
        self.upkeep()
        self.assertEqual("active", self.store.get(corrected.id).status)

    def test_a_rule_is_saved_with_its_tags_in_one_write(self) -> None:
        # F-L5: a stop between two writes left the rule's text as an ordinary checked lesson.
        from knowitall2.store import Store

        with mock.patch.object(Store, "set_tags", side_effect=RuntimeError("stopped")):
            saved = tool_rules.save_rule(self.memory, advice="Quote paths with spaces in scp", shell="any",
                                         programs=["scp"], pattern="", error="",
                                         sources=[self.store.get(item) for item in self.sources])
        self.assertIn(commands.RULE_TAG, saved.tags)
        self.assertEqual(commands.RULES_AGENT, saved.source_agent)


class OneTimeTests(RuleTestCase):
    def run_cli(self, *arguments: str) -> tuple[int, str]:
        from knowitall2.cli import main

        output = io.StringIO()
        # No background rewrite of knowledge files, which would hold the data home open past the test.
        with redirect_stdout(output), redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {known.ENVIRONMENT_SWITCH: "0"}):
            code = main(list(arguments))
        return code, output.getvalue()

    def test_catalog_rules_writes_rules_for_the_lessons_already_learned_and_a_dry_run_counts_them(self) -> None:
        heredoc = self.heredoc_lessons()
        code, output = self.run_cli("catalog", "--rules", "--dry-run")
        self.assertEqual(0, code)
        self.assertIn("Tool rules: 1 groups of lessons learned in several chats wait for their rules, about 1 model "
                      "calls. Dry run", output)
        from knowitall2.learning.state import LearnerSettings

        engine = FakeEngine({"rules": [{"group": "G1", "shell": "bash", "programs": [], "pattern": "heredoc-escapes",
                                        "error": "", "advice": "Write the script with the Write tool.",
                                        "sources": heredoc}]})
        with mock.patch("knowitall2.learning.command.load_settings",
                        return_value=LearnerSettings(enabled=True)), \
                mock.patch("knowitall2.learning.command.build_engine", return_value=engine):
            code, output = self.run_cli("catalog", "--rules", "2")
        self.assertEqual(0, code)
        self.assertIn("wrote 1 rules", output)
        self.assertEqual(["user"], [call.get("origin") for call in LearnerState().calls])


class BriefingTests(RuleTestCase):
    def test_the_briefing_has_three_tool_rules_for_the_agents_shells_most_recurrent_first(self) -> None:
        # Study 2026-10-13, D3: three lines answered 13 of 33 held moments before the first try; two of them were
        # about Claude Code's Bash tool, which Codex does not have.
        from knowitall2.memory import Memory

        bash = self.rule("Write scripts with the Write tool, not a heredoc", shell="bash", pattern="heredoc-escapes",
                         chats=34)
        sleep = self.rule("Wait with an until-loop, not sleep then a command", shell="bash", programs=["sleep"],
                          chats=11)
        pipe = self.rule("Assign a foreach statement's output before piping it", shell="powershell",
                         pattern="foreach-pipe", chats=5)
        codex = self.rule("Run one PowerShell statement per exec call", shell="codex", programs=["exec"], chats=3)
        rg = self.rule("Pass globs to rg with -g", shell="any", programs=["rg"], chats=2)
        self.rule("Never mind this guess", shell="any", programs=["rg"], chats=50, source="inferred")
        with mock.patch.object(commands, "WINDOWS", True):
            for agent, expected in (("claude-code", [bash, sleep, pipe]), ("codex", [pipe, codex, rg])):
                with self.subTest(agent=agent):
                    text = Memory(self.store, agent=agent, clock=self.clock).briefing(project_path=self.alpha)
                    self.assertIn("Tool rules (how commands behave on this computer", text)
                    shown = [record_id for record_id in (bash, sleep, pipe, codex, rg) if f"[{record_id}]" in text]
                    self.assertEqual(expected, sorted(shown, key=text.index))
                    self.assertEqual(1, text.count(f"[{expected[0]}]"))   # not again in the table of contents
                    self.assertNotIn("Learned in", text)
        with mock.patch.object(commands, "WINDOWS", False):
            text = Memory(self.store, agent="claude-code", clock=self.clock).briefing(project_path=self.alpha)
            self.assertNotIn(f"[{pipe}]", text)   # no PowerShell tool off Windows


class InstructionTests(unittest.TestCase):
    def test_the_server_instructions_fit_what_claude_code_shows(self) -> None:
        # 2026-10-14: the 0.11.0 instructions (about 2,450 characters) lost their end in a live Claude Code session.
        from knowitall2.mcp_server import INSTRUCTIONS

        self.assertLess(len(INSTRUCTIONS), 2000)
        self.assertTrue(INSTRUCTIONS.endswith("If KnowItAll2 is unavailable, continue normally."))

    def test_the_instructions_name_tool_behavior_as_something_to_recall(self) -> None:
        # Study 2026-10-13, H5: none of 151 recalls asked how a shell or a tool behaves.
        from knowitall2.agents import skill
        from knowitall2.mcp_server import INSTRUCTIONS

        for text in (INSTRUCTIONS, skill.SKILL_MARKDOWN):
            with self.subTest(text=text[:40]):
                self.assertIn("a tool's behavior or an error message", " ".join(text.split()))


if __name__ == "__main__":
    unittest.main()
