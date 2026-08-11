#!/usr/bin/env bash
set -euo pipefail

# Missed schedules are durable facts with an explicit catch-up policy.
#
# Story: a weekly digest agent lives on a laptop that was closed for three
# weeks. On reopening, the `catchup: latest` agent fires exactly once for the
# most recent missed occurrence; the default-policy agent does not fire, and
# the miss is journaled as a durable `scheduler.tick.missed` event.

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
WORK="$DEMO_DIR/work"
PROJECT="$WORK/digest"

rm -rf "$WORK"
mkdir -p "$PROJECT/agents" "$PROJECT/.zeta"

# CRITICAL: pin runtime state to this demo's work dir so no zeta command can
# attach to the repo root's .zeta.
export ZETA_STATE_DIR="$PROJECT/.zeta"

zeta_cmd() {
  (cd "$PROJECT" && uv run --project "$REPO_ROOT" zeta "$@")
}

# BSD (macOS) date with a GNU fallback.
days_ago() {
  date -u -v-"$1"d "+$2" 2>/dev/null || date -u -d "$1 days ago" "+$2"
}

YESTERDAY="$(days_ago 1 %Y-%m-%d)"
YESTERDAY_DOW="$(days_ago 1 %w)"        # cron weekday of yesterday (0=Sunday)
YESTERDAY_DAYNAME="$(days_ago 1 %A)"
ANCHOR_DATE="$(days_ago 22 %Y-%m-%d)"   # three weeks before yesterday, same weekday
MISS_W2_DATE="$(days_ago 15 %Y-%m-%d)"
MISS_W1_DATE="$(days_ago 8 %Y-%m-%d)"

CRON="0 9 * * $YESTERDAY_DOW"
MISSED_AT="${YESTERDAY}T09:00:00+00:00"
ANCHOR_AT="${ANCHOR_DATE}T09:00:00+00:00"
ANCHOR_NEXT_AT="${MISS_W2_DATE}T09:00:00+00:00"

echo "==> [1/7] Scaffolding the weekly-digest project (two agents, same weekly cron)"
echo
echo "    cron: '$CRON'  (every $YESTERDAY_DAYNAME 09:00 UTC — most recent occurrence: yesterday)"
echo "    agent weekly-digest-latest   -> catchup: latest"
echo "    agent weekly-digest-default  -> no catchup (default: same-day backfill only)"
echo

write_agent() {
  local slug="$1" display="$2" policy_label="$3" catchup_line="$4"
  cat > "$PROJECT/agents/$slug.md" <<EOF
---
name: $display
description: Appends one line to digest.md when its weekly schedule fires.
session: shared
base_dir: $PROJECT
schedules:
- cron: "$CRON"
$catchup_line
tools:
- read
- write
---
The weekly digest occurrence scheduled for {{ event.payload.timestamp }} is due.

If digest.md exists, read it first. Then write digest.md so it contains its
previous content plus this exact new line at the end:

{{ event.payload.date }}: weekly digest (policy=$policy_label)

Do not change anything else and do not write any other file.
EOF
}

write_agent "weekly-digest-latest" "Weekly Digest (catchup latest)" "catchup-latest" "  catchup: latest"
write_agent "weekly-digest-default" "Weekly Digest (default policy)" "default" ""

echo "==> [2/7] TIME SIMULATION: seeding journal history from three weeks ago"
echo
echo "    The laptop-closed gap is simulated by seeding the two durable facts a"
echo "    real run three weeks ago would have journaled (source: demo:time-simulation):"
echo "      - the schedule activation anchor (first-observed at $ANCHOR_AT)"
echo "      - the last published scheduler tick ($ANCHOR_AT, for both agents)"
echo "    Everything after this point is real runtime behavior at real wall-clock time."
echo "    Occurrences that came due while 'closed': $MISS_W2_DATE, $MISS_W1_DATE, $YESTERDAY (all 09:00 UTC)."
echo

seed_published_tick() {
  local slug="$1"
  zeta_cmd events publish scheduler.tick.published \
    --source demo:time-simulation \
    --idempotency-key "scheduler:published:$slug:0:$CRON::$ANCHOR_AT" \
    --payload-json "{\"agent\":\"$slug\",\"schedule_index\":0,\"event_type\":\"agent.$slug.scheduled\",\"cron\":\"$CRON\",\"timezone\":null,\"scheduled_at\":\"$ANCHOR_AT\",\"observed_at\":\"$ANCHOR_AT\",\"next_at\":\"$ANCHOR_NEXT_AT\",\"status\":\"published\",\"reason\":\"due now\",\"published_event_id\":null}"
}

seed_published_tick "weekly-digest-latest"
seed_published_tick "weekly-digest-default"

zeta_cmd events publish scheduler.tick.activated \
  --source demo:time-simulation \
  --idempotency-key "scheduler:activated:weekly-digest-latest:0:$CRON::latest" \
  --payload-json "{\"agent\":\"weekly-digest-latest\",\"schedule_index\":0,\"event_type\":\"agent.weekly-digest-latest.scheduled\",\"cron\":\"$CRON\",\"timezone\":null,\"catchup\":\"latest\",\"observed_at\":\"$ANCHOR_AT\",\"status\":\"activated\",\"reason\":\"schedule first observed\"}"
echo

echo "==> [3/7] Schedule status on reopening the laptop (before any run)"
echo
echo "    Columns: agent, cron, status, last_published_at, next_at, reason."
echo "    Both schedules last published three weeks ago ($ANCHOR_DATE); the decision"
echo "    about the missed occurrence ($MISSED_AT) is journaled by the next run."
echo
zeta_cmd schedules status
echo

echo "==> [4/7] zeta run — one runtime pass resolves the miss by policy (live model call)"
echo
zeta_cmd run
echo

echo "==> [5/7] The digest: the latest-policy agent fired exactly once"
echo
if [ ! -f "$PROJECT/digest.md" ]; then
  echo "FAIL: digest.md was not written" >&2
  exit 1
fi
cat "$PROJECT/digest.md"
echo
LINES="$(grep -F -c "weekly digest (policy=" "$PROJECT/digest.md")"
if [ "$LINES" != "1" ]; then
  echo "FAIL: expected exactly 1 digest line, found $LINES" >&2
  exit 1
fi
if ! grep -qF "policy=catchup-latest" "$PROJECT/digest.md"; then
  echo "FAIL: digest line was not written by the catchup-latest agent" >&2
  exit 1
fi
if ! grep -qF "$YESTERDAY" "$PROJECT/digest.md"; then
  echo "FAIL: digest line is not for the latest missed occurrence $YESTERDAY" >&2
  exit 1
fi
echo "    OK: exactly 1 line, from weekly-digest-latest, for occurrence $YESTERDAY."
echo "    Three occurrences were missed; 'latest' fires only the most recent one."
echo "    weekly-digest-default did not fire (default = same-day backfill only)."
echo

echo "==> [6/7] The durable record: every scheduler decision is a journaled event"
echo
echo "--- scheduler decisions (zeta events list --type-prefix scheduler.tick) ---"
zeta_cmd events list --type-prefix scheduler.tick
echo
MISSED_JSON="$(zeta_cmd events list --type-prefix scheduler.tick.missed --json)"
if ! printf '%s' "$MISSED_JSON" | grep -qF "weekly-digest-default"; then
  echo "FAIL: no durable scheduler.tick.missed record for weekly-digest-default" >&2
  exit 1
fi
echo "    OK: the default agent's miss is recorded as scheduler.tick.missed"
echo "    (reason: previous-day tick not backfilled) — dropped, but never silently."
PUBLISHED_JSON="$(zeta_cmd events list --type-prefix scheduler.tick.published --json)"
if ! printf '%s' "$PUBLISHED_JSON" | grep -qF "latest catch-up"; then
  echo "FAIL: no scheduler.tick.published record with reason 'latest catch-up'" >&2
  exit 1
fi
echo "    OK: the fired tick's journaled reason is 'latest catch-up' (not 'due now'):"
echo "    the runtime knew this was a catch-up for a past occurrence, and said so."
echo
echo "--- the catch-up occurrence event itself (payload = intended occurrence) ---"
zeta_cmd events list --type-prefix agent.weekly-digest
echo
echo "--- runs (zeta ps): one run, triggered by the catch-up event ---"
zeta_cmd ps
echo
echo "--- schedule status now ---"
zeta_cmd schedules status
echo

echo "==> [7/7] Close and reopen the laptop again: zeta run is idempotent"
echo
zeta_cmd run
LINES="$(grep -F -c "weekly digest (policy=" "$PROJECT/digest.md")"
if [ "$LINES" != "1" ]; then
  echo "FAIL: second run refired the occurrence (found $LINES digest lines)" >&2
  exit 1
fi
echo
echo "    OK: digest.md still has exactly 1 line. The occurrence's idempotency key"
echo "    makes the second pass a no-op (journal shows 'skipped: already published')."
echo

echo "==> What just happened"
echo
echo "    Catch-up is a per-schedule policy over durable occurrence records, not a"
echo "    side effect of a process being awake:"
echo "      - catchup: latest  -> the most recent missed occurrence fired exactly ONCE,"
echo "                            anchored at activation, idempotent across runs."
echo "      - default          -> cross-day misses never fire, and the miss itself is"
echo "                            journaled as a scheduler.tick.missed event."
echo "    A plain cron would have either done nothing (machine was off, no record) or,"
echo "    naively replayed, fired 3 stale digests. Here missed work is data: recorded,"
echo "    queryable, and resolved by an explicit policy."
echo

echo "==> Inspect it yourself (run from $PROJECT)"
echo
echo "    export ZETA_STATE_DIR=\"$PROJECT/.zeta\""
echo "    uv run --project \"$REPO_ROOT\" zeta events list --type-prefix scheduler.tick --json"
echo "    uv run --project \"$REPO_ROOT\" zeta schedules status"
echo "    uv run --project \"$REPO_ROOT\" zeta ps"
echo "    cat \"$PROJECT/digest.md\""
