"""The agent-neutral Skill installed for every supported agent."""

from __future__ import annotations

from .. import __version__

SKILL_MARKDOWN = f"""---
name: knowitall2
description: Use KnowItAll2, the user's long-term memory for projects, systems, infrastructure, procedures, decisions, lessons, and rules. Use at the start of work in a project, before rediscovering anything that may have been learned before (hosts, services, routes, credential locations, procedures, past decisions), and after establishing a durable fact worth keeping. Never use it to store secrets.
metadata:
  version: "{__version__}"
---

KnowItAll2 is available through MCP tools: `briefing`, `recall`, `remember`, `forget`, `questions`, `answer`, `settle`, and `learn`.

1. At the start of work in a project, call `briefing` once with `project_path` set to the workspace root, unless a KnowItAll2 briefing is already in your context. Its "Also known here" list is a table of contents: headlines of what KnowItAll2 knows for the project and the systems it uses. Later in a chat, KnowItAll2 may add what is new since the briefing, or the table of contents of another project the chat moved to.
2. Before searching files, networks, or the user for something that may already be known, call `recall` with a few keywords, such as words from a headline in the table of contents. Use what it returns, and treat memories marked unverified as leads to check. Partial matches share only some of your keywords; check that they fit.
3. When you establish something durable that would help a future session, such as a fact about a system, a procedure that worked, a decision, or a lesson from a failure, call `remember` with one self-contained statement. When the user tells you such a thing (a system, a method, a rule), save it right away. Either way, tell the user in one short line what was saved, so they never have to guess.
   - Set `source` to `user` only for the user's own words, `observed` for something confirmed by tool output, and otherwise `inferred`.
   - Use `scope: project` only for things that matter solely inside the current project.
   - Never include passwords, tokens, keys, or other secrets; record where a credential is kept instead.
4. When the user asks KnowItAll2 to learn from the session, call `learn` with `reason: asked`. When a task the user gave you is complete, call `learn` once with `reason: finished`, not after every step. It returns at once; KnowItAll2 also learns by itself after a commit and when a session ends, and shows the user what it learned.
5. If a memory is wrong or outdated, save the correction with `remember` and `replaces` set to the old id, or retire it with `forget`. Only the user's own word changes something the user stated: KnowItAll2 keeps such a memory and asks the user instead. If the user just asked you for the change, record their choice with `answer` as the reply says.
6. When the briefing says KnowItAll2 has questions for the user, call `questions` at a convenient moment and ask the user in plain words. Record each choice with `answer`, and only a choice the user made: an answer counts as the user's own word.
7. A briefing or recall result may include an optional KnowItAll2 request with a task id such as `t-1a2b3c4d`: a check of which of two memories is right, or something to find out about a system. Help only when this session's work already shows the answer or one quick look-only check does, and never let it get in the way of the user's task or change anything to find out. Report with `settle`, and set `certain` to false when it is not settled.
8. When the user asks to update KnowItAll2, run its update script with Python 3.12 or later: `python "%USERPROFILE%\\.knowitall2\\update.py"` on Windows (in PowerShell, `python "$env:USERPROFILE\\.knowitall2\\update.py"`), or `python3 ~/.knowitall2/update.py` on Linux. It downloads the new version and refreshes every agent's setup. Show the user its output, including anything under "What to do now". If the script is missing, follow the Updating section of `docs/install-for-agents.md` in the KnowItAll2 folder (usually `~/.knowitall2/app`).
9. If KnowItAll2 is unavailable, continue normally. Never block a task on it.
"""
