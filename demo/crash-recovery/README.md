# Crash recovery: kill the runtime at any moment

This demo publishes three events that each trigger a summarizer agent, starts
the Zeta worker, and sends `kill -9` to its whole process group while an
attempt is mid-flight (a live model call is running). It then restarts the
worker and shows that no event was lost, the killed turn ran again to
completion, every event produced exactly one output, and re-publishing an
already-processed event with the same idempotency key is deduplicated by the
journal before any work is created.

Zeta is crash-only: recovery is not a separate code path, it is the normal
worker startup. Every fact the runtime needs — events, queue items, attempts,
claims — lives in a durable journal, and every identity below an event
(queue item, attempt, run, output) is a pure function of that event, so
re-execution collides with prior work instead of duplicating it.

## Why other frameworks can't do this

- **LangGraph / CrewAI** hold orchestration state in the Python process (or an
  optional checkpointer that the user must wire up and that checkpoints graph
  state, not an event journal). `kill -9` mid-run loses in-flight work, and
  re-submitting the same trigger creates a second run with new outputs. There
  is no ingress-level idempotency key.
- **Temporal** does get the durability half right: workflow history is durable,
  workers are stateless, and a killed worker's tasks are retried after a
  timeout — that part of this demo Temporal can match. What it does not give
  you is this demo's identity model: Zeta derives queue item, attempt, run,
  and effect identities deterministically from the event, and deduplicates at
  event ingress with a plain `--idempotency-key`, so "publish, crash, restart,
  re-publish" converges without any application-level workflow-id discipline.
  In Temporal the equivalent guarantees exist only if you design workflow ids
  and activity idempotency yourself.
- **n8n** executions that die mid-flight are simply marked crashed; re-running
  a webhook trigger re-executes the workflow and repeats its side effects.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the summarizer makes live model calls

```sh
./run.sh
```

The script is self-contained: it recreates `work/` from scratch each time and
never touches anything outside this directory. One run takes about two
minutes; most of it is the 60-second claim lease of the killed worker expiring
(see below) plus four short model calls.

Note on timing: the script kills the worker only after it has observed a queue
item in `claimed` state, then verifies after the kill that the item is still
claimed. The demonstrated crash therefore always lands between claim and
completion — during the attempt's model call in practice. If the attempt
somehow completes inside the ~3s kill window, the script refuses to continue
rather than pretend, and asks you to re-run.

## What you'll see and how to interpret it

- `==> Publishing three note.created events` — each `zeta events publish`
  carries `--idempotency-key note:<name>` and prints a durable `evt_` id. The
  events are facts in the journal before any worker exists.
- `==> Starting the worker ... kill -9 mid-attempt` — the script polls
  `zeta queue list --json` until an item is `claimed`, waits 3 seconds so the
  attempt's model call is in flight, then `kill -9`s the worker's process
  group. Nothing gets to flush or clean up.
- `==> Durable state right after the crash` — `zeta queue list` still shows
  the dead worker's item as `claimed` and the untouched items as queued;
  `zeta attempts list` shows the killed attempt with no terminal state. This
  is the point: the crash lost a process, not state.
- `==> Restarting the worker` — the same `zeta run` command is invoked in a
  loop. Items the dead worker never claimed complete immediately. The
  in-flight item is protected by a 60s claim lease (fencing: a slow worker
  must not be silently preempted), so the transcript shows a short wait until
  the lease expires, after which the item is reclaimed as attempt #2 and
  completes. No operator action, no recovery command, no special mode.
- `==> Final state` — every queue item is `completed`, the killed attempt
  remains visible in history next to its successful retry, and `summaries/`
  contains exactly one file per note. The killed attempt may have partially
  executed — that is at-least-once processing — but outputs have stable
  identities (one path per event), so re-execution converges to exactly one
  artifact.
- `==> Re-publishing an already-processed event` — publishing the same event
  with the same idempotency key prints `already published` with the original
  `evt_` id. The script verifies the id matches, that `zeta run` reports
  `processed 0`, and that the summary files are byte-identical. Deduplication
  happens at journal ingress, before routing, queueing, or any model call.

## Where else this principle applies

- **Payment and order processing** — the same publish/crash/re-publish
  sequence occurs when a client retries a "charge card" request after a
  timeout; ingress idempotency keys plus retry-stable effect identities are
  what prevent double charges.
- **Webhook consumers** — GitHub, Stripe, and Slack all deliver webhooks at
  least once; a journal that dedupes on delivery id makes redeliveries free.
- **Email and notification senders** — a worker killed between "send" and
  "record sent" must not send twice on retry; deterministic effect identities
  make the retry collide with the first send.
- **Data pipelines** — re-running a failed ETL step must overwrite the same
  partition, not append a second copy; that is the same "output identity is a
  function of the input" rule the summarizer uses here.
- **Infrastructure reconcilers** (Kubernetes-style controllers) — crash-only
  design with durable desired state and idempotent apply is exactly this
  recovery model applied to infrastructure.
