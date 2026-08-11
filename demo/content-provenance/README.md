# Content provenance: every published sentence traceable to its source

A content pipeline — meeting transcript, summary, published note — where each
step is an immutable, content-addressed object in Zeta's trace substrate,
linked by recorded derivations. When someone asks "which model call produced
this sentence, and from what input?", the answer is read from storage, not
reconstructed from logs. Each derived object names its producer (for example
`ModelTransform:v1`) and the exact input objects it was built from; each model
response links to the exact prompt object that was sent; the prompt's
components link back to the content-addressed source. This is the level of
provenance that AI-disclosure rules are starting to demand.

## Why other frameworks can't do this

- **LangGraph / CrewAI** trace runs (LangSmith spans, callbacks). Those are
  log lines: mutable rows keyed by run id, correlated after the fact. There is
  no content-addressed object graph, so "this paragraph came from that model
  call over that input" is an inference over telemetry, not a stored fact —
  and nothing detects tampering with the record.
- **Temporal** durably records workflow history, so it can honestly answer
  "which activity ran with which arguments". But payloads are opaque blobs in
  history, not hash-addressed objects with derivation edges between them; you
  would build the content graph, the addressing, and the query tools yourself.
- **n8n** keeps per-node execution data for debugging, with no immutability or
  lineage guarantees at all.

In Zeta the substrate is part of the runtime: every prompt component, prompt,
model response, and content revision is stored under its BLAKE3 content
address, and `Derivation` records connect them. `zeta traces tree`, `show`,
`closure`, and `replay` query that graph directly. Because addresses are
content hashes, altering any object after the fact changes its address and
breaks every downstream link — the provenance chain is tamper-evident by
construction.

## How to run

Prerequisites:

- Python 3.11+ and `uv`
- Codex CLI logged in (`codex login`) — the run makes live model calls
  (the agent turn plus two recorded model transformations; expect one to a
  few minutes)

```sh
./run.sh
```

Everything the script creates lives under `demo/content-provenance/work/`,
which is recreated on every run.

## What you'll see and how to interpret it

1. **Scaffolding** — `zeta new` creates a project in `work/pipeline`; the
   `note-writer` agent is generated from `note-writer.md.tmpl` and the fake
   meeting transcript is copied to `source/transcript.txt`.
2. **Publishing the trigger event** — `zeta events publish transcript.ready`
   enqueues the work durably.
3. **Running the agent** — `zeta run` drains the queue. The agent stores the
   transcript verbatim as a content object (`ContentLiteral:v1`), then derives
   `summary` and `note` objects through two `transform_content` model calls.
   Each of those calls stores its own prompt object (with the input content
   object as a component), the raw model response, and a derivation naming
   producer, inputs, and the instruction used.
4. **`zeta traces log`** — the raw store contents: every prompt, assistant
   message, tool call/result, and content-workspace snapshot the run
   recorded, each with its short content address.
5. **The provenance walk** — the script answers the provenance question with
   stored data only: it takes the object id the agent's `finish` call named,
   reads the note object (`traces show --json`), and follows its stored
   links: note → (summary object, model response #2), response → prompt,
   summary → (transcript object, model response #1). It prints the full
   `b3:` address of every hop, plus the derived texts.
6. **`zeta traces show <prompt>`** — the exact model call that produced the
   summary, component by component: system instruction, the
   `content_transform_input` component carrying the content-addressed
   transcript, and the one-line instruction. This is what the model saw,
   verifiable against the stored request hash.
7. **`zeta traces tree`** — the derivation graph around the note's
   transform call: `tool_call ← ToolCallProjection ← assistant_message ←
   ModelResponse ← prompt ← PromptBuilder ← components`. Every line under a
   producer is an exact recorded input, not a correlation guess.
8. **`zeta traces closure <note>`** — every object reachable from the note
   (twelve objects in a typical run: two content nodes, two prompts, two
   responses, and their components): a complete, self-contained audit set
   you could hand to a reviewer.
9. **Interpretation** — each id is a `b3:` BLAKE3 hash of the object's
   content. Log lines can be edited or lost without anything noticing; here,
   changing any byte of any object changes its address and severs every
   downstream reference. The chain is verifiable, not merely asserted —
   `zeta traces replay` can even rebuild a stored prompt, verify it against
   the recorded payload hash, and resend it.

The two derived texts are produced by live model calls, so their wording
differs between runs; the structure of the graph does not. Note the
content-addressing at work: the transcript object and the summary-prompt
object get the same `b3:` address on every run (same bytes, same address),
while the model responses and everything downstream of them differ.

One current limitation, stated honestly: content-workspace derivation edges
(`ModelTransform:v1`, `ContentLiteral:v1`) are recorded session-neutral, and
the session-scoped `traces tree`/`show` views do not display them yet. The
walk therefore follows the objects' stored links (which encode the same
inputs), and `traces tree` is demonstrated on the agent-turn part of the
graph, where derivation edges are session-scoped.

## Where else this principle applies

- **Regulated document generation** (finance, legal, medical): show an auditor
  the exact model call and exact source excerpt behind every generated clause.
- **AI-disclosure compliance**: labeling requirements increasingly ask which
  parts of a publication are model-generated and from what input — here that
  is a stored graph, queryable per sentence.
- **Data pipelines / feature stores**: the same derivation records give
  dataset lineage — which transformation over which input snapshot produced
  this training table.
- **Incident forensics for agents**: when an agent publishes something wrong,
  walk the stored graph to the prompt that caused it instead of grepping
  logs, then `traces replay` it against a fixed model to test a remedy.
- **Reproducible research**: figures and claims addressed by content hash,
  each linking to the exact inputs and the exact model or program that
  produced them.
