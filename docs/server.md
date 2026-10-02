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

Open `http://<that computer>:4191/` in a browser. The first visitor creates
the server's admin account, with a username and a password of their choosing.
Do this right after starting it, before you make the server reachable from
anywhere but your own network.

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
  container). The server then sees each caller's real address and marks its
  sign-in cookie secure. Agents use the API under `/api/`, with their own
  keys; if your proxy adds its own sign-in page in front of the server, let
  `/api/` through without it, or agents cannot connect. Once agents use the
  proxy's address, let only the proxy reach port 4191 (with your firewall),
  since the direct address skips the proxy's sign-in page and HTTPS.
- **On the internet**, use HTTPS only. KnowItAll2 notes it when an agent
  connects over plain `http://` to anything but a private address.

## Backups

The server makes a backup once a day and keeps the last seven in the
`backups` folder of its data volume. **Download a backup** on the server's
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
is removed: memories and connected agents are kept. Open the page to create
the admin again, then empty that line. Each value works once, so a value left
in place does not reset the admin again on the next restart.

## Stopping or removing the server

```bash
docker compose -f deploy/compose.yaml down
```

stops it and keeps its data. Adding `--volumes` also deletes the data. Each
computer keeps its own copy of the memory either way; `knowitall2 server
disconnect` on a computer makes it keep its memory there only again.
