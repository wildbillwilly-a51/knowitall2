# Changelog

## 0.11.8 (2026-10-18)

- **Only the source review decides whether knowledge is lasting.** Reconciliation
  now chooses new, update or repeat; it cannot discard source-approved details
  as low value or partially covered. Legacy drop decisions fail closed. Every
  duplicate still passes the detail and authority checks. This preserves source
  review decisions at the cost of potentially retaining more low-value additions;
  publication and evidence gates still apply.

## 0.11.7 (2026-10-18)

- **Reconciliation references match the shared input context.** An existing
  memory shown under one candidate can also be chosen for another candidate in
  that same call. References to memories shown nowhere still fail closed.
  Application refreshes shared targets: repeats check the current replacement,
  and updates cannot merge into an already superseded row. Evidence, authority,
  source boundaries and detail-preservation checks still apply.

## 0.11.6 (2026-10-18)

- **Recovery respects its approved source boundaries.** Final recovery slices
  review only memories whose evidence appears in that slice. Other histories of
  the same session remain excluded from duplicate and update targets. Memories
  with no retained evidence keep the existing inclusion policy.
- **A correction survives a later preliminary duplicate judgment.** It proceeds
  to the normal reconciliation and publication checks, rather than becoming
  stuck or confirming the wrong original. Later substantive retirements still
  apply. Invalid reconciliation references stay pending and identify their target.

## 0.11.5 (2026-10-18)

- **Final reviews preserve details when a proposed duplicate is incomplete.**
  Every repeat path checks technical details, all significant source words,
  evidence strength and the resolved target before retiring an existing memory
  or dropping a new candidate. Unsafe duplicate judgments keep the original.
  A confirmed original also survives a merge that omits its details; a merge
  cannot discard the new candidate's own information either.
- **Technical preservation includes flags, identifiers and code expressions.**
  Shared maintenance checks recognize assignment syntax, underscores, command
  flags, quoted code and function calls, alongside paths, numbers and polarity.
  Maintenance duplicates and merges also retain every significant source word.
- **Incomplete reviews identify the undecided memories.** Final inputs show
  earlier review states and original evidence; the last part explicitly revisits
  outstanding decisions. Missing decisions still leave the range pending.

## 0.11.4 (2026-10-18)

- **Backend allowance failures preserve pending learning.** Structured quota
  errors and session/weekly limit responses pause all automatic native model
  consumers using durable reset-aware backoff. Explicit probes remain possible.
- **Failed or incomplete final reviews never become completed progress.** Quick,
  final and recovery failures stay separate; duplicate transcripts, bounded
  reviews and malformed reconciliation cannot silently confirm unreviewed work.
- **Historical recovery is explicit and resumable.** Hash-bound manifests keep
  ordinary cursors intact, include approved failed maintenance snapshots, and
  rereview learned records without overriding user-authoritative memories.
- **Usage accounting keeps the entire active budget window** and timestamps
  calls individually. Rejected native attempts have a separate durable ledger.
- **Refused sync changes remain recoverable and visible.** Local edits are
  quarantined instead of overwritten, with safe retry/dismiss and connection
  lifecycle handling. Negative and future row revisions are rejected.
- **Database downloads stream within a concurrency limit.** Compose binds to
  loopback by default; project knowledge-file exports have per-project controls
  and location visibility. Existing hook installation safety remains enforced.

## 0.11.3 (2026-10-17)

- **Learning preserves complete statements and technical spelling.**
  caller-controlled shell arguments and file searches stay unverified.
- **Tool results survive checkpoints and restarts,** including several results
  in one transcript record and slices of the final learning pass.
- **Explicit rule promotions converge across offline computers** without
  duplicate cycles, lost authority, or stale confirmations demoting a rule.
- **Synchronization validates shared fields and question choices,** including
  canonical UTC times before applying rows, and compares legacy times as dates.
- **The application's own access keys are screened.** They are redacted and refused as memories.
- **Sync returns the canonical changes made during a push** before it
  completes. Invalid legacy questions fail safely when answered or settled.

## 0.11.2 (2026-10-16)

- **Codex gets tool rules after each shell command too.** Codex now has the
  hook Claude Code has had since 0.11.1: after a shell command, it gives the
  tool rule that command calls for (PowerShell's, not Claude Code's Bash
  tool's), or the lesson whose error text its output shows, once a chat and
  never one the briefing already gave. Replayed on the same 31 chats, tool
  rules now reach 20 of 33 quirks agents met (16 without it), 14 before the
  first try. Codex starts hooks through PowerShell, so it costs about half a
  second per shell command, which you chose to accept (2026-10-15). **Trust
  the new hook once with /hooks in Codex** (it shows as new); until then
  Codex does not run it.
- **A tool rule comes without the lessons it was made from.** After a
  command, Claude Code and Codex were given a rule and, beside it, one of
  its own source lessons with the same advice. Now a rule's source lessons
  are not given with it, nor later in that chat, nor after the briefing
  showed the rule.

## 0.11.1 (2026-10-15)

Getting what KnowItAll2 knows to agents at the moment it helps, from the
research of 2026-10-13 and your decisions that day.

- **Tool rules, before the mistake.** A tool quirk learned in two chats
  becomes one rule: the shell and program, the command pattern (such as a
  heredoc holding a backslash), the error text it explains, and what to do.
  After any shell command a rule fits, Claude Code is given it once a chat,
  even when the command did not fail; the briefing lists three. Replayed on
  31 real chats, rules reach 16 of 33 quirks agents met, 14 before the
  first try, against 3 after the failure before. Only KnowItAll2 writes
  rules, from lessons learned and checked in two chats, and a rule goes
  when those lessons do.
- **Faster hooks.** The hook after a shell command takes about 0.10 s,
  faster than 0.11.0's after a failed one. Restart Claude Code after
  updating, so it runs the new hooks.
- **Fewer, better pointers.** Branch names, addresses, KnowItAll2's own
  news, and the agent's own tools no longer set off a pointer, nor do names
  too general to mean one system. On 22 real chats: 120 pointers instead of
  178, a third of the noise, and most of the useful ones kept.
- **A project's knowledge folder holds what it uses.** Also the systems its
  documents and scripts name, the hosts its chats reach, and a file a chat
  named that the folder lacked.
- **"New since" is short and relevant.** At most three items, about the
  project or the systems the chat touched, and never what the chat itself
  taught; what a chat taught is told to it once, marked as not yet checked.
- **Tidying combines more.** Near-repeats that each hold a detail are
  combined rather than refused as duplicates, and a combined memory is not
  reviewed again at once.
- **Agents' instructions fit.** They are shorter, so Claude Code no longer
  cuts off their end.
- **News once a chat.** In the Claude desktop app, agents pass on what
  KnowItAll2 learned once a chat (your choice, 2026-10-14); later news stays
  in the app's notice, and news that needs you is always passed on.

## 0.11.0 (2026-10-14)

From a review of how KnowItAll2 works in real use, research into why it learns
poor memories, and your decisions on 2026-10-13. Replayed on ten real chats,
65% of what the new learning keeps passes every check, against 37% before.

- **Learning happens twice.** During a chat, KnowItAll2 still learns at each
  commit, and other chats see it right away, marked "from a chat still in
  progress; may change". Once the chat has been quiet for 90 minutes, or
  ends, KnowItAll2 reads the whole stretch with how it ended. It confirms
  what held, corrects or retires what the chat later overturned, adds what
  it missed, and checks each memory against the ones most like it, so it
  adds to an existing memory instead of saving a repeat. The wait is
  changeable in the app; a longer wait you had set for learning before is
  kept.
- **Your rules, your preferences, and how your projects work are told
  apart.** A rule needs your own words that make one ("make that a rule",
  "from now on"), also when an agent saves it. How you like to work is a
  preference. What you decided or required for a project is filed with that
  project as trusted project information, not as a rule for everything.
- **Memories are filed where the work happened.** Learning names the project
  a memory belongs to, and an agent's `remember` files it under the current
  project unless several projects use it. An agent's save that is close to
  something already known is answered with that memory, to add to it.
- **Less that goes out of date.** Learning leaves out status and records of
  the chat's own work; what the catalog still files as a status goes after
  a week unless confirmed.
- **Honest labels.** A memory is marked observed when a command's output
  shows its details (addresses, paths, versions), wherever the quote came
  from. A short quote counts when it is distinctive, such as "port 8443".
- **Tidying catches up and stays caught up.** Only memories that are new or
  changed are reviewed, plus a slow pass over everything once a month, so
  tidying no longer falls behind. Near-repeats that each hold a detail are
  combined into one memory that keeps every name, number, and "not", and
  can be undone. When two memories disagree, the one the evidence does not
  favour is marked and ranked lower while it is checked, and a check no
  agent takes within a week is settled by the evidence; only your own
  choices still come to you.
- **Each kind of work has its own daily calls.** Background learning,
  tidying, and filing each have their own allowance; learning from your
  work and what you start yourself never use them up.
- **Knowledge files stay on your computer.** By default no knowledge files
  are written in folders OneDrive, Dropbox, Google Drive, or iCloud keep in
  sync, nor in projects whose npm package or Docker image would carry them,
  unless they are excluded there; `doctor` says what line to add.

## 0.10.6 (2026-10-12)

From a full review of 0.10.5 on 2026-10-11, with your decisions about where
KnowItAll2 may write.

- **Knowledge files only where you work now.** A project moved to a new
  folder no longer has its old copy written: files go only to the copy of
  each project an agent last worked in (and its worktrees), and a folder no
  agent has used for 30 days stops too. KnowItAll2 takes its own files out of
  the folders it stops writing, once, and never touches them again. A
  leftover empty `.git` folder no longer counts as a repository.
- **Each project's folder holds what concerns it.** The project's own
  memories and those of the systems it uses, not everything KnowItAll2
  knows; agents still reach the rest with recall. `doctor` names projects
  that live in a folder OneDrive or the like keeps in sync.
- **KnowItAll2 tidies itself again.** Merging repeated memories had made no
  call in two weeks, because learning from your work used up the day's
  calls. Tidying now has its own few calls a day that nothing else can use,
  `doctor` says when it stalls, and `knowitall2 maintain --max-calls N`
  catches up. Background learning takes the newest chats first and sets
  aside those quiet for 14 days (`learn --catch-up` still takes them).
- **Tidying is careful with what you said.** Two memories merge only when
  they say the same thing, your own words nearly word for word; one is
  marked outdated only by a newer memory at least as well supported.
- **When you change your mind, the newer words win.** "Never squash
  commits" after "Always squash commits" replaces the old preference instead
  of counting as saying it again; which words are newer follows when you
  said them, not when the chat was learned. A preference is saved in your
  own words, counted once per chat, and one that contradicts one of your
  rules is kept and asked about.
- **A rule only from your own plain words.** "Make that a rule" counts, and
  "from now on" at the start of an instruction; "don't make that a rule",
  "as a rule of thumb", and text you pasted or quoted do not.
- **Claude Code can never be blocked by KnowItAll2.** If KnowItAll2's files
  are missing (deleted, or mid-uninstall), its hooks now do nothing instead
  of stopping every message. Uninstalling one agent no longer touches what
  the other uses, says to delete the folder only when nothing uses it, and a
  test uninstall never touches the real data home.
- **A busy memory no longer costs a new chat its briefing.** The briefing is
  read without waiting for another process that is writing.
- **Codex keeps its settings for KnowItAll2.** Settings you add to
  KnowItAll2's server entry survive an update, and uninstalling leaves
  nothing that stops Codex from starting.
- **Only checked lessons when a command fails.** The reminder after a failed
  command now uses only lessons that were observed or that you stated.
- **More passwords caught.** About 40 more shapes are kept out of memory and
  out of what learning sends to the model, among them `sqlcmd -P`,
  `redis-cli -a`, piped `ConvertTo-SecureString`, XML and Docker
  credentials, Cisco and OpenWrt passwords, and Tailscale, Vault, and Docker
  Hub tokens; placeholders and prose that only look like secrets are no
  longer flagged.
- **A leftover rule question closes.** "Should it be a rule?" questions made
  before 0.10.5 are settled as notes.

## 0.10.5 (2026-10-11)

From your review of how KnowItAll2 weighs what you say: something said once in
a chat was treated as a possible permanent rule, and KnowItAll2's questions
came up in nearly every chat.

- **What you say once is a preference, not a rule.** Learning keeps how you
  want something done as a preference, with the work it was about, and never
  asks whether it should be a rule. A rule only when you say so: "make that a
  rule", "from now on".
- **Saying it again is what makes a preference last.** When you say the same
  thing in another chat, KnowItAll2 counts it; said in three chats, briefings
  list it under "Your preferences".
- **Newer words win.** A newer statement of yours replaces an older one
  without asking, and what you said about a system gives way to what an
  agent observed. Your rules, preferences, and decisions still change only
  by your own words.
- **Questions only where they belong.** A question reaches you only in a
  chat about its project, at most two at a time and in at most two chats;
  one no one answers closes after 30 days, changing nothing. The news after
  a commit is one line and never mentions questions.
- **A lesson the model refuses to read no longer holds up the rest.** Noting
  which command each lesson is about tries a refused batch again in small
  groups, and leaves out what is refused twice.
- **A project keeps what it learned when its repository gets a remote.** A
  project is known by its folder until its repository has a remote, then by
  the remote. What was saved before now moves with it the first time an
  agent works there, on every computer (`knowitall2 rejoin` does it for
  every folder at once), and work in that folder is no longer filed under
  the old project.

## 0.10.4 (2026-10-11)

From a measurement of five days of real use (37 chats): KnowItAll2 delivered
about a third of what agents needed, and about a quarter more was in memory
but not delivered, mostly quirks met while running a command.

- **When a command fails, the agent hears what KnowItAll2 knows about it.**
  In Claude Code, after a Bash or PowerShell command fails, KnowItAll2 gives
  the agent the newest lessons about that command, such as how a tool is
  blocked or must be run here, once per command in a chat and only when the
  error points at that command. Learning notes which command each new lesson
  is about; `knowitall2 catalog --commands` does the same for the lessons
  you already have. Replayed on those five days, it spoke up about once in
  two chats, nearly always about that failure, and commands that succeed are
  not slowed down.
- **Pointers no longer follow KnowItAll2's own news.** Agents pass on what
  KnowItAll2 learned, and the pointer then named KnowItAll2's own file in
  unrelated projects: one pointer in seven, almost never read.
- **Learning no longer keeps where some work stood.** A memory saying some
  work is "not yet" fixed, deployed, or released goes out of date, often
  before anyone reads it; learning now turns such memories down.
- **Upkeep gets its share of each background run.** Learning from waiting
  chats used every call of every run, so merging repeated memories had not
  run in a week (one quirk was stored 66 times). Learning now leaves calls
  for upkeep, the catalog, and noting commands.
- **Guessed rules that need no answer no longer wait for you.** When a rule
  the learner guessed repeats or contradicts one of your own, or was about
  one piece of work, the question review forgets it or keeps it as a note.
  Making something a rule is still only yours.
- A computer closes the server's error responses once it has read them.

## 0.10.3 (2026-10-09)

Fixes from a review of 0.10.2 on 2026-10-09; each was reproduced before it
was fixed.

- **Learning stops reading once it has no calls left.** Once its calls
  for the day were used up, each background learning run still read,
  split, and screened every session waiting to be learned, only to put it
  off: about 8 minutes of work every 30 minutes on a computer with a large
  backlog. A run now stops reading as soon as no call is left, and its
  entry in the journal says how many sessions wait.
- **A computer's key is sent only to its server.** If the server's address
  ever redirected elsewhere, the agent's key went along with the request.
  KnowItAll2 now refuses redirects and says where the address points.
- **KnowItAll2's own keys and codes are kept out of memory.** An agent's
  server key, or the server's recovery or setup code, was not recognized
  as a secret, so it could be saved as a memory or sent to the learning
  model. Both are now screened like other secrets.
- **The server's admin sign-in is safe from two requests at once.** A
  recovery code used twice at the same moment worked twice, and a sign-in
  with the old password could finish just after a recovery and still get
  in. Each now counts only if the password or code is still the current one.
- **Replacing a memory with one that is already known works.** It used to
  confirm the known memory and leave the old one in place.
- **The knowledge files follow every change.** A change from another
  computer could be missed when it carried an earlier time than the newest
  memory here, and syncs, learning, upkeep, and changes in the app did not
  rewrite the files until the next chat message. Each now does when it
  finishes.

## 0.10.2 (2026-10-08)

- **KnowItAll2 looks into its questions before you see them.** From
  2026-09-28 the background catalog spent every run's calls on filing
  memories and describing systems, so its review of questions never ran:
  all 39 open questions reached you unreviewed, after the 3-day wait. Each
  run now reviews one batch of questions first. The questions list says
  what was found, marks a question no one has looked into yet, and asks
  agents to look into each one and recommend an answer before asking you.
- **A system's file has every memory that names it.** A memory is filed
  under the one system it is mainly about, and 60% of memories name another
  system too; those were missing from the other system's file and profile.
  On 2026-10-05 the iDRAC address of a failing host was filed under the
  homelab, so the iDRAC's file and profile said it was not known, and the
  agent tried the wrong iDRAC. Each file now lists, last, the memories filed
  elsewhere that name it, with where they are filed; a system's profile in
  the app shows them and counts them toward what is known, and the catalog
  sees them when it writes what is missing.
- **The catalog corrects wrong aliases.** Aliases could only be added, so
  words like "workstation" and "Docker" kept pointing to one system. When
  the catalog describes a system it now gives the complete list of names
  that mean that system and nothing else, which replaces the old list; an
  alias that is another system's or a project's name is never kept. Every
  system is described once more after the update, which cleans up the
  existing aliases.
- Finding names in text is faster (word by word instead of one large
  pattern).

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
  containing a character such as "â‰¥" or "â†’" made the session-start briefing
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
