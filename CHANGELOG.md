# Changelog

## 0.10.1 (2026-10-05)

- **Fewer pointers, to files worth reading.** In the first day of 0.10.0
  agents opened the pointed file after only 8 of 45 pointers: most named
  something the work did not need. A pointer now comes only from your
  message and the agent's own words, not from every command it runs; a
  login name (`name@host`) or a folder deeper in a path or a web address no
  longer counts, though a host at the start of a path still does; and a
  file with fewer than three memories gets no pointer. Replayed on those
  chats: about a quarter fewer pointers, every one an agent followed kept,
  and more of the files that held a missed answer pointed to in time.

## 0.10.0 (2026-10-04)

Everything KnowItAll2 knows is now where agents already look: in files in
each project. Measured in real sessions, agents asked KnowItAll2 for
knowledge rarely and found about 30% of what they needed through it; given
the same knowledge as files, with a pointer to the right one, they found the
stored answer every time in a test on real failures.

- **Everything KnowItAll2 knows, as files in each project.** Every active
  memory is written to `knowitall2-known/` at the root of each project folder
  KnowItAll2 has seen: one file per system or project, how to reach it and
  where its sign-in is kept first, then how-tos, then the rest, newest first,
  and a `README.md` listing the files. Agents search it like any project
  file. Nothing is filtered or ranked: what an agent can find is everything
  KnowItAll2 knows. The files are rewritten in the background when memories
  change, here or on another computer (after a sync).
- **Git never sees it.** The folder is listed in the repository's own
  exclude file (shared by its worktrees), and a `.ignore` file lets search
  tools that follow Git's ignore rules see it. A folder or `.ignore` that
  KnowItAll2 did not write is left alone, and `knowitall2 doctor` names any
  project where your own `.ignore` hides the folder. Folders that are not Git
  repositories get no files.
- **A pointer to the right file.** When your message, or the agent's own
  commands, name a system or project KnowItAll2 knows about, the agent is
  told once in that chat which file holds what is known about it.
- **The instructions say where everything is.** The KnowItAll2 section of
  `AGENTS.md` and `CLAUDE.md`, the skill, and the briefing tell agents to
  search the folder and read the file they are pointed to, and never to edit
  it. Run the update to refresh each agent's instructions.
- **Off when you want.** `knowitall2 known --off` removes the folders and
  keeps them out; `knowitall2 known --on` brings them back;
  `knowitall2 known` writes them now. Uninstalling the last agent removes
  them.

Fixes from a second review of the whole program, a day after 0.9.0, first
prepared as 0.9.1. Each fix came with a test that fails without it.

- **A server put back from a backup no longer splits your memory.** When
  the server's memory goes back (a backup put back, or a new, empty
  volume), every connected computer notices at its next sync and sends its
  memories again, so the server and every computer end up with everything.
  Before, they silently kept different memories. A sync interrupted at the
  wrong moment also no longer leaves one memory different from the server.
- **Learning trusts less of what it reads.** A web page fetched from the
  command line, a commit or diff, or a settings file printed from a
  project's own folder no longer counts as something a command observed. A
  learned memory counts as your own words only when it says nearly only
  what you said, with no address, path, or command you did not write; text
  you pasted into a chat is not your words. A memory the model wrongly says
  it updates is left alone, and a run that hits a busy memory store keeps
  what it already learned.
- **"Learn these" works.** The app's button for catching up on past
  sessions answered with an error since the app's first version. Catching
  up now reads only the part of a session that was set aside.
- **Questions reach you.** A question handed to agents that none checked
  comes to you after a week even when learning is off, and the app lets you
  answer it. A disagreement with something you said always comes to you.
- **"Ask an agent to find these out" stays in the project folder** on
  Claude Code 2.1 or later.
- **Fewer false alarms about secrets.** "The password is shared with the
  team" and similar sentences are no longer refused as secrets, while
  passwords given to `docker login` and `smbclient` are now caught.
- **Codex's hooks stay** for a Python installed under `Program Files
  (x86)`, which 0.9.0's update removed.
- **The update undoes itself** when the new version cannot start, and two
  updates never run at once.
- **The server is cheaper to refuse**: it checks an agent's key before
  reading what the agent sends. Removing an agent frees its maintenance
  turn, and wrong setup codes are limited like wrong passwords.
- **Smaller fixes.** The catalog tries again what a short model answer left
  out instead of filing it as general for good; a project's instructions
  no longer hide one of your rules by quoting it in a "does not apply"
  sentence; the import report never shows a secret; a Codex answer that
  mentions a login no longer stops learning; the learner's state file
  stops growing for deleted sessions.

## 0.9.0 (2026-10-03)

Fixes from an outside review of the whole program. Every finding was
checked against the code, and each fix came with a test.

- **Secrets are caught in many more shapes.** Passwords and tokens under any
  key name (`DB_PASSWORD=`, `"password": "..."`), whole private keys,
  credentials in addresses, command-line password options, and more
  services' tokens are kept out of memory, the activity log, and the server.
  `doctor` lists older memories that now look like they hold a secret, and
  changes nothing itself.
- **Only you change your own statements.** An agent that asks to forget or
  replace something you said now asks you instead, and learning never
  replaces your statements on its own. `knowitall2 remember` saves what it
  is given as inferred unless `--source user` is passed.
- **Learning trusts what it saw, not what it read.** A memory counts as
  observed only when it rests on what a command printed. What came from
  documents, searches, other tools, or helper agents stays unverified, and
  what is learned from a project's documents stays with that project. A
  memory said to be in your own words must say what you said.
- **Sharing across computers never loses a change.** A sync cut short, a
  memory that arrives before its project, a reconnect, a database put back
  from an older copy, or an edit made during a sync no longer drops
  anything, and a computer's first upload finishes later if it is
  interrupted.
- **On Windows, nothing stops on an odd path or a busy file.** Hooks work in
  folders with accented names and never fail a session. Learning never runs
  through `cmd.exe`, so a Claude Code installed with npm gets its whole
  instructions, and an engine that hangs is stopped on time. Files another
  process is reading or replacing are waited for, and the learner's and
  sync's locks are free the moment their holder ends.
- **Setup leaves your files as they were.** Settings files keep their line
  endings, indentation, and escaping, and links stay links. Setup checks
  that it can finish before it changes anything. Codex hooks are left out,
  and `doctor` says why, where no Windows command can run them safely.
  `doctor` checks only the agents you set up, and reads the memory store
  without changing it.
- **The server is harder to misuse.** A HEAD request no longer breaks the
  next request on the same connection. Sign-in limits hold under many tries
  at once. After an admin reset, the setup page needs a one-time code from
  the server's log. New admin passwords are hashed at a higher cost. A
  computer whose clock runs ahead no longer wins every disagreement, and
  backups keep one copy for each of the last seven days.
- **Smaller fixes.** Old activity and use records are tidied daily, whether
  learning is on or not; the current chat's log is found quickly however
  many logs there are; requests to this computer and its own network skip
  the system proxy; output piped from the command line is UTF-8; and
  `update` runs with the Python KnowItAll2 was set up with.

## 0.8.4 (2026-10-02)

- **KnowItAll2 is public, as source-available software.** Anyone may install
  and use it free of charge on their own computers; see `NOTICE` for what
  else is and is not allowed. Installing needs no invitation or account:
  point your agent at the repository's address.

## 0.8.3 (2026-10-02)

Fixes from an end-to-end review of sharing memory across computers.

- **Memories saved after "use only the server's memory" are shared.** A
  computer that set its memories aside when connecting kept its projects
  but never sent them, so a memory saved later in one of its own projects
  waited to be sent forever. Its projects are now sent when it connects.
- **A server never stops an older computer from syncing.** Kinds of data a
  newer server shares are skipped by a computer that does not know them,
  as unknown columns already were.
- **A stopped server behind a proxy reads as unreachable.** A proxy's 502,
  503, or 504 used to read as "the KnowItAll2 server said: Bad Gateway".
- **`knowitall2 catalog` takes its turn.** Like maintenance, it waits while
  another computer sharing the memory is tidying up.
- **One guesser cannot lock everyone out of the server's page.** Failed
  sign-ins and codes are limited per address only.
- **The server backs up soon after a burst of changes**, such as a computer
  sending its whole memory, as well as daily; it answers HEAD requests; and
  `server status` and `doctor` flag a server that speaks another version.
- The documents describe what is built: the design, the summary, the
  vision, and the README.

## 0.8.2 (2026-10-02)

- **Learning prefers the official Claude Code command-line tool.** When
  Anthropic's Windows installer has put `claude.exe` in `.local\bin` in your
  user folder, KnowItAll2 uses it even from processes that started before it
  was added to your PATH. The Claude desktop app's own engine is used only
  when the tool is not installed, so the desktop app's internal folders no
  longer matter to learning.

## 0.8.1 (2026-10-02)

- **Learning works again with the Claude desktop app.** An automatic update
  of the app on 2026-10-01 moved its Claude Code engine one folder deeper,
  and KnowItAll2 no longer found it, so nothing was learned ("no Claude Code
  engine was found"). KnowItAll2 now looks a few folders deep, and passes
  over an engine whose download is incomplete.
- **`doctor` checks the learning engine.** When learning is on and its
  engine cannot be found, `doctor` (which runs after every update) says so
  and what to do.

## 0.8.0 (2026-10-02)

- **One memory for your agents on every computer, if you want it.** You can
  run a KnowItAll2 server in Docker (`deploy/`, guide in `docs/server.md`)
  and connect your agents on several computers to it. Memory still stays on
  your computer unless you choose this when your agent installs or connects
  KnowItAll2.
- **A simple page runs the server.** The first visitor creates the admin
  account and gets a one-time recovery code; the compose file can reset the
  admin if both are lost, without touching memories. Signed in, you connect
  agents with one-time codes (each agent its own), remove them, see the
  server's health, and download a backup. The server keeps a daily backup.
- **Each computer keeps working on its own.** A connected computer keeps its
  own copy of the shared memory and keeps it in step in the background, so
  briefings and searches never wait on the server, and what is saved while
  the server is down is sent when it is back. When two computers change the
  same memory, the newer change wins on both; the same memory learned on two
  computers stays one. Only one computer at a time tidies up the memory.
- **Setting up with a server.** `knowitall2 setup <agent> --server <address>
  --join-code <code>` connects before changing anything, and asks what to do
  with memories the computer already has (`--send-memories` or
  `--replace-memories`, with a backup). `knowitall2 server status`,
  `connect`, and `disconnect` change your mind later; `doctor` and the app
  show how sharing is going.
- **Memories say which computer saved them.** The memory store moves to
  schema 3 when this version first opens it (after backing it up beside
  itself). Chats that were open during the update say so once; start a new
  one.

## 0.7.0 (2026-09-30)

- **Briefings lead with what matters to the project.** Your rules about a
  system the project's memories are not about (such as gating GitLab behind
  a sign-in) are left out of its briefing with one line naming those
  systems, so recall finds them when you work there. The project's two
  newest memories from the last three days are given in full, so what one
  chat just found reaches the next one, not only a shortened headline.
- **A rule can apply everywhere.** Tag a rule `everywhere` (say it again with
  the tag) and it stays in every project's briefing, whatever system it is
  about, such as keeping every credential in your password manager.
- **News reaches a chat while it works.** When an agent searches or saves in
  KnowItAll2, the answer also says what was added for its projects since
  its briefing that it has not seen, such as a decision you made in another
  chat. Before, that waited for your next message.
- **A folder a project moved out of no longer borrows the project above
  it.** A chat opened in a folder that is now empty belongs to no project,
  instead of the repository of a parent folder. What it learns is still
  filed under the project its work was in.

## 0.6.0 (2026-09-30)

- **Learning keeps what agents need most.** KnowItAll2 now looks first for
  how systems are reached (hosts, accounts, how to run commands with
  privileges, APIs), where things live (configuration, state, data, logs,
  backups), and procedures and fixes that worked, including what shows only
  in commands and their output. It skips records of the work itself
  (releases, commit ids, test counts, "as of" states), which went out of
  date. Tested on the same excerpts before and after: 54 memories kept
  before, 67 after, with the access routes and locations that were missed.
- **Long decisions are no longer lost.** A memory may be up to 1,000
  characters, and longer knowledge is split, where before a long decision
  was turned down.
- **Fewer questions you cannot answer.** An instruction an agent wrote is
  kept as a note instead of becoming a question about your rules, and every
  question says which chat and project it came from, and when.
- **Learn from what projects keep in writing.** `knowitall2 learn --documents`
  reads each project's state file, summary, handoff, and other documents,
  and Claude Code's own notes for it, the most useful first (about two or
  three calls a project), and learns from them like a session. Documents it
  has read are not read again until they change. Documents an agent reads in
  a chat now reach learning too.
- **Past chats are no longer lost.** Chats set aside as already learned by
  another system could later be marked learned without being read; they are
  listed again under catching up, and `--max-calls` catches up the newest of
  a folder's chats first.

## 0.5.1 (2026-09-30)

- **Learning from your work never waits.** What KnowItAll2 learns after a
  commit, at the end of a session, or when you ask is learned right away,
  with no daily limit. The daily limit now holds back only background
  learning (sessions that went quiet, catching up, tidying up), which gets
  what is left of the day's total.
- **What is learned goes to the right project.** A chat opened in one folder
  often works in another project. KnowItAll2 now files what it learns under
  the project the work touched (three or more tool calls in its folder, more
  than in the chat's own), so it shows in that project's briefings.

## 0.5.0 (2026-09-30)

- **Agents can see what KnowItAll2 knows.** The briefing at the start of each
  chat now lists, after your rules and the project's key points, the
  headlines of everything else known for the project and for the systems it
  uses, by system, so the agent knows what to look up. Notes about a current
  state (counts, versions, "not yet") are left out because they go out of
  date, and a rule the project's AGENTS.md already states is not repeated.
- **Chats that stay open keep up.** When you send a message, the agent hears
  about what KnowItAll2 learned since the chat started (a line each, at most
  three), and, when the chat starts working in another project, what is
  known about that project, once. A chat that was open before this update
  gets its project's list once, at your next message.
- **Search finds what is there.** A search no longer needs half of its words
  to match. Memories are ranked by how much of the search they cover, rarer
  words counting more; a long, specific search shows the closest partial
  matches, marked as such, instead of nothing. Word endings no longer matter
  ("releases" finds "release"), and when nothing matches, the agent is told
  which words no memory mentions.
- **KnowItAll2 is part of how agents start work.** Setup adds a short
  KnowItAll2 section to your global agent instructions (Claude Code's
  CLAUDE.md, Codex's AGENTS.md): read the briefing when getting oriented,
  and look things up before working them out again. Uninstall removes it;
  nothing else in those files changes.
- **Memories can be moved to the right project.** `knowitall2 move` files
  memories that were learned in a chat opened in one folder under the
  project they are about.

Updating: in Codex, type /hooks and trust KnowItAll2's new message hook
(UserPromptSubmit). If Codex is open during the update, close it and run the
update again first. Claude Code needs nothing.

## 0.4.2 (2026-09-29)

- **Briefings and news no longer vanish over one character.** A memory
  containing a character such as "≥" or "→" made the session-start briefing
  and the news KnowItAll2 passes to the agent fail on Windows, silently; the
  news was then counted as shown and never delivered. Hook output is now
  plain-ASCII JSON, news counts as shown only once it has been written, and
  the command line and background learning no longer fail on such
  characters either.

## 0.4.1 (2026-09-29)

- **One "not learned yet" message per chat.** When several commits in one
  chat run into the daily learning limit, you are told once that it has not
  learned yet, not once per commit.

## 0.4.0 (2026-09-29)

- **Updates reach chats that are already open.** Claude Code and Codex keep
  KnowItAll2's tools running for as long as a chat is open, which in the
  desktop apps can be days, so until now a chat kept the version it started
  with. KnowItAll2's tools now run the installed version for every request,
  so an update takes effect in open chats from their next KnowItAll2
  request, with no restart and no extra usage. Claude Code also shows new
  or changed tools at once; open Codex chats get new tools after Codex
  restarts.
- **See what was learned in the Claude desktop app.** The Claude desktop app
  does not show KnowItAll2's own messages. There, the agent now tells you in
  a line or two, at the start of its reply to your next message, what
  KnowItAll2 learned or could not do. Codex and the Claude Code terminal
  show the messages themselves, as before.

Updating: quit and reopen Claude Code and Codex once, so chats that are open
now start using the new version. After that, updates reach open chats by
themselves.

## 0.3.1 (2026-09-29)

- **Ask an agent to find these out**, once per system. On a system's
  profile, one button now starts one agent right away in the project folder
  the system is worked on in. It looks for everything that is missing at
  once, reading files only: it changes nothing and runs nothing. Each answer
  names the file it came from and quotes it, and is kept only if that quote
  really is in the file. What it cannot find is left to the agents that work
  with the system later, as before. The page shows while it looks, then what
  it found and where. It uses the same model as learning, counts toward the
  daily total, and never waits for the daily limit.
- **A better learning model.** A comparison on six real sessions showed the
  light models missing too much, and Haiku misreading some facts. Learning
  now uses Sonnet on Claude Code (it kept a third more, read facts more
  accurately, and ran faster, for about 1.4 times the usage per call) and
  Codex's everyday model at medium effort (about twice what its fast model
  kept, for about 10% more). Updating moves learning off the former default
  once and says so; you can still choose another model under Learning.
- A quote with a few words re-typed now counts as evidence for a learned
  memory, labeled inferred, so fewer good ideas are turned down; only an
  exact quote makes a memory "seen in action" or one of your rules.
- On Codex, learning switches to Codex's current everyday model by itself
  when the one it used is no longer offered.

## 0.3.0 (2026-09-29)

- **Learning as you work, and you see what it learned.** KnowItAll2 now
  learns from a session right away when a turn ends with a commit, when a
  Claude Code session ends, or when you ask your agent to learn (or it
  finishes a task you gave it). Learning runs on its own, so your agent
  never waits. When the turn ends, the session shows what was learned,
  what was turned down, or why nothing could be learned yet, such as the
  daily limit. This costs your agent no tokens.
- **What it just learned**, at the top of the Learning page, with what it
  is learning right now, and **Learn anyway** for a session that waited for
  the daily limit. The Overview shows the latest result.
- Agents now save what you tell them about a system, a method, or a rule
  right away, and say in one line what was saved.

Updating: tell your agent "update KnowItAll2". In Codex, trust the new
KnowItAll2 Stop hook once with `/hooks`.

## 0.2.1 (2026-09-29)

- **"Update KnowItAll2".** Tell your coding agent to update KnowItAll2 and it
  runs the new update script: it downloads the new version, refreshes every
  agent's setup (only what is out of date), refreshes the app's shortcut,
  checks everything, and tells you in plain words what changed and what to do.
  While Codex is open it leaves Codex's own settings alone and says so.

Updating from 0.2.0: tell your agent "update KnowItAll2 by following the
Updating section of docs/install-for-agents.md in ~/.knowitall2/app". After
that, "update KnowItAll2" is enough.

## 0.2.0 (2026-09-28)

The KnowItAll2 app, written for people who are not developers.

- **The KnowItAll2 app.** `setup` now installs it with KnowItAll2, in the
  Start menu on Windows or the applications menu on Linux. It opens as its
  own window and shows:
  - Overview: whether KnowItAll2 is working, and how much your agents used it;
  - What it knows: every system and project by area, each with a profile of
    what is known (what it is, where it is, how agents reach it, where the
    sign-in is kept, what agents can do, how-tos, rules, decisions,
    lessons), a status, what is missing, and a way to fill each gap: tell
    KnowItAll2 yourself, or ask an agent to find out;
  - Memories: each one as a plain sentence, with the exact wording on
    request; confirm, correct, forget, and restore;
  - Questions: only what is still unclear or yours to decide, in plain
    words, with a "Not sure" choice;
  - Learning: each run, every idea it considered and why it was or was not
    kept, settings, Learn now, and catching up on past sessions;
  - Activity: everything KnowItAll2 did, including problems.

  It runs only while its window is open, needs no extra software, and is
  reachable only from your own computer.
- **A catalog of what KnowItAll2 knows.** After learning, a background pass
  gives each memory a plain one-line summary and files it under the system
  it is about; each system gets a plain summary and a list of what is
  missing. It uses the same model calls and daily limit as learning.
  `knowitall2 catalog` runs it by hand.
- **Fewer, clearer questions.** KnowItAll2 settles a disagreement between
  memories itself when the evidence is clear. Otherwise the next agent
  working on that project gets an optional, look-only check (the new
  `settle` tool). Only then are you asked. Your own statements and rules are
  only ever changed by you.
- **Learning keeps far more of what it finds.** The evidence check no longer
  rejects real quotes whose punctuation the model re-typed (such as curly
  apostrophes) or that it shortened with "...". In a live sample, 11 of 14
  ideas passed instead of 4; paraphrases still fail.
- **Catching up.** `learn --catch-up` and the Learning screen learn from past
  sessions that were skipped as already covered, by project folder, with an
  estimate of the calls first.
- **Activity journal.** Everything KnowItAll2 does is kept for 90 days, and
  problems are recorded even when the database cannot be opened.
  `knowitall2 activity` shows both, and `stats` reports recent problems.

Updating from 0.1.0: pull the new version, then run `setup` again for each
agent you use, so it gets the new tool and instructions and the app is
installed. Start new agent sessions afterwards.

Known limitations:

- Systems cannot yet be renamed, moved to another area, or merged in the
  app; the catalog sometimes splits or files a system oddly.
- On Windows, when setup runs inside a packaged app such as the Claude
  desktop app, the app's shortcut starts on the desktop and adds itself to
  the Start menu the first time it is opened from there.
- The catalog's first pass over existing memories takes about one model call
  per 25 memories, plus one per 5 systems, within the daily limit.
- macOS is still not supported, and there is still no sync between
  computers.

## 0.1.0 (2026-09-28)

The first release for invited testers.

- **Memory.** A local store with full-text search. `recall` searches by
  keywords, `remember` saves one durable statement, and `forget` retires a
  memory that is wrong. Secrets are refused: KnowItAll2 stores where a
  credential is kept, never the credential itself.
- **Agents.** Claude Code and Codex are supported through one MCP server with
  six tools (`briefing`, `recall`, `remember`, `forget`, `questions`,
  `answer`), a Skill, and a session-start hook that adds the briefing to each
  new session. `setup`, `doctor`, and `uninstall` change only what KnowItAll2
  owns.
- **Learning** from finished Claude Code and Codex sessions, in the
  background. It is off until you turn it on, and it runs on the Claude Code or
  Codex engine already on your computer, with your own login.
  - Each memory must quote the session it came from.
  - Only your own words become rules.
  - Weaker evidence never replaces stronger evidence.
  - `learn --dry-run` and `--show` preview exactly what would be sent.
- **Maintenance.** After learning, groups of related memories are reviewed for
  duplicates, outdated facts, and temporary status. Nothing is deleted or
  rewritten: `history` lists every change, and `restore` undoes one.
- **Questions.** When KnowItAll2 cannot decide on its own, it asks you in rare
  batches, and your answer decides.
- **Import** from other systems, in a documented JSON Lines format.
- **Platforms:** Windows 10 and 11, and Linux, with Python 3.12 or later.

Known limitations:

- macOS is not supported.
- Memories stay on the computer where they were saved; there is no sync.
- Learning needs Claude Code or Codex on the same computer.
- Codex must be installed with its companion programs (its full package) to
  reach KnowItAll2's tools.
