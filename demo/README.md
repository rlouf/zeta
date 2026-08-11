# Zeta demos

Sixteen self-contained demonstrations of what Zeta's durable runtime makes
possible. Each demo is one directory with one entry point: its `run.sh` builds
an isolated project under `work/`, drives real agents against the live model,
and prints a narrated transcript that ends with commands you can run to inspect
the durable state yourself. Each directory's `README.md` explains what the demo
proves, how to interpret the output, and where else the principle applies.

The common thread: things other frameworks treat as ephemeral — a prompt, a
tool result, a handoff, a wait, a schedule miss — have durable identity in
Zeta, so they can be verified, diffed, replayed, promoted, or restored later.

## Prerequisites

- Python 3.11+ and `uv`
- The Codex CLI, logged in once with `codex login`
- Every demo makes live model calls; tasks are kept tiny on purpose

Run any demo from its directory:

```sh
cd demo/crash-recovery
./run.sh
```

Demos are isolated: each pins `ZETA_STATE_DIR` into its own `work/` directory,
which is recreated on every run and ignored by Git.

## The demos

### Trust and forensics

| Demo | What it shows | ~Time |
|---|---|---|
| [`incident-forensics`](incident-forensics/) | Root-cause a wrong agent output from durable state: causal chain → exact stored prompt → the bad skill rule; fix it and a component diff proves the skill was the only delta. | 20 s |
| [`content-provenance`](content-provenance/) | Transcript → summary → note, every step a content-addressed object linked by derivations. "Which model call produced this sentence, from what input?" is answered by reading stored objects. | 35 s |
| [`durable-approval`](durable-approval/) | A human-in-the-loop wait that survives with no process alive, and an approval that joins the same causal record as the agent's own steps. | 20 s |
| [`verified-audit-trail`](verified-audit-trail/) | The journal as evidence: hash-chain verification over an approvals clerk's history. A one-byte tamper and a deleted tail are each caught with the exact structured divergence; "tamper-evident" is claimed only relative to an external anchor. | 30 s |
| [`notarized-deliverable`](notarized-deliverable/) | Ship a report with a provenance bundle a client verifies offline — a standalone script (stdlib + `blake3`, no zeta) recomputes every content, object, derivation, and chain address down to the anchored head. | 25 s |

### Replay

| Demo | What it shows | ~Time |
|---|---|---|
| [`model-replay-diff`](model-replay-diff/) | Rebuild a stored production prompt byte-for-byte (`payload verified`), resend it, and diff the results component by component — the mechanism behind migrating models on your own workload. | 20 s |
| [`shadow-eval`](shadow-eval/) | Replay the day's real traffic as a free eval set and file a schema-validated `model.eval.report` event, without touching production behavior. | 20 s |
| [`agent-ci`](agent-ci/) | CI for agent definitions: replay a frozen corpus of real triggers through a candidate definition. A cosmetic edit replays green; a rules change replays red with a per-ticket outcome diff as the review artifact. | 60 s |

### Durable computation

| Demo | What it shows | ~Time |
|---|---|---|
| [`crash-recovery`](crash-recovery/) | `kill -9` the worker mid-model-call: claims, queue items, and attempts survive; recovery is the normal startup path; outputs exist exactly once; the journal dedupes idempotent republish. | 70 s |
| [`derivation-cache`](derivation-cache/) | A crashed transform recovers from its recorded result without a new model call (7 s vs 39 s), and identical content dedupes to one stored object across runs. | 3.5 min |
| [`memory-promotion`](memory-promotion/) | Session scratch stays scratch; conclusions promote to durable scope inside the attempt-completion transaction; a stale writer is rejected with a real `ContentConflict`. | 20 s |

### Coordination and self-extension

| Demo | What it shows | ~Time |
|---|---|---|
| [`typed-handoff`](typed-handoff/) | Two agents hand off through a JSON-Schema event contract; a malformed payload is bounced back to the model with the exact validation error, and causality is traceable across the boundary. | 30 s |
| [`catchup-schedules`](catchup-schedules/) | A genuinely missed weekly occurrence fires exactly once under `catchup: latest`; under the default policy the miss itself is journaled. Missed work is data, not a silent gap. | 10 s |
| [`fleet-sre`](fleet-sre/) | Dead-letters, retries, and unhandled events diagnosed by a meta-agent querying the same durable state the operator sees; its incident report is itself on the record. | 25 s |
| [`self-bootstrapping`](self-bootstrapping/) | An agent authors a new standing agent from a request event; the creation is reviewable, revertable, and on the causal chain like everything else. | 25 s |
| [`skill-evolution`](skill-evolution/) | A self-tuning agent amends its own policy skill after a confirmed mistake. Every revision is content-addressed and pinned by the runs that used it, and any historical revision is restored — hash-verified — from the prompt store alone. | 50 s |

## Honesty notes

Each README states exactly what its demo does and does not prove. Where a demo
works around a current limitation of the runtime, the README says so and cites
the code. The limitations found while building these demos are filed as
[issues #22–#33](https://github.com/rlouf/zeta/issues).
