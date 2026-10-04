# KnowItAll2

An always-on memory for coding agents. KnowItAll2 remembers what your agents
learn about your projects, systems, and preferences, learns more as you work,
and gives Codex, Claude Code, and other MCP-capable agents the right context
when they need it, without you having to manage it.

> **Status:** pre-release, version 0.8.4. Source-available: free to install
> and use on your own computers; see [`NOTICE`](NOTICE). Testers: start with [`docs/testing.md`](docs/testing.md).

## Installing

Ask your coding agent to install it for you:

> Install KnowItAll2 from `<this repository's address>` by following
> `docs/install-for-agents.md`.

The agent checks the prerequisites, asks which agents to set up, and runs
KnowItAll2's own `setup` and `doctor` commands.

### Sharing memory between computers

Memory stays on your computer unless you choose otherwise. If you use agents
on more than one computer, you can run a KnowItAll2 server in Docker and
connect each agent to it with a one-time code from the server's page; see
[`docs/server.md`](docs/server.md). Each computer keeps its own copy, so
KnowItAll2 keeps working when the server is down.

## What works today

- A local memory store with full-text search.
- An MCP server (`knowitall2 serve`) with eight tools:
  - `briefing`: the user's rules, the project's key points, and a table of
    contents: the headlines of everything else known for the project and the
    systems it uses, by system (current-state notes left out);
  - `recall`: keyword search across everything remembered, ranked by how much
    of the search a memory covers (rare words count more), with partial
    matches marked as such;
  - `remember`: save one durable fact, procedure, decision, lesson, or rule;
  - `forget`: retire a memory that is wrong or no longer true (for something
    the user stated, KnowItAll2 asks the user instead);
  - `questions` and `answer`: the rare questions only the user can settle,
    in plain words, and the user's choices;
  - `settle`: an agent's report on an optional KnowItAll2 request, a quick
    look-only check of which of two memories is right, or something missing
    to find out;
  - `learn`: ask KnowItAll2 to learn from the current session now, such as
    when a task is finished.
- A `knowitall2` command with the same operations, plus `stats`, which reports
  how useful the memory has been, and `move`, which files memories under the
  project they are about.
- The KnowItAll2 app, installed by `setup` in the Start menu or the
  applications menu (or `knowitall2 app`): a local window, written for people
  who are not developers, with technical detail on request.
  - Overview: its health and use.
  - What it knows: every system and project, by area, with a profile of what
    is known (what it is, where, how agents reach it, where the sign-in is
    kept, what agents can do, how-tos, rules, decisions, lessons), a status
    (ready to use, partly known, only mentioned), what is missing, "Tell
    KnowItAll2", and "Ask an agent to find these out": one agent, started
    right away in the system's project folder, reads files only (itself, so
    nothing is redacted first, and also while learning is off) and keeps an
    answer, as unverified, only if its quote is in the file it names and
    says what the answer says; what it cannot find waits for the agents
    that work with the system later.
  - Memories: each one as a plain one-line summary, with the exact wording on
    request; search, filters by system, confirm, correct, forget, restore.
  - Questions: only what is still unclear or is the user's to decide, in
    plain words, with a "Not sure" choice.
  - Learning: what it just learned (and what it is learning right now), each
    run and every idea it considered, settings, Learn now, "Learn anyway" for
    a session that waited for the daily limit, and catching up on past
    sessions; and Activity.
  - It runs only while its window is open, reads the live database, needs
    nothing installed, and is reachable only from this computer.
- A background catalog, after learning: each memory gets a plain one-line
  summary and is filed under the system it is about; each system gets a
  plain summary and a list of what is missing. `knowitall2 catalog` runs it
  by hand (`--dry-run` shows what it would do).
- Questions are settled by whoever can: KnowItAll2 itself when the memories
  make the answer clear, then the next relevant agent (an optional,
  look-only check offered in its briefing or search results), and only then
  the user. Nothing but the user's own word changes the user's statements.
- An activity journal of everything KnowItAll2 does: briefings, recalls,
  saves, removals, questions and answers, each learning run with every
  candidate memory and why it was kept or rejected, maintenance changes, and
  problems. `knowitall2 activity` shows it (`--problems` for failures); entries
  are kept for 90 days, redacted like everything else.
- `knowitall2 setup codex|claude-code`, `doctor`, and `uninstall`. These change
  only settings that KnowItAll2 owns and keep the rest of each file, with its
  line endings and indentation. Uninstalling removes what setup added; it can
  leave an empty settings section behind, or change the blank lines at the end
  of a file.
- Project identity from Git, so moved and cloned copies share their memories.
- Secret screening: KnowItAll2 stores where a credential is kept, never the
  credential itself.
- Learning from Claude Code and Codex session logs:
  - `knowitall2 learn --dry-run` and `--show <session>` show exactly what would
    be sent, without calling a model;
  - `--enable` records your consent. Learning calls run on the Claude Code or
    the Codex engine already on your computer, on your existing login, sealed
    from your tools and settings; `--backend claude-cli|codex-cli` chooses;
  - `--start-from-now <agent>` treats an agent's existing sessions as already
    read, for history that is already captured elsewhere, and
    `--catch-up [FOLDER]` (`--since`, `--estimate`) learns from such sessions
    after all, by project folder;
  - a plain `knowitall2 learn` run extracts labeled memories. Each call also
    sees the related memories already known, so repeats are skipped and
    updates replace older memories. Weaker evidence never overwrites a stronger
    memory, and never your own statements: KnowItAll2 asks you instead.
- Ranking that prefers recently confirmed memories; your own statements never
  fade.
- Background maintenance after each learning run:
  - reviews groups of related memories that changed since their last review;
  - merges duplicates, replaces outdated memories, and retires temporary
    status, under the same evidence rules as learning;
  - never rewrites or deletes: `knowitall2 history` shows every removal and
    `knowitall2 restore <id>` brings one back;
  - `knowitall2 maintain --dry-run` shows what would be reviewed.
- `knowitall2 learn --documents [PROJECT]` learns from what projects keep in
  writing: state files, summaries, handoffs, and Claude Code's own notes, the
  most useful first, about two or three calls a project; unchanged
  documents are not read again. `--estimate` shows the calls first. What a
  document says is kept as unverified and belongs to its project.
- `knowitall2 import <file>` brings in memories from another system, in the
  JSON Lines format described in [`docs/import-format.md`](docs/import-format.md);
  `--dry-run` shows each line's outcome first.
- Learning at the moments work happens, when learning is on:
  - when a turn ends with a commit in it (a shell command that ran
    `git commit`, cherry-pick, or revert and did not fail), when a Claude
    Code session ends, or when an agent calls the `learn` tool because the
    user asked or a task finished;
  - it learns that session right away, with no daily limit (the limit holds
    back only background learning), and files what it learns under the
    project the work was in, which may not be the chat's folder;
  - it learns even while the session is still active, in a
    separate process, so the agent never waits;
  - the user is told in the session itself (a message the agent does not
    read, so it costs no tokens) what was learned, turned down, or already
    known, or why nothing could be learned yet; news of a session that
    ended is shown by the next session in the same folder;
  - sessions that ended without any of those are still learned a while
    after they go quiet.
- A short section in each agent's global instructions (Claude Code's
  `CLAUDE.md`, Codex's `AGENTS.md`), installed by `setup` and removed by
  `uninstall`: agents read the briefing when they orient themselves, and
  look things up before working them out again.
- Session hooks for Claude Code and for Codex, installed by `setup`:
  - at session start: the briefing, news of learning, and the regular pass
    over finished sessions, detached and throttled;
  - when the user sends a message: the headlines of what KnowItAll2 learned
    since the chat started (at most three), and, when the chat starts
    working in another project, that project's table of contents, once;
  - at the end of each turn: learning after a commit, and news of learning;
  - at the end of a Claude Code session (Codex has no such hook): learning
    the rest of the session;
  - never delay or block a session;
  - in Codex, run once you trust them with `/hooks`.

## Requirements

- Windows 10 or 11, or Linux.
- Python 3.12 or later with SQLite FTS5, which python.org builds and current
  Linux distributions include.

## Development

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests
python -m knowitall2 --version
```

On Linux, the same commands run with `export PYTHONPATH=src` and `python3`.

Data lives in `~/.knowitall2` (`%USERPROFILE%\.knowitall2` on Windows) unless
`KNOWITALL2_HOME` is set.

## License

Source-available, not open source: you may install and use KnowItAll2 free
of charge on your own computers, under the terms in [`NOTICE`](NOTICE).
