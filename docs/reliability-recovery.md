# Reliability fixes and recovery

These changes are prepared in source. Publishing, server deployment, installed
client updates, configuration changes and recovery of live data require the
user's deployment approval.

## Learning integrity

Backend authentication, availability and allowance failures do not count
against a transcript. Native model consumers share a durable backend pause in
`learner/backend-availability.json`. Explicit reset metadata takes precedence;
when a reset cannot be interpreted, a labelled probe interval starts at 15
minutes and increases to six hours after repeated rejections.
`learn --retry-backend` permits one explicit probe during a pause. Attempts,
including blocked native requests, are recorded separately in
`learner/backend-attempts.jsonl`; suppressed calls do not launch a model.

Quick and final passes have separate failure counters. Failed ranges remain
pending, with their byte/event boundaries and reasons. Repeated content errors
or timeouts back off up to 24 hours. A failed final pass never confirms memories
or advances the settled cursor. Malformed or incomplete final decisions fail
closed. Stale promotion excludes sessions that still have pending reviews.
An explicit choice to disable learning retains its existing accept-as-is policy.

Final-pass duplicates, sibling repeats, and maintenance use conservative detail
preservation checks. A proposed replacement must retain every significant source
word and technical token; otherwise the original remains. Explicit corrections
are distinguished from duplicates. Incomplete decisions name their record IDs
and remain pending; an unexplained retirement cannot complete a review.

Call budgets retain the complete active window, regardless of call volume.
Call timestamps describe individual completion, not the learning run's start.
Learning and user call histories retain seven days; journal reports retain their
existing longer history. Work-triggered learning remains exempt from the
background budget; both respect backend availability.

## Historical review recovery

`knowitall2 learn --recovery-plan PLAN.json --since 2026-10-11` reads the local
journal and state without model calls or state changes. It identifies historical
skip incidents, verifies available transcripts and binds each proposed range to
its absolute path, session ID, complete-line boundaries and prefix SHA-256.

Old skip events did not retain exact byte boundaries. Those entries explicitly
propose a conservative whole-session final rereview; they do not claim to have
reconstructed the missing range. Multiple copies of a session are listed as
separate candidates. The operator must select one verified source per session,
resolve missing sources, and inspect the manifest before applying it.

After approval, `knowitall2 learn --recover PLAN.json --allow-whole-session`
queues those selected rereviews. It makes no model call. Subsequent learning
processes them under normal background budgets and backend pauses. Recovery
has its own cursors and completion IDs; it does not rewind ordinary progress,
and replaying a completed manifest queues nothing. Changed approved prefixes
are refused; appended later activity remains ordinary learning.

Recovery revisits learned, non-authoritative source-session memories even when
an old failed pass removed their provisional labels. Direct user statements and
choices are excluded from that review set; ordinary publication authority and
duplicate checks still apply. No recovery is applied automatically on update.
The plan also lists maintenance failures that match retained allowance error
timestamps unambiguously. Approved entries reopen only the exact, unchanged
failed review snapshots; a newer review is preserved. Resetting this bookkeeping
occurs during a subsequent learning run, separately from queueing the manifest.

## Synchronization

Terminal refusals are retained in local metadata as a rejected-change queue.
The server's version cannot overwrite the rejected local edit while it remains
unresolved. Sensitive payloads are not copied into that queue; reasons are
redacted. `knowitall2 sync --rejections` shows metadata without payloads.

Correct the local content, then use `sync --retry-rejected ID`; the next sync
sends its current version. `sync --dismiss-rejected ID` chooses the server's
version and rewinds the pull cursor so that version is fetched again. Dismissing
a new local-only row leaves that row local if no server version exists.
Accepted subsequent edits resolve earlier refusals for the same row.
Disconnecting archives old-server refusal evidence locally rather than letting
it protect rows from a new server. Explicit resolution holds the sync lock,
and unresolved refusals remain visible in command, doctor and app health.

The server rejects negative and future row revisions. `invalid_base` is a
structured retryable response: a client forgets its invalid revision and sends
again through normal conflict resolution. A valid revision is still an
authenticated client's claim, not cryptographic proof that it read that row.

## Export and deployment hardening

Database downloads use fixed-size reads, keep at most two concurrent snapshots,
and clean up temporary copies on completion or disconnect. The example Compose
file binds host port 4191 to loopback by default. Set `KNOWITALL2_BIND_ADDRESS`
explicitly to a trusted interface when a remote proxy or private client needs
it; provide HTTPS or a trusted encrypted transport for remote traffic.

`known --project FOLDER --off` disables and removes managed knowledge files for
one project. `--on` clears that project override without overriding the global
switch or cloud/package protections. `known --list` shows known export locations
and holds. Git exclusions cannot control unrelated backup or archive software.

## Deployment validation, after approval

1. Back up all live stores and learner state, then deploy/update the approved
   source using the existing release workflow.
2. Close Codex before its updater refreshes hooks. Reopen it, trust hooks if
   prompted, and verify that doctor reports the after-command hook installed.
   Do not edit a running Codex configuration to bypass the installer's deferral.
3. Verify normal sync, unresolved rejection reporting, backend pause status and
   successful quick/final learning in isolated synthetic sessions.
4. Review the historical recovery manifest, select source paths, and apply only
   after explicit live-data recovery approval. Verify recovery completion IDs
   and the resulting memory changes; a generic `done` status is insufficient.
5. Verify the explicitly approved maintenance review resets and subsequent
   maintenance. Do not clear unrelated reviews indiscriminately.
