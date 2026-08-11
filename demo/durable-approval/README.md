# Durable approval: a human-in-the-loop wait that survives restarts

An agent files a purchase request and calls `wait_for` to pause until a
`purchase.approved` event arrives. The run ends, the process exits, and the
wait exists only as rows in the project journal. Later — after any number of
restarts, with no daemon alive in between — the approval is published as a
plain journal event; the next `zeta run` resumes the same agent session and
completes the workflow. Because the approval is an event in the same journal
as everything else, the audit trail is one causal chain: who approved,
what they approved, and every runtime step in between.

## Why other frameworks can't do this

- **Temporal** does have durable waits — signals delivered to a parked
  workflow survive worker restarts. That half is not the differentiator. The
  difference is what the approval *is*: in Temporal, a signal is an entry in
  one workflow's private event history, readable through Temporal's API. In
  Zeta, the approval is a first-class event in the project journal, with its
  own `source`, payload, and `caused_by` link — the same journal that records
  the agent's attempts, publications, and the wait lifecycle
  (`runtime.wait.created` / `runtime.wait.matched`). One `zeta events chain`
  walks from the completion back through the approval to the original
  request. Also, Temporal waits live in workflow code you wrote; here the
  *agent* decides to wait, by calling a tool mid-conversation.
- **LangGraph** supports human-in-the-loop interrupts with a checkpointer,
  but resuming requires your application process to reload the checkpoint and
  call the graph again with the resume value; the approval itself is an
  argument to a function call, not a durable record with provenance.
- **CrewAI** has no durable wait primitive; a human-input step blocks a live
  process.
- **n8n** "wait for webhook" nodes persist across restarts, but the approval
  arrives as an HTTP call whose content is only retained as execution data,
  not as a causally linked record you can query alongside every other action
  the system took.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the demo makes two live model calls

```sh
./run.sh
```

Everything runs inside `work/` (recreated on each run). One full run takes
about 1-2 minutes, most of it the two model calls.

## What you'll see and how to interpret it

- **Step 1** publishes `purchase.requested` with `zeta events publish`. This
  is the only input; there is no long-running orchestrator.
- **Step 2** runs `zeta run`. The procurement agent handles the event,
  publishes `purchase.filed`, and calls the `wait_for` tool with
  `{event_type: purchase.approved, fields: {request_id: REQ-1001}}`. The tool
  ends the run; `zeta run` drains the queue and exits.
- **Step 3** proves the wait is durable state, not process memory:
  `zeta waits list` shows the `active` wait, `zeta ps` shows the first run
  `completed` (the agent is parked, not running), and `lsof` shows no process
  holds the project journal open. There is nothing to keep alive.
- **Step 5** publishes `purchase.approved` from a different source
  (`approval-portal`), with `--caused-by` pointing at the original request.
  Storing this event matches the wait in the same transaction
  (`runtime.wait.matched`) and queues a continuation for the same agent
  session.
- **Step 6** starts a brand-new `zeta run` process, which executes the
  continuation. The agent sees the approval payload and publishes
  `purchase.completed`.
- **Step 7** shows the wait now `matched`, both runs `completed`, and the
  final run summary ("Purchase request REQ-1001 is complete.").
- **Step 8** is the audit angle: `zeta events chain` on the
  `purchase.completed` event prints the full causal chain —
  `purchase.requested` → `purchase.approved` → `runtime.wait.matched` →
  `runtime.attempt.completed` → `purchase.completed`. The approver's identity
  sits in the same verifiable record as the agent's own steps.

## Where else this principle applies

- **Expense and access approvals**: any workflow where an agent must stop for
  a human decision and the decision itself must be auditable (SOX, ISO,
  internal controls).
- **Deployment gates**: an agent prepares a release, waits for a
  `deploy.approved` event, and the audit trail shows who unblocked
  production.
- **Customer escalations**: a support agent waits for a human reply or a
  customer confirmation event for days without holding any process open.
- **Inter-agent coordination**: one agent waits for an event another agent
  publishes; the journal records the full cross-agent causal chain.
- **Incident management**: an agent pages a human, waits with a deadline
  (`wait_for` accepts one), and the timeout itself becomes a journaled event
  that resumes the agent on an escalation path.
