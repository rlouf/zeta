# Model migration with proof

Before switching a fleet of standing agents to a new model, you want to know
how the candidate behaves on *your* workload — not on a benchmark. This demo
runs a small ticket-classifier agent on three production events, then shows
that every prompt Zeta sent to the model is a stored, content-addressed
object: you can list the prompts, diff two of them component by component,
and replay one — byte-for-byte verified against the recorded payload hash —
through a different model profile, diffing the answers. That replay-and-diff
loop is the migration check: run the candidate on the exact requests
production made, and read the divergence line by line before any traffic
moves.

## Why other frameworks can't do this

The missing primitive elsewhere is the *rebuilt prompt as an addressable
object*. LangGraph and CrewAI assemble the model request in memory and hand
it to the provider; what you can log afterwards is an observability record
(LangSmith traces, callbacks), not an object the framework itself can rebuild
and resend with a verification that the bytes match. Temporal gets you half
of this: deterministic replay re-executes workflow code against recorded
activity results, so it can prove your *orchestration* is deterministic — but
the LLM call is an activity whose input Temporal treats as an opaque payload;
there is no component graph to diff and no first-class "resend this recorded
request against another model". n8n stores node execution data you can
inspect, but again as JSON blobs, with no payload-hash verification and no
replay-against-another-model command.

In Zeta, the prompt is a `prompt` object in the trace store that links the
exact components it was built from — system prompt, tool descriptors, each
timeline message — all content-addressed. `zeta traces replay` rebuilds the
request from that graph, verifies it hashes to what was actually sent, and
resends it; `zeta traces diff` compares two prompts by component identity.
Both are ordinary CLI commands over the store, not an add-on tracing product.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the demo makes live model calls
  (seven small ones: six agent steps plus one replay)

```sh
./run.sh
```

The script creates a fresh `work/` directory next to itself, scaffolds the
agent there, and pins `ZETA_STATE_DIR` inside `work/` so nothing outside the
demo directory is touched. A run takes well under a minute.

## What you'll see and how to interpret it

- **The agent under migration** — a one-file agent: it accepts
  `ticket.created` events, classifies each ticket as billing/bug/feature,
  writes the decision to `decisions/<id>.txt`, and replies with the category.
  `session: shared` gives it one timeline across all tickets.
- **Publishing three production ticket events** — three `ticket.created`
  events stand in for real production traffic.
- **`zeta run`** — drains the queue: three agent turns, six model requests
  (a tool call plus a final answer per ticket), each recorded in the trace
  store as it happens. No special "record mode" — this is how Zeta always
  runs.
- **Routing decisions** — the observable production output, so you can see
  the workload we are about to replay was real.
- **Stored prompts** — `zeta traces prompts` lists every recorded prompt
  with component counts and token estimates; `traces log --kind prompt`
  gives the short ids used below. Six prompts for six model requests.
- **`zeta traces show <id>`** — one prompt in full: its content-address, the
  components it links (each itself addressable), the `PromptBuilder`
  derivation that produced it, and the `ModelResponse` that consumed it.
  This is the object other frameworks don't have.
- **`zeta traces diff <old> <new> --stat`** — component diff between the
  first and last prompt of the session. `= 3 unchanged` means the system
  prompt, tool descriptors, and content manifest are byte-identical
  (same content address); the `~`/`+` lines are exactly the timeline growth.
  Drop `--stat` to get unified text diffs of the changed components.
- **`zeta traces replay <id> --diff`** — the migration check.
  `payload verified` is the load-bearing line: the request rebuilt from the
  component graph hashes to exactly what production sent. The diff that
  follows compares the recorded answer with the replayed one; here it is
  empty because the replay chose the same category. **Honesty note:** only
  the built-in `codex` profile is configured on this machine, so this is a
  same-model replay. With a candidate profile configured in
  `~/.zeta/models.toml`, adding `--model <candidate>` to this exact command
  performs the cross-model migration check — the mechanism is identical.
- **The replay is itself recorded** — `traces tree <id> --down` shows the
  prompt's forward derivations: the original `ModelResponse` and the new
  `ModelReplay` side by side. The migration experiment lives in the same
  auditable graph as the production run.

A migration decision would run this loop over the recorded prompts of every
agent in the fleet: replay each against the candidate profile, collect the
answer diffs, and look at where the candidate diverges — different tool
calls, different classifications, different tone — on the actual prompts the
fleet produced last week, before flipping the model profile.

## Where else this principle applies

Storing the exact rebuilt request as a verifiable, addressable object — not
a log line — pays off anywhere you need to re-ask a recorded question:

- **Prompt-template upgrades** — replay last week's prompts under the new
  template policy and diff prompts *and* answers, separating "the prompt
  changed" from "the model changed its mind".
- **Regression forensics** — when an agent misbehaves, diff the bad turn's
  prompt against the last good one component by component, then replay the
  suspect prompt to see if the failure reproduces.
- **Provider migration and failover** — the same replay check works when
  moving between API providers or self-hosted endpoints, not just between
  models.
- **Evaluation set mining** — recorded production prompts are an eval set
  that needs no synthetic data; content addresses deduplicate it for free.
- **Compliance and audit** — "what exactly did we send the model, and would
  today's system answer differently?" becomes two commands with
  hash-verified evidence rather than a log archaeology project.
