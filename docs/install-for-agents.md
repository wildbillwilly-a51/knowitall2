# Installing KnowItAll2: instructions for coding agents

You are a coding agent, such as Codex or Claude Code, and the user has asked you
to install KnowItAll2. Follow these steps in order.

This pre-release version runs from a clone of the repository. A packaged
install arrives with the first release.

## Rules

- Run only the commands in this guide. Do not edit agent configuration files
  by hand, and do not download or run anything else.
- Show the user each command and get their approval before running it.
- If a command fails, show the user its message and stop. Do not work around
  the failure.
- KnowItAll2 changes only user-level settings that it owns: per agent, one MCP
  server entry, its session hooks, one Skill, and one marked section in the
  agent's global instructions (Claude Code's `CLAUDE.md`, Codex's
  `AGENTS.md`) that tells agents to read the briefing and look things up
  first. It keeps everything else in those files, with their line endings
  and indentation, never writes into the user's projects, and never stores
  secrets. Uninstalling removes what setup added; it can leave an empty
  settings section behind, or change the blank lines at the end of a file.

Each step gives a Windows (PowerShell) and a Linux (bash) form. Use the one for
this computer.

## 1. Check the prerequisites

This version supports Windows 10 and 11, and Linux.

Windows (PowerShell):

```powershell
python --version
git --version
```

Linux (bash):

```bash
python3 --version
python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')"
git --version
```

Python must be 3.12 or later, with SQLite full-text search (FTS5), which
python.org builds and current Linux distributions include. If Python is
missing or older, stop and ask the user to install Python 3.12 or later: from
python.org on Windows, or with the system package manager on Linux. Use the
same interpreter for every command below (`python` on Windows, `python3` on
Linux), because KnowItAll2 registers that exact interpreter with the agents.

## 2. Get KnowItAll2

Agents run KnowItAll2 from this folder, so use a location the user does not
edit. Clone the repository URL the user gave you.

Windows: use exactly this folder in the user profile, not `AppData`. Windows
redirects files that packaged desktop apps create under `AppData` into the
app's private storage, which other agents cannot see and which is deleted with
the app.

```powershell
git clone <repository-url> "$env:USERPROFILE\.knowitall2\app"
```

Linux:

```bash
git clone <repository-url> ~/.knowitall2/app
```

If that folder already exists from an earlier install, update it instead with
`git -C <folder> pull --ff-only`.

## 3. Register KnowItAll2 with the user's agents

Find which supported agents are installed:

- Codex is installed if the `.codex` folder exists in the user's home.
- Claude Code is installed if `.claude.json` exists in the user's home.

Ask the user which of them to set up.

Then ask where their memories should be kept:

- **On this computer** (the usual choice): run the commands below as they
  are.
- **On a KnowItAll2 server the user already runs**, shared with their agents
  on other computers. Ask for the server's address (such as
  `https://kia.example.lan` or `http://192.168.1.20:4191`) and, for each
  agent you will set up, a join code. Tell the user how to make one: open
  the server's page, sign in, and use **Add an agent**, once per agent.
  Claude Code and Codex on the same computer each need their own code, and
  a code works once, for 15 minutes. Add `--server <address> --join-code
  <code>` to the first agent's setup command and `--join-code <code>` to the
  next one's, for example `setup codex --server https://kia.example.lan
  --join-code ABCD-EFGH-JKLM-NPQR`.

  If setup says this computer already has memories, ask the user which they
  want and run the command again with that choice added:
  `--send-memories` adds them to the server's shared memory, and
  `--replace-memories` sets them aside (a backup is kept) and uses only the
  server's. If setup says the code or the server does not work, show the
  user its message; nothing was changed, so the command can be run again
  with a new code, or without the server to keep memories on this computer.

Run each line below as one command, exactly as it is, including the
`PYTHONPATH` part at its start: many agents start a new shell for every
command, so a setting made by an earlier command is gone. The other
`knowitall2` commands in this guide are run the same way, with the words
after `knowitall2` changed.

Windows (PowerShell):

```powershell
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 setup codex
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 setup claude-code
```

Linux (bash):

```bash
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 setup codex
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 setup claude-code
```

`setup` also installs the KnowItAll2 app, a local window that shows what
KnowItAll2 knows and does: it adds a KnowItAll2 shortcut to the Start menu on
Windows, or to the applications menu on Linux. Setting up a second agent
does not add it again. On Windows, a coding agent running inside a packaged
desktop app (such as the Claude desktop app) cannot add to the Start menu;
setup then puts the shortcut on the desktop, and the app adds itself to the
Start menu the first time the user opens it from there. If the user does not
want the shortcut, add `--no-app-shortcut`.

A running Claude Code can rewrite its settings file. If you are Claude Code,
tell the user to quit and reopen Claude Code after this step, then continue
with step 4 in the new session.

Codex runs a hook only after the user trusts it. After setting up Codex, tell
the user to start Codex, run `/hooks`, and trust the KnowItAll2 SessionStart,
Stop, and UserPromptSubmit hooks once. Until then, Codex sessions still reach
KnowItAll2 through its tools, but do not get the automatic briefing, learning
after a commit, the messages about what was learned, or what is new since a
chat started.

On Windows, if setup says it did not add the KnowItAll2 session hooks, a
folder name in the path to Python or to the user's home has a character
(such as `'`, `&`, `$` or `%`) that PowerShell or cmd would read as part of
the command. Codex then works with KnowItAll2 through its tools only, as
above. Tell the user; do not edit `hooks.json` to work around it.

## 4. Verify

Windows (PowerShell):

```powershell
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 doctor
```

Linux (bash):

```bash
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 doctor
```

Every line should start with `OK`. An agent that is installed but was not
set up here is listed as `not set up; skipped`. To check only one agent, add
`--agent codex` or `--agent claude-code`; that agent is then checked in
full, set up or not.

For any `FAIL` line, show the user the `fix:` line under it; it gives the
whole command to run on this computer. If the Claude Code registration was
lost, ask the user to quit Claude Code, run the Claude Code setup command
from step 3 in a terminal, and then reopen Claude Code.

Until the user has trusted the Codex hooks with `/hooks`, the `Codex session
hook` line says `OK` and ends with "Codex runs the ... hooks once you trust
them with /hooks". Nothing is wrong: it is a reminder of the step above, and
the line says "trusted in Codex" once the user has done it.

Codex calls MCP tools, including KnowItAll2's, through its code mode. If a
Codex session says the tools are unavailable because `codex-code-mode-host`
is missing, Codex was installed without its companion programs, for example
as the bare `codex` binary on Linux. Ask the user to install Codex's full
package instead (its npm package or the `codex-package` release archive).

## 5. Finish

Tell the user:

- to start a new Codex session, or quit and reopen Claude Code, to load
  KnowItAll2, and in Codex to trust its hooks once with `/hooks`;
- that memories are stored in the `.knowitall2` folder in their home on this
  computer; when connected to a server, that folder holds this computer's
  copy of the shared memory, kept in step in the background, so KnowItAll2
  keeps working when the server cannot be reached;
- that KnowItAll2 never stores secrets, only where they are kept;
- that it can also learn from finished Claude Code and Codex sessions in the
  background. This is off until the user agrees, because each learning call
  sends a redacted session excerpt to a model through the Claude Code or Codex
  engine on this computer, on the user's existing login. `learn --dry-run`
  shows what would be sent, and `learn --enable` turns it on with whichever
  engine is installed (`--backend claude-cli` or `--backend codex-cli`
  chooses). Ask the user; do not enable it on their behalf.
- where to find the KnowItAll2 app, as setup reported it: in the Start menu
  or the applications menu, or on the desktop. It shows KnowItAll2's health,
  activity, memories, learning, and questions, and runs only while its window
  is open.

## Updating

When the user asks to update KnowItAll2, run its update script:

```bash
python3 ~/.knowitall2/update.py
```

On Windows (PowerShell): `python "$env:USERPROFILE\.knowitall2\update.py"`.
Whichever `python` starts it, the script runs the update with the Python
KnowItAll2 was set up with (and says so), so the agents keep using that one.

It downloads the new version, refreshes the setup of every agent that was
set up (changing only what is out of date), refreshes the app's shortcut,
runs the health checks, and prints what changed and what the user needs to
do. Show the user that output, above all anything under "What to do now".
While Codex is open, the script changes nothing in Codex's own settings; it
says when Codex must be closed and the update run again.

If `update.py` is missing (installs from before version 0.2.1), download the
new version first, then run the update:

```bash
git -C ~/.knowitall2/app pull --ff-only
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 update
```

On Windows (PowerShell):

```powershell
git -C "$env:USERPROFILE\.knowitall2\app" pull --ff-only
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 update
```

Each running agent session keeps the KnowItAll2 version it started with. Tell
the user to start new sessions, or quit and reopen the agent app, after an
update. A session started before an update that changed the memory store's
schema reports that the store was upgraded by a newer KnowItAll2; restarting
that session fixes it, and the memories are safe.

## Uninstalling

Remove KnowItAll2 from each agent, then the app's shortcut.

Windows (PowerShell):

```powershell
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 uninstall codex
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 uninstall claude-code
$env:PYTHONPATH="$env:USERPROFILE\.knowitall2\app\src"; python -P -m knowitall2 app --remove-shortcut
```

Linux (bash):

```bash
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 uninstall codex
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 uninstall claude-code
PYTHONPATH="$HOME/.knowitall2/app/src" python3 -P -m knowitall2 app --remove-shortcut
```

When the computer was connected to a KnowItAll2 server, uninstalling an
agent also removes its key here; uninstalling the last one disconnects the
computer and keeps its copy of the memory. Tell the user to remove those
agents on the server's page too.

## Connecting to a server later, or leaving it

Run these the same way as the commands in step 3: the `PYTHONPATH` part,
then `python -P -m knowitall2` (`python3 -P -m knowitall2` on Linux) and the
words shown after `knowitall2`.

`knowitall2 server status` shows whether this computer shares its memory
through a server and whether the server answers. `knowitall2 server connect
<agent> --server <address> --join-code <code>` connects an agent that is
already set up (with `--send-memories` or `--replace-memories` when setup
asks), and `knowitall2 server disconnect` keeps memory on this computer only
again, keeping its copy.

Uninstalling keeps the user's memories. To remove everything, including the
memories and the app, delete the `.knowitall2` folder in the user's home
afterwards.
