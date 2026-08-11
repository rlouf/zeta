# Derivation cache: a crashed work item never re-pays for derived results

An ingestion agent turns source documents into derived artifacts (one-sentence
summaries) using Zeta's content tools. Every artifact — source, prompt, model
answer, summary — is stored in a content-addressed substrate at the BLAKE3
address of its bytes, linked by derivation records that say exactly what
produced it. The demo shows two consequences of giving computation an
identity:

1. **Identity dedup across runs.** Re-ingesting the same document produces the
   same address, so the store keeps exactly one copy, and every summary ever
   derived from it points at that one object.
2. **Retry reuse across crashes.** A model transform is keyed by
   (work item, input object ids, transformation, destination, model). The demo
   kills the worker with SIGKILL right after a summary is computed but before
   the work item completes. After the queue lease expires, the retried attempt
   resolves the transform by that identity and reuses the recorded model
   answer — no second model call — and the final summary object links to the
   model response produced by the attempt that died.

## Why other frameworks can't do this

- **Temporal** does cover half of this: on worker crash, workflow replay skips
  completed activities by reading their recorded results from the execution's
  event history. But that memoization is positional — keyed by workflow
  execution and activity ordering — not by the identity of the computation.
  Results are opaque payloads in one execution's history: there is no
  content-addressed store, so identical artifacts across executions are stored
  again, and there is no derivation graph you can query to ask "what produced
  this object, from which inputs, with which instruction and model".
- **LangGraph / CrewAI** checkpointers snapshot mutable graph/agent state and
  restore it. Restoring state is not the same as identifying computation: two
  runs over identical inputs share nothing, and a restored checkpoint tells
  you where you were, not how any artifact was derived.
- **n8n** retries re-execute nodes; there is no notion of a result that is
  already known by identity.

In Zeta the cache is not a feature bolted onto the runtime — it falls out of
the substrate. Objects are immutable and addressed by their content, every
derived object records its producer and inputs, and the transform's identity
key is itself a content address. "Have I already computed this?" is a lookup,
not a replay.

## How to run

Prerequisites: Python 3.11+, `uv`, the Codex CLI logged in (`codex login`),
and the `sqlite3` command-line tool (preinstalled on macOS).

```sh
./run.sh
```

The script makes live model calls (several agent runs, each ~15-30s) and
deliberately waits ~65s for a queue lease to expire after killing the worker,
so a full run takes roughly 3-5 minutes. All state lives in `work/`, which is
recreated on every run.

## What you'll see and how to interpret it

- `==> Pass 1` publishes `docs.ingest` events for two documents. The agent
  stores each document verbatim as a content node and derives a summary with a
  `transform_content` model transformation. The object listing shows BLAKE3
  (`b3:`) addresses: the id of each object is the hash of its content.
- `==> Derivation graph for summary/alpha` walks the recorded derivations:
  summary node ← ModelTransform ← assistant message ← ModelResponse ← prompt ←
  the source document node. This provenance is stored data, not logging.
- `==> Pass 2` republishes the *same* two documents as new events. The source
  addresses printed for pass 1 and pass 2 are identical — the store kept one
  copy of each source ("IDENTITY" line). The summaries, however, are computed
  again, and the script says so: **a republished event is new work**. Zeta's
  transform cache is scoped to the work item (the queue item that binds one
  event to one agent); it is retry reuse, not a global cross-run model cache.
  The demo does not pretend otherwise.
- `==> Pass 3` publishes a third document, waits (polling the substrate) until
  the summary transform's result has been recorded, then SIGKILLs the whole
  worker process tree before the attempt can complete.
- `==> Crash recovery` waits out the 60s queue lease and runs the worker
  again. Zeta reclaims the orphaned work item and starts attempt 2 of the
  *same* queue item. The evidence printed:
    - the count of summarization model calls (ModelResponse derivations
      recorded by `transform_content`) is **unchanged** by the recovery run;
    - the model answer recorded by the killed attempt and the answer linked by
      the final summary are the **same object id**. Attempt 2 found the result
      by identity and never called the model for it.
- `==> Attempts` shows the same queue item twice: the killed attempt 1 (it
  never reached a terminal state, so it still displays as `running`) and the
  completed attempt 2. The recovery run also finishes visibly faster than the
  original runs (~7s vs ~20-30s in our runs) because the only remaining model
  calls are the agent's own loop turns, not the summarization.

### Honest scope and caveats

- The reuse boundary is the work item. Attempt retries of one queue item reuse
  recorded transforms; separately published duplicate events recompute. If you
  need duplicate events to collapse, that is what connector idempotency keys
  are for (dedup at ingress), composing with the retry reuse shown here.
- The transform identity includes the transformation arguments byte for byte.
  The retried agent must issue the same call again; the agent template pins
  the exact arguments to make that reproducible. If the model deviates on the
  retry, the script detects it, reports that no reuse was observed, and exits
  nonzero rather than faking a hit.
- `zeta traces tree`/`show` are session-scoped, and content-workspace
  derivations are recorded without a session id, so the script reads the
  substrate SQLite store directly (read-only, `inspect_substrate.py`) to count
  model calls and render the derivation graph.
- The demo omits `--model`: the built-in `codex` profile is active by default,
  and naming it explicitly fails (`unknown model profile: codex`, a known
  issue in this build).

## Where else this principle applies

- **Data/ML pipelines**: feature extraction, embedding, and labeling jobs that
  crash mid-batch resume without re-paying for completed items, because each
  derived artifact has an identity, not a row in a job table.
- **Build systems for agents**: the same property that makes Bazel/Nix
  incremental — content-addressed inputs and recorded derivations — applied to
  model calls, so "rebuilds" of agent work skip everything already derived.
- **Audit and reproducibility**: any regulated pipeline (finance, healthcare)
  can answer "which source, prompt, instruction, and model produced this
  artifact" by reading the derivation graph instead of reconstructing logs.
- **Cost control in fan-out workloads**: map-style transforms over hundreds of
  documents can be killed and resumed freely; only the missing leaves are ever
  recomputed.
- **Multi-agent dedup**: agents that independently store identical
  intermediate artifacts converge on the same addresses, so shared storage and
  provenance come for free instead of via an explicit coordination layer.
