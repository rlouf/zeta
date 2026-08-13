# Zeta Tools

This document describes the Zeta-specific tools `publish_event`, `wait_for`,
`cancel`, `query_log`, `query_context_budget`, `query_content`,
`transform_content`, and `finish`.

## `publish_event`

Use `publish_event` to request an event. Zeta processes the request only after
the agent run completes successfully.

### Availability

Zeta provides `publish_event` when the agent declares at least one event in
`publishes`:

```yaml
---
name: Release Summarizer
description: Summarizes a release.
accepts:
  - release.completed
publishes:
  - release.summary.ready
---
```

Do not add `publish_event` to the agent `tools` list. The name is reserved.
Zeta adds the tool to the model request.

### Examples

#### Start The Next Step

An invoice agent can publish `invoice.extracted` after it extracts the invoice
data. Another agent can accept this event and check the data. The first agent
does not need to know which agent performs the check.

```json
{
  "event_type": "invoice.extracted",
  "payload": {
    "invoice_id": "INV-1042",
    "total": 1250
  }
}
```

#### Deliver A Result

A support agent can publish `slack.message.post` after it writes a reply. The
Slack connector can deliver the event to the selected channel.

```json
{
  "event_type": "slack.message.post",
  "payload": {
    "channel_id": "C123",
    "text": "The deployment completed successfully."
  }
}
```

#### Schedule A Follow-Up

An incident agent can request a later status check. The agent does not need to
remain active while it waits.

```json
{
  "event_type": "incident.status_check.requested",
  "payload": {
    "incident_id": "INC-42"
  },
  "at": "2030-01-02T10:00:00Z"
}
```

### Input

The tool accepts this object:

| Field | Required | Rule |
| --- | --- | --- |
| `event_type` | yes | The type must be in the agent `publishes` list. |
| `payload` | yes | The value must be an object that matches the event schema. |
| `at` | no | The value must be an ISO 8601 date-time with a UTC offset. |

The object cannot contain other fields.

This call requests an event for immediate publication:

```json
{
  "event_type": "release.summary.ready",
  "payload": {
    "summary": "The release is ready."
  }
}
```

This call requests an event for a future time:

```json
{
  "event_type": "release.summary.ready",
  "payload": {
    "summary": "The release is ready."
  },
  "at": "2030-01-02T09:00:00+01:00"
}
```

### Result

A valid request returns a stable publication handle:

```json
{
  "ok": true,
  "handle": "pub_0123456789abcdef01234567"
}
```

An invalid request returns an error:

```json
{
  "ok": false,
  "error": {
    "code": "undeclared-event-type",
    "message": "the agent does not list event 'release.unknown' in publishes"
  }
}
```

The tool can return these error codes:

| Code | Meaning |
| --- | --- |
| `undeclared-event-type` | The agent does not declare the event type. |
| `invalid-event-payload` | The payload does not match the event schema. |
| `invalid-publish-time` | The `at` value is invalid or has no UTC offset. |

### Publication

The model can call `publish_event` more than once during one run. Zeta publishes
the requested events in call order.

The model must call `publish_event` for each event. Zeta does not infer an event
from the model response.

Zeta publishes the requested events only after the run completes successfully.
A failed or cancelled run publishes none of its requested events.

If `at` is absent or is at or before the completion time, Zeta publishes the
event after the run completes. If `at` is later, Zeta publishes the event at
that time.

## `wait_for`

Use `wait_for` when an agent must pause until an event arrives. The current run
ends. Zeta starts a new run in the same agent session when the event arrives.

You can also set a deadline. If no matching event arrives by that time, Zeta
starts a new run with a timeout event.

Zeta adds `wait_for` to authored agents automatically. Do not add it to the
agent's `tools` list. The name is reserved.

### Input

The tool accepts this object:

| Field | Required | Rule |
| --- | --- | --- |
| `event_type` | yes | A non-empty event type. |
| `fields` | no | Top-level event payload fields that must have the same JSON values. |
| `deadline` | no | An ISO 8601 date-time with a UTC offset. |

The object cannot contain other fields.

`fields` is optional. If it is absent or empty, any event with the requested
type matches.

If `fields` contains a key, the event payload must contain that key with the
same value. Other top-level fields in the event payload are allowed. Zeta does
not match part of a nested object. The complete nested value must be equal.

### Example

This call waits for an update to issue 7:

```json
{
  "event_type": "github.issue.updated",
  "fields": {
    "number": 7
  },
  "deadline": "2030-01-02T10:00:00Z"
}
```

This event matches because `number` is `7`. The extra `state` field is
allowed:

```json
{
  "number": 7,
  "state": "closed"
}
```

### Result

A valid request returns a stable wait handle and stops the current run:

```json
{
  "ok": true,
  "handle": "wait_0123456789abcdef01234567",
  "stop": true
}
```

An invalid deadline returns this error and does not stop the run:

```json
{
  "ok": false,
  "error": {
    "code": "invalid-wait-deadline",
    "message": "deadline must include a UTC offset"
  }
}
```

### What Zeta Does

Zeta saves the wait only after the current attempt completes successfully. A
failed, cancelled, or stale attempt does not create a wait.

One agent session can have one active wait. A second wait for the same session
fails the attempt and does not replace the first wait.

The first matching event consumes the wait once. A duplicate delivery does not
start a second continuation. If matching and timeout happen at the same time,
only one of them wins.

The continuation uses the same agent, session, and project version as the
original run. For a match, the prompt receives the event type and payload that
matched. The original event still follows normal event routing.

Use `zeta waits list` to inspect active and completed waits. Add `--json` for
JSON output.

## `cancel`

Use `cancel` to stop future work created by the same agent session. The tool
accepts an active `wait_...` handle or a pending `pub_...` handle.

Zeta adds `cancel` to authored agents automatically. Do not add it to the
agent's `tools` list. The name is reserved. Session runs do not receive this
tool.

### Input

| Field | Required | Rule |
| --- | --- | --- |
| `handle` | yes | An active `wait_...` handle or pending `pub_...` handle. |
| `reason` | no | A non-empty reason for the cancellation. |

The object cannot contain other fields.

### Result

A valid tool call records a cancellation request. The agent run continues:

```json
{
  "ok": true,
  "handle": "wait_0123456789abcdef01234567",
  "status": "requested"
}
```

Zeta applies the request only after the attempt completes successfully. A
failed, cancelled, or stale attempt does not cancel any work.

The handle must belong to the same agent session. A request for another
session's handle fails the attempt.

Cancelling an active wait records `runtime.wait.cancelled`. Zeta does not
start a continuation for that wait. Cancelling a pending deferred publication
records `runtime.deferred_publication.cancelled`. Zeta does not publish that
event.

Matching, timeout, publication, and cancellation are atomic. Only one terminal
state can win. Repeating a cancellation is safe. Zeta returns the resource's
current terminal state and does not add another cancellation event.

### Command Line

Runtime operators can cancel any supported handle:

```sh
zeta cancel run_0123456789abcdef01234567
zeta cancel wait_0123456789abcdef01234567
zeta cancel pub_0123456789abcdef01234567 --reason "No longer needed"
zeta cancel wait_0123456789abcdef01234567 --json
```

For a run ID, the command cancels the related queue item. A queued turn becomes
terminal immediately. A running turn reports `cancelling` until its worker
stops. The request survives client and worker restarts.

The command reports the current state. A repeated command succeeds and
reports `changed: false` in JSON output.

## `query_log`

Use `query_log` to read summaries of earlier model runs in the current
session. The summaries can help an agent recover earlier decisions, results,
and failed actions.

### Availability

Add `query_log` to the agent `tools` list:

```yaml
---
name: Release Investigator
description: Investigates release failures.
accepts:
  - release.investigation.requested
tools:
  - query_log
---
```

`query_log` is available only in a durable runtime session. Zeta limits every
query to the current session and excludes the active run. The tool does not
accept a session id.

### Examples

#### Review Recent Work

Use an empty object to list the most recent prior runs:

```json
{}
```

The list can help an agent find the run that made an earlier decision. The
agent can cite the returned run id when it uses that decision.

#### Find Recent Failures

Use `failed` and `since` to find failed or aborted runs from a recent period:

```json
{
  "failed": true,
  "since": "6h",
  "limit": 10
}
```

This query can help an agent find a failed deployment, test run, or connector
delivery without reading every prior run.

#### Inspect One Run

Use a full run id or a unique prefix to expand one run:

```json
{
  "run_id": "run_01JXYZ"
}
```

The expanded result can help an agent recover the objective, final answer,
tool failures, and prompt trace ids from that run.

### Input

The tool accepts this object:

| Field | Required | Rule |
| --- | --- | --- |
| `since` | no | Use `YYYY-MM-DD` or an age such as `2d`, `6h`, or `30m`. |
| `failed` | no | Use `true` to list only failed or aborted runs. |
| `run_id` | no | Use a full run id or a unique run id prefix. |
| `limit` | no | Use an integer from 1 through 50. The default is 20. |

The object cannot contain other fields. If `run_id` is present, Zeta expands
that run and does not apply the listing filters.

### Result

A list result contains one text summary and metadata:

```json
{
  "ok": true,
  "content": [
    {
      "type": "text",
      "text": "run_01JXYZ  failed  2030-01-02T09:00:00+00:00  deploy release"
    }
  ],
  "metadata": {
    "runs": 1,
    "run_ids": ["run_01JXYZ"],
    "session_id": "release-session",
    "limit": 20
  }
}
```

An expanded result can include these fields in its text summary:

- Run id and start time.
- Outcome and objective.
- Final answer.
- Compact tool activity and failure details.
- Prompt trace ids.

The expanded result does not include provider usage data, raw tool arguments,
or raw tool results.

The tool can return these error codes:

| Code | Meaning |
| --- | --- |
| `query-log-unavailable` | The run is not in a durable runtime session. |
| `invalid-since` | The `since` value has an invalid format. |
| `unknown-run-id` | No run in the current session matches the id. |
| `ambiguous-run-id` | More than one run matches the id prefix. |

### Limits

Zeta reads at most the 5,000 newest events in the current session. A result
contains at most 50 runs and 12,000 text characters. These limits keep the
model context bounded.

## `query_context_budget`

Use `query_context_budget` to inspect the context budget for the active model
turn. The tool does not change the prompt. It does not start compaction.

### Availability

Add `query_context_budget` to the agent `tools` list:

```yaml
---
name: Context-Aware Investigator
description: Investigates large sets of evidence.
accepts:
  - investigation.requested
tools:
  - query_context_budget
---
```

The tool accepts an empty object. The model cannot select another model,
prompt, endpoint, or budget.

### Result

The result describes the latest prompt and its active model:

```json
{
  "ok": true,
  "context_window_tokens": 32768,
  "prompt_tokens": 12288,
  "prompt_tokens_source": "provider",
  "reserved_output_tokens": 8192,
  "remaining_tokens": 12288,
  "usage_ratio": 0.5,
  "compaction_strategy": "structural_trim",
  "compaction_threshold_tokens": 15000
}
```

The fields have these meanings:

| Field | Meaning |
| --- | --- |
| `context_window_tokens` | The context window that Zeta found for the active model. |
| `prompt_tokens` | The token use of the latest prompt. |
| `prompt_tokens_source` | `provider`, `estimate`, or `unavailable`. |
| `reserved_output_tokens` | The token space reserved for the model output. |
| `remaining_tokens` | The context window minus the prompt and output reservation. |
| `usage_ratio` | The prompt use divided by the usable prompt budget. |
| `compaction_strategy` | The active prompt compaction strategy, or `off`. |
| `compaction_threshold_tokens` | The active compaction threshold, or `null`. |

Zeta uses provider usage when the provider reports it. Otherwise, Zeta can
estimate the prompt use from the stored prompt trace. The estimate is stable,
but it can differ from the provider tokenizer.

Some model endpoints do not report a context window. In that case,
`context_window_tokens`, `remaining_tokens`, and `usage_ratio` are `null`.
The query still succeeds. `prompt_tokens` is also `null` when Zeta has no
provider usage and no stored prompt trace.

`remaining_tokens` can be negative. `usage_ratio` can be greater than `1.0`.
These values show that the prompt and output reservation exceed the model
budget.

### Examples

#### Compact Evidence Before More Reads

An investigation agent can read many large files. It can call
`query_context_budget` before it reads the next group. If little space remains,
the agent can use `transform_content` to reduce the current evidence into one
short session memory. It can continue from that memory in the next turn.

#### Reduce Child Model Results

A root model can ask child models to inspect many documents. It can call
`query_context_budget` after each batch. If the usage ratio is high, it can use
`transform_content` to reduce the child results before it starts the next
batch. This keeps the root context focused on conclusions and open questions.

## Content Tools

The content tools let an agent use values that do not fit in the root model
context. The agent can inspect a bounded list, transform selected values, and
return a stored value.

Add the required tools to the agent definition:

```yaml
tools:
  - query_content
  - transform_content
  - finish
```

Content has one of these scopes:

| Scope | Lifetime |
| --- | --- |
| `run` | The content is available in later turns of the current run. |
| `session` | The content is available in later runs of the current session. |
| `agent` | The content is available in later runs of the same agent. |

Zeta changes the run scope immediately. Zeta changes the session or agent
scope only after the run succeeds. A failed or cancelled run does not promote
content.

### Why Content Tools Are Useful

- An agent can keep a complete tool result outside the root context. It can
  make a short summary for the next turn.
- An agent can map one model instruction over many documents. It can reduce
  the results into one report.
- An agent can keep a decision in the current session. A later run can use the
  decision without reading the complete earlier conversation.
- An agent can improve a procedure and keep it for later runs.
- An agent can build a large answer in content objects. It can return the
  final object with `finish`.

## `query_content`

Use `query_content` to inspect the current content workspace. The result has
stable keys, object ids, scopes, and bounded previews. It also has the current
head id. Use this head id in the next `transform_content` call.

This example lists procedure content whose key starts with `release/`:

```json
{
  "key_prefix": "release/",
  "kind": "procedure",
  "limit": 20
}
```

The tool accepts these fields:

| Field | Required | Rule |
| --- | --- | --- |
| `key_prefix` | no | Return keys that start with this value. |
| `kind` | no | Return content with this kind. |
| `source_scope` | no | Use `run`, `session`, or `agent`. |
| `limit` | no | Use an integer from 1 through 50. The default is 20. |
| `cursor` | no | Use the cursor from an earlier result. |

A result has this form:

```json
{
  "ok": true,
  "head": "b3:2d063d1b1cbb14278839d182222ff0928212eb462a2d256f16f67a508b83ef29",
  "items": [
    {
      "key": "release/check-manifest",
      "kind": "procedure",
      "object_id": "b3:3ec88844d91cc5ff131c16e533b6702a7c3858b99815422f21bd93ddbff0e417",
      "source_scope": "agent",
      "chars": 48,
      "preview": "Check the release manifest before publication."
    }
  ],
  "next_cursor": null
}
```

The result does not include complete large values. Use a transformation to
process these values outside the root context.

## `transform_content`

Use `transform_content` to create a new content revision. Each call selects
inputs, applies one transformation, and writes one destination.

Each call is one logical commit:

```text
read an exact revision -> compute -> store the result and its derivation -> try to advance the head
```

Python or model computation can take time. Another transformation can advance
the head before that computation finishes. `expected_head` makes Zeta activate
the result only when the selected revision is still current. If the head moved,
Zeta does not make the candidate revision active. This prevents an old result
from replacing newer content.

The call must include `expected_head`. Zeta rejects the call if another change
moved the current head. A replacement must also include the current object id
in `destination.expected_object_id`.

### Transformation Types

| Type | Use |
| --- | --- |
| `literal` | Store a supplied text or JSON value. |
| `patch` | Replace selected fields of one content node. |
| `drop` | Remove selected keys from the new revision. |
| `identity` | Copy one selected value to another key or scope. |
| `model` | Run a model over selected values. |
| `python` | Run Python over selected values and compose model transforms. |

Model transformations support these modes:

| Mode | Use |
| --- | --- |
| `one` | Transform the selected values into one result. |
| `map` | Transform each selected value and keep the input order. |
| `reduce` | Combine the selected values into one result. |

### Store A Procedure

This call stores a procedure for later runs of the same agent:

```json
{
  "expected_head": "b3:2d063d1b1cbb14278839d182222ff0928212eb462a2d256f16f67a508b83ef29",
  "reason": "The release failed because this check was absent.",
  "inputs": {},
  "transformation": {
    "type": "literal",
    "value": "Check the manifest before publication.",
    "title": "Check the release manifest"
  },
  "destination": {
    "key": "release/check-manifest",
    "kind": "procedure",
    "scope": "agent",
    "expected_object_id": null
  }
}
```

The new value is available in the next turn of this run. Zeta requests agent
promotion. The value becomes durable only if the run succeeds.

### Map Over Documents

This call extracts one finding from each selected document:

```json
{
  "expected_head": "b3:2d063d1b1cbb14278839d182222ff0928212eb462a2d256f16f67a508b83ef29",
  "reason": "Extract evidence from each document.",
  "inputs": {
    "kind": "document"
  },
  "transformation": {
    "type": "model",
    "mode": "map",
    "instruction": "Extract evidence that is relevant to the objective.",
    "max_concurrency": 4
  },
  "destination": {
    "key": "findings",
    "kind": "collection",
    "scope": "run",
    "expected_object_id": null
  }
}
```

Zeta stores each child prompt and answer. The collection links to the answers
in input order. All child calls use one shared run budget.

### Compose Transforms With Python

A Python transformation must define `main(ctx, transform)`. `ctx.select`
returns the selected graph values. `transform` requests a nested model
transformation.

```python
async def main(ctx, transform):
    documents = ctx.select(kind="document")
    findings = await transform(
        inputs=documents,
        transformation={
            "type": "model",
            "mode": "map",
            "instruction": "Extract one relevant fact.",
            "max_concurrency": 4,
        },
        destination={
            "key": "findings",
            "kind": "collection",
            "scope": "run",
        },
    )
    return await transform(
        inputs=findings,
        transformation={
            "type": "model",
            "mode": "reduce",
            "instruction": "Write one report from the findings.",
        },
        destination={"key": "report", "kind": "text", "scope": "run"},
    )
```

Python can use arbitrary imports, files, network calls, and subprocesses. Zeta
does not use a security sandbox. Zeta applies a timeout and the shared run
cancellation signal. Nested model calls use the normal model path and trace.

### Result

A successful call returns object references and the new run head:

```json
{
  "ok": true,
  "status": "applied",
  "active_scope": "run",
  "head": "b3:ee4deb261bd45e0c54a65f1490aa94edd20230f3ef0b43cf96747f6fc40757a4",
  "object_ids": ["b3:3ec88844d91cc5ff131c16e533b6702a7c3858b99815422f21bd93ddbff0e417"],
  "promotions": [
    {
      "scope": "agent",
      "key": "release/check-manifest",
      "status": "requested"
    }
  ]
}
```

## `finish`

Use `finish` to select a reachable content object as the final answer. The
root model does not have to copy a large value into another message.

```json
{
  "object_id": "b3:3ec88844d91cc5ff131c16e533b6702a7c3858b99815422f21bd93ddbff0e417"
}
```

A valid call returns this result and stops the run:

```json
{
  "ok": true,
  "stop": true,
  "object_id": "b3:3ec88844d91cc5ff131c16e533b6702a7c3858b99815422f21bd93ddbff0e417"
}
```

The object must be reachable from the current content head. Zeta reads the
complete value only at the external result boundary.

## Agent-Authored Tools

An agent can store Python source as content with the kind `tool_definition`.
The content must contain these fields:

```json
{
  "name": "check_release",
  "capability_id": "agent.release-agent.check_release",
  "source": "from zeta.capabilities.registry import RegisteredCapability\n..."
}
```

The source must expose one `RegisteredCapability` as `tool`. It can also
expose a zero-argument `tool()` factory. The capability id must have this
form:

```text
agent.<agent-slug>.<tool-name>
```

Use `transform_content` with an agent destination:

```json
{
  "expected_head": "b3:2d063d1b1cbb14278839d182222ff0928212eb462a2d256f16f67a508b83ef29",
  "reason": "Use the release check in later runs.",
  "inputs": {},
  "transformation": {
    "type": "literal",
    "value": {
      "name": "check_release",
      "capability_id": "agent.release-agent.check_release",
      "source": "...Python source..."
    }
  },
  "destination": {
    "key": "tools/check_release",
    "kind": "tool_definition",
    "scope": "agent",
    "expected_object_id": null
  }
}
```

Zeta compiles and imports the source in a subprocess before it stores the
revision. Zeta checks the namespace, name, input schema, delivery mode, source
size, and collisions. This check tests correctness. It is not a security
sandbox.

The tool cannot change the current run's tool schema. Zeta activates the tool
in the next project generation after the run succeeds. The owner agent gets
the tool automatically. The generation records the exact source, schema, and
content object id.

Human-authored tools use the same validation and execution path. Put one
Python module in this directory:

```text
agents/tools/<agent-slug>/<tool-name>.py
```

### Inspect And Restore Content

Use these commands to inspect and restore durable agent content:

```sh
zeta agents content show release-agent
zeta agents content log release-agent
zeta agents content diff release-agent OLD_HEAD NEW_HEAD
zeta agents content restore release-agent HEAD --reason "Restore a working revision."
```

Use these commands for graph-authored tools:

```sh
zeta agents tools list release-agent
zeta agents tools show release-agent check_release
zeta agents tools disable release-agent check_release --reason "This version is broken."
zeta agents tools restore release-agent check_release OBJECT_ID --reason "Use the working version."
```

Restore and disable create new ref states. They do not delete content objects
or earlier revisions. File-authored modules remain project files and are not
changed by these commands.
