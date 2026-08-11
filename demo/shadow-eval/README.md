# Continuous shadow evaluation on real production traffic

Your production prompts are a free, perfectly representative eval set — if you
can get them back. Zeta records every prompt an agent sends to the model as a
content-addressed, replayable object. This demo runs a small "production"
sentiment-tagging agent over three feedback events, then performs a shadow
pass: it enumerates the day's recorded prompts, replays each one against a
candidate configuration without touching production behavior, and publishes
the per-prompt match/divergence results as a typed `model.eval.report` event
in the same durable journal — where any downstream agent can react to it.

## Why other frameworks can't do this

The shadow pass depends on one thing most frameworks do not have: the exact
bytes of every production model request, stored as first-class, addressable
objects. LangGraph and CrewAI log what you tell them to log (or what an
observability integration like LangSmith samples); the prompt you can
reconstruct later is an approximation, so a replay tests a reconstruction,
not the workload. Zeta verifies the replayed payload against the recorded
hash — `payload verified` means you replayed the real request. Temporal gets
the durability half right: its event history can deterministically replay
workflow *code*, but the LLM call is an opaque activity result — Temporal
replays around the model call, never through it. n8n keeps execution logs
for debugging, not replayable model requests. The second half — the eval
result as a schema-validated event in the same journal that carries
production traffic, able to trigger downstream agents — is Zeta's event
model; in the other tools the eval report would live in an external system.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the demo makes live model calls
  (seven small ones: three production classifications, three replays, one
  evaluator turn)

```sh
./run.sh
```

Everything runs in a throwaway `work/` directory with its own
`ZETA_STATE_DIR`; nothing outside this demo folder is touched. A full run
takes on the order of a minute, depending on model latency.

## What you'll see and how to interpret it

- `The production agent` — a sentiment tagger that accepts
  `feedback.received` events and replies with one word. Its reply is its
  production output.
- `Publishing three production feedback events` / `zeta run` — the day's
  "production traffic". Each model call is recorded in the trace store as it
  happens; nothing extra is instrumented.
- `The day's production prompts, stored as addressable objects` —
  `zeta traces prompts` lists the three recorded prompts by content hash,
  with component counts and store-size stats. This is the eval set: no
  export, no sampling, no reconstruction.
- `Shadow pass` — for each prompt, `zeta traces replay <id> --diff`. The
  `payload verified` line means the request was rebuilt byte-for-byte from
  the stored component graph and hashes to what production sent. No diff
  lines means the candidate answered identically ("match"); a unified diff
  would show exactly how the candidate diverges on that production prompt.
  Only the built-in `codex` profile is configured here, so this is honestly
  a same-profile replay (expect matches): the mechanism, not the model pair,
  is what is being shown. With a candidate profile in `~/.zeta/models.toml`
  you would add `--model <profile>` to the same command for a cross-model
  shadow eval. (Known bug at time of writing: `--model codex` fails with
  `unknown model profile: codex` because the built-in profile is not in
  `models.toml`; omitting `--model` uses the active built-in profile.)
- `Publishing the batch results` / second `zeta run` — the script publishes
  the raw batch as `shadow.eval.requested`; a small evaluator agent picks it
  up and calls `publish_event` with the typed `model.eval.report`. The
  runtime validates that payload against
  `agents/events/model.eval.report.json` at the call — an invalid payload is
  rejected back to the agent. (The `returns:` mechanism would be the natural
  fit here, but it currently fails with the codex profile, so the evaluator
  uses `publishes:` instead.)
- `The typed report in the durable journal` — `zeta events list` shows the
  event with source `agent:shadow-evaluator` and the full validated payload:
  per-prompt statuses, counts, and a verdict enum. A downstream agent with
  `accepts: [model.eval.report]` could alert on `divergence_detected` or
  track the match rate over time.
- `Causal chain of the report` — the report is causally linked to the
  request that produced it, in the same journal as the production events.
- The closing section shows the `schedules:` frontmatter (`cron`,
  `timezone`, `catchup: latest`) that makes this a nightly job under
  `zeta serve`, with no external cron. In this demo the script drives the
  replay loop so the run is deterministic and inspectable; a scheduled
  evaluator granted the `bash` tool would run the same
  enumerate-replay-report loop itself.

A divergence in a real deployment means: on this exact production input, the
candidate configuration makes a different decision than production did. That
is the unit of evidence a migration or regression decision needs — per
prompt, with the diff attached, before any traffic moves.

## Where else this principle applies

- **Model migrations** — replay a week of traffic against the new model and
  read the divergence list before switching, instead of A/B testing on live
  users.
- **Prompt and system-prompt changes** — the same replay works when the
  candidate differs by instructions rather than by model; divergences show
  which production cases the new wording changes.
- **Regression gates in CI** — replay a pinned set of recorded production
  prompts on every change to agent definitions and fail the build on
  unexpected divergence.
- **Incident forensics** — when production misbehaves, replay the exact
  offending prompt with `payload verified` instead of arguing about what the
  model probably saw.
- **Compliance and audit** — the eval report, the replays, and the
  production traffic live in one durable, causally-linked journal, so "what
  did we test the candidate on" has a verifiable answer.
