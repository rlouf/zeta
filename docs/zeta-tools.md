# Zeta Tools

This document currently describes only `publish_event`.

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
