# Catch-up schedules: missed occurrences are durable facts, resolved by policy

A weekly digest agent lives on a laptop that was closed for three weeks. Three
scheduled occurrences came due while nothing was running. On reopening, one
`zeta run` resolves the gap by explicit per-schedule policy: the agent with
`catchup: latest` fires exactly once, for the most recent missed occurrence,
with a journaled reason of `latest catch-up`; a second agent on the same cron
with the default policy does not fire, and its miss is itself recorded as a
durable `scheduler.tick.missed` event. Nothing is silently dropped, and
nothing is blindly replayed N times.

Zeta has exactly two catch-up policies, and this demo shows both:

- **default** (no `catchup` key): a missed occurrence is backfilled only later
  on the same calendar day. Across days it never fires — but the miss is
  journaled as a `scheduler.tick.missed` event with a reason, so it is
  queryable data, not an absence.
- **`catchup: latest`**: the single most recent missed occurrence stays
  eligible across days. It fires once, is anchored at schedule activation (an
  occurrence from before the schedule first existed never fires), and carries
  a per-occurrence idempotency key so it can never fire twice.

There is no "fire everything you missed" policy in Zeta. That is a design
position, not a gap: the demo's contrast is Zeta's two real policies against
each other and against plain cron, which gets this wrong in both directions.

## Why other frameworks can't do this

- **cron / systemd timers + an LLM script**: if the machine is off at the
  scheduled time, nothing fires and nothing records that it didn't. There is
  no durable occurrence record to even ask "what did I miss?". A naive
  "run-on-boot and loop over missed slots" script fires N stale runs.
- **anacron** approximates `catchup: latest` for daily/weekly jobs, but keeps
  only a private last-run timestamp file: no queryable record of the missed
  occurrences, no per-occurrence idempotency, no per-job policy choice
  expressed next to the job's definition.
- **LangGraph / CrewAI**: no scheduler at all — you bring your own cron, and
  inherit its failure modes above.
- **n8n**: the Schedule Trigger fires only while the n8n process is up;
  executions that would have happened while it was down are dropped without a
  record.
- **Temporal**: the honest half — Temporal Schedules do have a catch-up window
  and overlap policies, and durable history. But that requires a running
  server cluster; it is not a laptop-local runtime where two markdown files
  define the agents and a `zeta run` on wake-up settles the ledger. The part
  Temporal cannot offer here is the shape of the artifact: schedule decisions
  (`activated`, `published`, `skipped`, `missed`) living in the same
  append-only journal as the agent's events, runs, and outputs.

## How to run

Prerequisites: Python 3.11+, `uv`, and the Codex CLI logged in (`codex login`).
The script makes one live model call (the digest agent run, ~10–60 s).

```sh
./run.sh
```

Everything is created under `work/` (wiped at the start of each run), and all
runtime state is pinned there via `ZETA_STATE_DIR`, so the demo never touches
any other Zeta state.

Note on time simulation: the three-week gap is simulated by seeding, through
the public `zeta events publish` CLI (source `demo:time-simulation`), the two
durable facts a real run three weeks ago would have journaled — the schedule
activation anchor and the last published scheduler tick. The missed occurrence
that then fires is not simulated: it is the cron's real most recent
occurrence, yesterday at 09:00 UTC, which genuinely came and went with no
runtime running. Everything after the seeding step is real runtime behavior at
real wall-clock time.

## What you'll see and how to interpret it

1. **Scaffolding** — two agents on the same weekly cron (yesterday's weekday,
   09:00 UTC), differing only in the `catchup` key of their `schedules` entry.
   Policy is declared next to the schedule, in the agent file.
2. **Time simulation** — the seeded activation anchor and last-published tick,
   clearly labeled. This is the "laptop was last open three weeks ago" state.
3. **Schedule status before running** — both schedules show
   `last_published_at` three weeks ago and the next future fire time. The
   runtime already knows the ledger position before deciding anything.
4. **`zeta run`** — one pass: `processed 1`. Exactly one piece of work
   resulted from three missed occurrences across two agents.
5. **The digest** — `digest.md` contains exactly one line, written by the
   `catchup-latest` agent, dated with the missed occurrence's date (not
   today). The script asserts the count is 1 and fails otherwise.
6. **The durable record** — the journal holds the whole decision trail:
   `scheduler.tick.missed` for the default agent (reason: `previous-day tick
   not backfilled`), `scheduler.tick.published` for the latest agent with
   reason `latest catch-up` (the runtime states it knew this was a catch-up,
   not an on-time fire), the `agent.weekly-digest-latest.scheduled` occurrence
   event whose payload carries the intended occurrence timestamp, and the
   completed run in `zeta ps`. `zeta schedules status` now shows `missed` vs
   `skipped: already published`.
7. **Idempotence** — a second `zeta run` processes 0 items and `digest.md`
   still has one line: the occurrence's idempotency key makes reopening the
   laptop twice a no-op, not a duplicate digest.

One honest limit: with `catchup: latest`, the two older missed occurrences
(weeks 1 and 2) are not individually journaled as `missed` events — they are
inferable from the gap between the last published tick and the catch-up fire.
The individually-journaled `missed` record in this demo comes from the
default-policy agent's cross-day miss.

## Where else this principle applies

- **Billing and invoicing**: a monthly invoice job that missed month-end must
  run once with the intended period, not three times or zero times.
- **Report generation**: a weekly report fired on wake-up should be dated for
  the occurrence it represents — the payload's `date`/`timestamp` carry the
  intended occurrence, not the processing time.
- **Data pipeline backfills**: "which partitions were never built?" should be
  a query over durable occurrence records, not a guess from log files.
- **Notification and reminder systems**: a reminder service that was down must
  choose deliberately between dropping stale reminders (recorded) and sending
  the latest one — and never spam the whole backlog.
- **Edge and IoT devices**: intermittently-powered devices are the permanent
  version of the closed laptop; scheduled work must be reconciled by policy on
  every wake.
