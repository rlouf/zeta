# Self-bootstrapping: the system grows a new agent through its own machinery

You ask for a new standing responsibility — "watch expense reports, flag any
over 5,000 USD" — by publishing an ordinary event. A scaffolder agent handles
that event and *authors a new agent* as a Markdown file in `agents/`. The next
`zeta run` picks the new agent up with no registration, restart, or deploy,
and it immediately handles real work. The creation itself is on the record
exactly like the work: a durable event, a run, a trace, and one causal chain
that connects the flagged expense back to the original request.

## Why other frameworks can't do this

In LangGraph and CrewAI, agents are Python objects constructed in code: adding
a responsibility means editing source, re-importing, and redeploying the
process; the creation leaves no trace in the system's own history. Temporal
can durably record that *something* happened, but workflows are compiled code
registered with workers — a workflow cannot author a new workflow type that
workers then execute; that requires a code deploy. n8n can create workflows
via its API, but the creation is an API mutation outside any execution record,
not an audited run of the system itself. In Zeta an agent *is* a Markdown
file, and `zeta run` re-reads `agents/` every invocation — so "grow a new
agent" is just a `write` tool call by another agent, with the same journal
entry, run record, and trace as any other work. The operator can `git diff`
the new agent before trusting it with more tools.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the demo makes two live model calls
  (one to author the agent, one for the new agent's first task), roughly
  10-60 seconds each

```sh
./run.sh
```

Everything runs inside `demo/self-bootstrapping/work/`, which is recreated on
each run and gitignored. Note: the built-in codex model profile is used
implicitly; passing `--model codex` fails with the current build (the built-in
profile is not listed in `~/.zeta/models.toml`).

## What you'll see and how to interpret it

- **Scaffolding** — the project starts with exactly one agent,
  `agents/scaffolder.md`. Event schemas for all four event types are
  pre-declared in `agents/events/` by the operator; the demo grows an agent,
  not unreviewed event contracts.
- **Step 1** — `agent.requested` is published with a payload describing the
  desired responsibility (slug, name, watched event, limit). This is the
  entire "change request": no config edit, no deploy.
- **Step 2** — `zeta run` gives the event to the scaffolder, which makes one
  `write` call (the new agent file) and one `publish_event` call
  (`agent.created`). This is a live model call.
- **Step 3** — the new file `agents/expense-watchdog.md` is printed in full.
  This is the artifact: frontmatter declaring what it accepts, publishes, and
  which tools it holds (only `write`), plus its instructions.
- **Step 4** — the script validates the generated file with Zeta's own spec
  parser (`zeta.authoring.spec.load_spec`) and asserts its accepts/publishes/
  tools. A malformed file fails the script here, before any second model call.
- **Steps 5-6** — an oversized expense report (`EXP-2107`, 23,400 USD) is
  published, linked with `--caused-by` to `agent.created`, and a fresh
  `zeta run` executes the minutes-old agent. Its prompt was authored by the
  scaffolder; its judgment (23,400 > 5,000) is the model's.
- **Step 7** — the new agent's output: the `flags/EXP-2107.md` file it wrote
  and the `expense.flagged` event it published.
- **Step 8** — `zeta events chain` walks from `expense.flagged` back through
  `expense.report.received` and `agent.created` to `agent.requested`: the
  system's growth and the work it produced sit in one audit trail. `zeta ps`
  shows both runs — the creator's and the created's — in the same process
  table.

The governance angle is deliberate: the generated agent holds only the `write`
tool. Because it is a plain reviewable file, an operator can read exactly what
it does — and diff, amend, or delete it — before granting it more capability.

## Where else this principle applies

- **Ops runbooks** — "page me when disk usage crosses 90%" arrives as a
  ticket-shaped event; the system grows the watcher, and the audit trail shows
  who asked for it and when it first fired.
- **Compliance monitoring** — a new regulation becomes a request event; the
  monitoring agent it produces carries a causal link to the mandate that
  created it.
- **Customer onboarding** — each new customer event scaffolds a
  per-customer triage agent from a vetted template, instead of redeploying a
  multi-tenant service.
- **Data quality** — an analyst's "flag rows where X" request becomes a
  standing checker whose creation, first catch, and every subsequent catch
  share one chain.
- **Personal automation** — "summarize invoices that land in this folder"
  becomes an ambient responsibility written by the assistant you asked,
  reviewable in the same folder as the ones you wrote yourself.
