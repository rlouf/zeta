# Zeta Prompt Trace

Zeta prompt trace treats prompts as durable object graphs instead of temporary
chat message lists.

The goal is not only to know what the model said. The goal is to know what was
put in front of the model, why it was there, what it came from, and how the
next turn reused it.

This makes a Zeta run inspectable after the fact. It also gives us a substrate
for prompt diffs, replay, context compaction, freshness checks, and future
model-controlled context management.

## Core Idea

Every model prompt is an object derived from other objects.

Those input objects are prompt components: the system prompt, user objective,
project context, timeline messages, tool descriptors, tool calls, tool
results, and any transformed context components.

The prompt object stores the exact final model payload. That means the stored
prompt is not an approximation of the request. It is the request: messages,
tools, tool choice, model, max tokens, and model options.

The model answer is another object derived from that prompt.

```text
prompt components --> prompt object --> assistant_message object

derivation: SigilPromptBuilder:v1
derivation: SigilModelResponse:v1
```

This graph is the central abstraction. Zeta session continuity points into the
trace store with refs such as `run/<id>/head`; the user-visible chat history is
a projection from trace objects, not the primary artifact.

## Objects And Derivations

The trace store has three conceptual primitives.

**Objects** are content-addressed records. They have a kind, schema, JSON data,
and ordered links to other objects.

Examples:

- `system_prompt`
- `user_objective`
- `project_context`
- `transcript_message` for messages projected from the run timeline
- `tool_descriptor_set`
- `prompt`
- `assistant_message`
- `tool_call`
- `tool_result`
- transformed context objects such as `compacted_context` or `task_state`

**Refs** are mutable names that point at object ids. They let us talk about
things like the current prompt or current system prompt without pretending
those objects are mutable.

Examples:

- `prompt/current`
- `prompt/current/system_prompt`
- `prompt/current/user_objective`
- `run/<id>/head`

The run head names the latest meaningful trace leaf for a run. In a completed
model turn this is usually an `assistant_message`. During an interrupted shell
handoff it may temporarily be a `tool_result`, because that is the leaf the
next prompt must continue from.

**Derivations** record how an object was produced. They contain the producer
name, input ids, resolved refs, and parameters.

Examples:

- `SigilPromptBuilder:v1` creates a `prompt` from component objects.
- `SigilModelResponse:v1` creates an `assistant_message` from a prompt.
- `SigilToolCallProjection:v1` projects a tool call from an assistant message.
- `SigilToolExecution:v1` records a tool result from a tool call.
- `PromptStructuralTrim:v1` or `PromptTaskStateExtractor:v1` can create
  transformed context components.

## One Model Turn

In a simple turn, Zeta collects prompt inputs, builds a prompt object, sends it
through the model boundary, and records the assistant message.

```text
  system_prompt
  user_objective
  timeline_history
  tool_descriptors
        |
        v
  +---------------+
  | PromptBuilder |
  +---------------+
        |
        v
  +---------------+       +-----------+       +----------------------+
  | prompt P1     | ----> | ModelCall | ----> | assistant_message A1 |
  | exact payload |       +-----------+       +----------------------+
  +---------------+
```

`PromptBuilder` is not just rendering text. It records the component objects,
builds the exact model payload, stores the prompt object, and records the
derivation from components to prompt.

`ModelCall` is the runtime boundary where the prompt is sent to the configured
OpenAI-compatible model endpoint. The resulting assistant message is stored as
an object linked back to the prompt.

## Tool Feedback Loop

When the assistant asks for a tool call, that call and its result become part
of the trace. The result then becomes input to the next prompt.

```text
  +-----------+       +-----------+       +----------------------+
  | prompt P1 | ----> | ModelCall | ----> | assistant_message A1 |
  +-----------+       +-----------+       | tool call C1         |
                                          +----------------------+
                                                     |
                                                     v
                                              +----------+
                                              | ToolCall |
                                              +----------+
                                                     |
                                                     v
                                              +----------------+
                                              | tool_result R1 |
                                              +----------------+
```

Conceptually:

```text
assistant_message A1 --> tool_call C1 --> tool_result R1
```

The tool result is then fed back into prompt construction.

```text
Turn 2
======

  system_prompt
  user_objective
  timeline_history
  assistant_message A1
  tool_result R1
  tool_descriptors
        |
        v
  +---------------+
  | PromptBuilder |
  +---------------+
        |
        v
  +---------------+       +-----------+       +----------------------+
  | prompt P2     | ----> | ModelCall | ----> | assistant_message A2 |
  | exact payload |       +-----------+       +----------------------+
  +---------------+
```

The important loop is:

```text
A1 + R1 become prompt components for P2
```

That is what turns an agent run from a linear timeline into a graph of
causes.

## Two Turns As A Trace Graph

The same flow can be viewed as a derivation graph.

```text
Turn 1
------

 system_prompt      \
 user_objective      \
 timeline_msg         +--> PromptBuilder --> prompt P1 --> ModelCall --> A1
 tool_descriptors    /          |                                      |
                            derivation:                                v
                         SigilPromptBuilder:v1                   tool_call C1
                                                                        |
                                                                        v
                                                                    ToolCall
                                                                        |
                                                                        v
                                                                 tool_result R1


Turn 2
------

 system_prompt        \
 user_objective        \
 timeline_history       \
 assistant_message A1    +--> PromptBuilder --> prompt P2 --> ModelCall --> A2
 tool_result R1         /          |
 tool_descriptors      /      derivation:
                         SigilPromptBuilder:v1
```

Each prompt points to the components used to build it. Each assistant message
points to the prompt that produced it. Tool results point back to tool calls,
which point back to the assistant output that requested them.

## Why This Matters

### Exact Prompt Accountability

Without prompt objects, debugging often starts with an unanswerable question:
"What exactly did we send to the model?"

With prompt trace, the answer is an object lookup. The prompt object stores the
exact final payload, including all messages and tool descriptors. That makes it
possible to inspect a bad answer by looking at the real model input rather than
reconstructing it from logs and assumptions.

This is especially important as prompt construction becomes more dynamic. A
system prompt can depend on tools, available skills, project context, current
turn events, transforms, and model settings. The trace records the final result
of all of that.

### Replay

There are two useful meanings of replay.

**Prompt replay** is deterministic. Given a run head, prompt components, refs,
and builder settings, we can rebuild the prompt payload. Given a prompt object
id, we can inspect the exact payload that was actually sent.

**Model replay** means sending a payload to a model again. That is useful, but
not guaranteed to reproduce the same assistant output unless the model,
backend, parameters, and sampling behavior are deterministic and unchanged.

The trace still makes model replay much more useful because it separates the
prompt question from the model question:

```text
Did the model fail because the prompt was wrong?
Or did it fail despite a good prompt?
```

Prompt trace lets us answer the first question directly.

It also enables alternate replay. For example, we can start from the same run
head with a different prompt transform policy and ask:

```text
What prompt would this run have produced with no context transform?
What prompt would it have produced with a different system prompt?
What changed between these two prompt payloads?
```

That is the basis for prompt diffs and controlled prompt experiments.

### Context Compaction

Compaction is much easier when prompts are component graphs.

The unsafe way to compact context is to rewrite a rendered message list. That
loses provenance and makes it hard to know what was removed.

The trace-friendly way is:

```text
source components --> transform --> replacement component
```

For example:

```text
raw read/grep tool_result R1 --> structural trim --> compacted_context C1
```

The prompt can include `C1` while `C1` links back to `R1`. The model sees the
compact projection, but the trace still knows where the projection came from.

That gives us a clean contract for future context transforms:

- consume prompt components
- emit prompt components
- link outputs to source objects
- record a derivation with a named producer
- preserve the ability to inspect or replay the source context

This is why compaction belongs in prompt construction rather than as a
destructive timeline rewrite.

### Future Model-Controlled Context

The same object graph can support models manipulating their own context later.

Today, PromptBuilder decides which components go into the prompt. In the
future, a model could propose context operations as structured actions:

- keep this source component
- drop this component from future prompts
- replace these messages with a task-state object
- rehydrate this compacted source
- refresh this stale project context
- pin this decision or constraint for the rest of the run

Those operations become much safer if they produce trace objects rather than
mutating an opaque prompt string.

```text
model proposes context edit
        |
        v
 context transform / policy check
        |
        v
 new prompt components with links to old components
        |
        v
 next PromptBuilder run
```

This turns "the model manages its context" from an informal behavior into an
auditable protocol.

### Freshness And Substitution

Refs and derivations give us the pieces needed for freshness checks.

If a prompt component was derived from a mutable ref, the derivation can record
which object id that ref pointed to at build time. Later, if the ref points to
a different object, the derived object can be marked stale.

That matters for project context, skills, tool descriptors, or any future
summarized state. A task-state component is only useful if we know what it was
derived from and whether those sources have changed.

Once freshness is available, substitution becomes principled:

```text
fresh task_state exists      -> reuse it
stale task_state exists      -> regenerate it
missing task_state           -> build raw prompt or extract one
```

## What The Trace Enables

The prompt trace substrate gives us:

- exact model input inspection
- prompt replay from stored payloads
- alternate prompt rebuilds from the same session
- prompt diffs across transforms, models, or system prompts
- assistant output attribution to the prompt that produced it
- tool call and tool result attribution
- context compaction without losing provenance
- freshness checks for derived context
- safer future model-controlled context editing
- evaluation harnesses that compare prompt policies on the same sessions

The key property is that every important artifact is addressable. If a prompt
or answer is surprising, we can walk the graph backward and see what caused it.

## Current Boundaries

Prompt trace does not make model execution deterministic. It records the exact
payload and the observed output. Replaying the model can still differ if the
model, backend, sampling, or external state differs.

Prompt trace also does not mean the user sees the raw graph. The displayed chat
history is still useful as a UI projection, but for Zeta it is derived from
trace objects. The canonical continuity pointer is the run head, and the
canonical model input is the prompt object payload.

Finally, the system still needs better user-facing tools. The graph exists, but
the practical workflow should eventually include commands such as:

```text
sigil zeta trace show --prompt-id ...
sigil zeta trace replay --prompt-id ...
sigil zeta trace diff --prompt-id ... --against noop
sigil zeta trace closure --object-id ...
```

Those tools would turn the stored graph into a day-to-day debugging and
evaluation surface.
