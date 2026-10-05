# Testing KnowItAll2

Thank you for testing KnowItAll2. This guide covers what it does, what to
try, what data leaves your computer, and how to report problems.

KnowItAll2 is a pre-release, free to install and use on your own computers;
see `NOTICE`.

## What it does

KnowItAll2 gives your coding agents (Claude Code and Codex) a shared,
long-term memory:

- **At the start of each session,** the agent gets a short briefing: your
  rules, the project's key points, and a list of everything else known for
  the project and the systems it uses, so it knows what to look up. In a
  chat that stays open, it hears about what KnowItAll2 learns later, and
  about another project when the chat starts working there.
- **While working,** the agent can `recall` what was learned before, such as a
  host, a procedure, a past decision, or where a credential is kept, and can
  `remember` new durable facts.
- **If you turn learning on,** KnowItAll2 also learns as you work: when a
  turn ends with a commit, when a Claude Code session ends, or when you ask
  your agent to learn, right away and filed under the project the work was
  in. It tells you in the session what it learned, and it
  keeps its memory tidy on its own. Sessions that ended without any of those
  are learned a while after they go quiet.

## Before you start

- Windows 10 or 11, or Linux, with Python 3.12 or later and Git.
- Claude Code, Codex, or both. On Codex, KnowItAll2's tools are called
  through Codex's code mode, so install Codex's full package: its npm package,
  or the `codex-package` release archive on Linux.
- Access to the release repository, and a way for Git to sign in to it (a
  credential manager or a personal access token).

## Installing

Ask your coding agent:

> Install KnowItAll2 from `<this repository's address>` by following
> `docs/install-for-agents.md`.

The agent checks the prerequisites, clones KnowItAll2 into `.knowitall2/app`
in your home folder, and sets it up for the agents you choose. Afterwards:

- quit and reopen Claude Code;
- in Codex, run `/hooks` once and trust the KnowItAll2 SessionStart, Stop,
  and UserPromptSubmit hooks.

## What leaves your computer

- **Your memories** stay in `.knowitall2` in your home folder. Nothing is
  uploaded, unless you connect KnowItAll2 to a server you run
  (`docs/server.md`), which then keeps the shared copy.
- **Learning is off until you turn it on.** When it is on, each learning call
  sends a redacted excerpt of one finished session to a model, through the
  Claude Code or Codex engine on your computer and on your own account. The
  excerpt holds your messages, the agent's replies, and commands with their
  output. Passwords, tokens, and keys are redacted before sending.
  - `knowitall2 learn --dry-run` lists what would be sent.
  - `knowitall2 learn --show <session>` prints it exactly.
- **"Ask an agent to find these out"** in the app is its own consent: it
  runs only when you press it, and also while learning is off. It starts one
  agent on the same engine and account in the system's project folder, and
  that agent reads the files itself, so nothing it reads is redacted first.
  It changes nothing and is asked to stay in that folder. On Claude Code
  2.1 or later it cannot read outside that folder (restricted mode); on an
  older Claude Code, or on Codex, whose read-only sandbox lets it read
  files anywhere on the computer, it can. What it finds is kept as
  unverified.
- Learning from your work (after a commit, at a session's end, or when you ask)
  happens right away, with no daily limit. Background learning (sessions
  that went quiet, catching up) uses at most 10 calls per run and 60 per day
  by default, counting the calls your work used.

## What to try

1. Start a session in one of your projects. Ask the agent what KnowItAll2
   told it at the start.
2. Tell the agent something durable, for example where a service runs or
   where a credential is kept, and ask it to remember it. Then, in another
   project or a new session, ask about it.
3. Tell it one of your rules in your own words, for example "always run the
   tests before committing". Check that later sessions start with it.
4. Optionally, preview learning with `knowitall2 learn --dry-run`, then turn
   it on with `knowitall2 learn --enable`, or in the app's Learning screen.
   Then commit something, or ask the agent to have KnowItAll2 learn from the
   session. When the turn ends, the session says what KnowItAll2 is learning;
   when your next turn ends, it says what was learned.
5. Open the KnowItAll2 app from its shortcut, which the install added to
   the Start menu or applications menu (or, on Windows, to the desktop
   until you first open it), or with `knowitall2 app`. It shows:
   - **Overview:** whether KnowItAll2 is working, and how much your agents
     used it today and this week;
   - **What it knows:** every system and project, what is known about it,
     what is missing, and ways to fill the gaps: tell KnowItAll2 yourself,
     or press "Ask an agent to find these out" and watch one agent look in
     the project's files (it changes nothing, and takes a minute or two);
   - **Memories:** each thing it knows in one sentence, with the exact
     wording on request; confirm, correct, forget, and restore;
   - **Questions:** only what is still unclear or yours to decide;
   - **Learning:** what it just learned, each run, every idea it considered
     and why it was or was not kept, settings, Learn now, and catching up on
     past sessions;
   - **Activity:** everything it did, newest first, including problems.

   The app runs only while its window is open and is reachable only from
   your own computer. In a terminal, `knowitall2 stats` and
   `knowitall2 activity` show the same information.

There is no `knowitall2` program on your PATH: run each `knowitall2` command
as one line that sets `PYTHONPATH` and starts Python, as in the install
guide, for example:

```bash
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 stats
```

On Windows (PowerShell):

```powershell
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 stats
```

`doctor` checks the installation and each agent you set up (add
`--agent codex` or `--agent claude-code` for one only). Each problem line
starts with `FAIL` and is followed by a `fix:` line with the command to run.
Until you trust the Codex hooks with `/hooks`, its Codex hook line is `OK`
and reminds you to trust them.

## Reporting problems

Open an issue in the repository you installed from. Include:

- what you did, what you expected, and what happened;
- the output of `knowitall2 doctor`, and of `knowitall2 activity --problems`
  (run as shown above);
- your operating system, and your Claude Code or Codex version.

Do not paste secrets, or session content you do not want to share.

## Updating

When a new version is announced, tell your coding agent "update KnowItAll2".
It runs KnowItAll2's update script, which downloads the new version,
refreshes every agent's setup, and tells you what changed and anything you
need to do, such as starting new sessions.

The first time, from version 0.2.0, your agent does not know about the
update script yet. Say instead: "update KnowItAll2 by following the Updating
section of docs/install-for-agents.md in ~/.knowitall2/app".

You can also run the script yourself: `python3 ~/.knowitall2/update.py` on
Linux, or `python "$env:USERPROFILE\.knowitall2\update.py"` in PowerShell.

## Uninstalling

Run, on Linux:

```bash
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 uninstall claude-code
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 uninstall codex
```

On Windows (PowerShell):

```powershell
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 uninstall claude-code
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 uninstall codex
```

Your memories are kept in `.knowitall2`; delete that folder to remove
everything.
