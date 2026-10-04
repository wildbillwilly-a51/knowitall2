# Running a KnowItAll2 server

A KnowItAll2 server lets your coding agents on several computers share one
memory: something Codex learns on your laptop is there for Claude Code on
your desktop. You only need one if you use agents on more than one
computer; otherwise KnowItAll2 keeps memory on your computer, as it does by
default.

Each computer keeps its own copy of the shared memory and keeps it in step
with the server in the background, so KnowItAll2 keeps working when the
server is down: what is saved meanwhile is sent when it is back.

## What you need

- A computer that is always on, with Docker and Docker Compose, such as a
  home server or a small virtual machine.
- A way for your other computers to reach it: its address on your network,
  a name in your DNS, a reverse proxy, or a private network such as
  Tailscale. That part is yours to choose.

## 1. Start the server

Copy this KnowItAll2 folder (the one holding `src/` and `deploy/`) to that
computer, then, inside it:

```bash
docker compose -f deploy/compose.yaml up -d --build
```

This builds a small image and starts the server on port 4191. To use another
port, change the first number under `ports:` in `deploy/compose.yaml`.

## 2. Create the admin account

When it starts without an admin account, the server prints a one-time
**setup code** in its log. Show the log with:

```bash
docker compose -f deploy/compose.yaml logs knowitall2
```

Open `http://<that computer>:4191/` in a browser, enter the setup code, and
choose a username and a password for the server's admin account. Only
someone who can read the server's log can do this, so nobody else on your
network gets there first. The code works once; each start without an admin
prints a new one.

Right after, the page shows a **recovery code** once. Keep it somewhere
safe, such as your password manager: with it, "Forgot your password?" sets a
new password.

## 3. Connect your agents

Each coding agent connects with its own one-time code. On the server's page,
use **Add an agent**, give it a name such as "Laptop - Codex", and copy the
code. Claude Code and Codex on the same computer each need their own code. A
code works once, for 15 minutes.

Then, on that computer, ask the agent to install KnowItAll2 (or to connect
an installed KnowItAll2) by following `docs/install-for-agents.md`, and give
it the server's address and the code. If the computer already has memories,
the agent asks whether to add them to the shared memory or to set them aside
and use the server's.

The server's page lists the connected agents and when each last checked in.
**Remove** stops an agent's access at once; the memories it saved stay.

## Reaching the server

- **On your own network**, the plain `http://` address is fine.
- **Through a reverse proxy** with HTTPS, point the proxy at port 4191 and
  set `KNOWITALL2_TRUSTED_PROXY` in `deploy/compose.yaml` to the proxy's
  address or network (for example `172.18.0.0/16` for a proxy in another
  container); name it as narrowly as you can, since the server believes the
  caller's address that address reports. The server then sees each
  caller's real address, for its limit on wrong passwords, and marks its
  sign-in cookie secure. Without the setting, every caller behind the proxy
  counts as one address, and the server notes that once in
  `logs/problems.jsonl` in its data volume. Agents use the API under
  `/api/`, with their own keys; if your proxy adds its own sign-in page in
  front of the server, let `/api/` through without it, or agents cannot
  connect. Once agents use the
  proxy's address, let only the proxy reach port 4191 (with your firewall),
  since the direct address skips the proxy's sign-in page and HTTPS. The
  server closes a connection after 120 seconds without a request; a proxy
  that keeps idle connections to it open must close them sooner (Traefik's
  default is 90 seconds), or a request it sends just as the server closes
  one can fail.
- **On the internet**, use HTTPS only. KnowItAll2 notes it when an agent
  connects over plain `http://` to anything but a private address.

## Backups

The server makes a backup once a day, and another soon after a burst of
changes, in the `backups` folder of its data volume. It keeps the newest
backup of each of the last seven days and the three newest, so a burst never
pushes out the daily ones. **Download a backup** on the server's
page gives you a copy at any time. Copy the backups off the server as part
of your own backup routine.

The memories, the admin account, and the backups all live in the Docker
volume `knowitall2_knowitall2-data`. Recreating the container (for example
to update it) keeps them; deleting the volume erases them.

## Updating

Replace the KnowItAll2 folder with the new version, then run the same command
again:

```bash
docker compose -f deploy/compose.yaml up -d --build
```

Computers with an older or newer KnowItAll2 keep working with it; each one
uses what both understand.

## Forgot the password and lost the recovery code

In `deploy/compose.yaml`, set `KNOWITALL2_RESET_ADMIN` to any new value, such
as today's date, and restart with the command above. Only the admin account
is removed: memories and connected agents are kept. The server prints a new
setup code in its log (`docker compose -f deploy/compose.yaml logs
knowitall2`); open the page and create the admin again with it, then empty
that line. Each value works once, so a value left in place does not reset
the admin again on the next restart.

## Stopping or removing the server

```bash
docker compose -f deploy/compose.yaml down
```

stops it and keeps its data. Adding `--volumes` also deletes the data. Each
computer keeps its own copy of the memory either way; `knowitall2 server
disconnect` on a computer makes it keep its memory there only again.
