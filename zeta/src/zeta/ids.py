"""Derived identity for durable runtime records.

Only an event receives a random id. Every id below an event is a pure function
of it:

```text
event_id       = evt_<uuid>
queue_item_id  = qi_<event_id>_<agent_id>
attempt_id     = att_<queue_item_id>_<attempt_number>
run_id         = run_<attempt_id>
```

This is what makes the chain idempotent. A router that sees the same event
twice computes the same queue item id, so the second routing attempt collides
with the first instead of creating parallel work.

Run ids are the one exception, and the exception is deliberate. A run id is
either **claimed in advance** or **derived** from the attempt:

- An RPC client must address a run before the run exists, so that it can cancel
  the run and correlate streamed events. No derived id is available that early,
  because the attempt appears only after the event is published, routed, and
  claimed. Such a caller claims an id with `claimed_run_id`.
- An authored agent needs no id in advance, so the harness derives one with
  `derived_run_id`.

`run_id_for_attempt` states the rule that joins them. The harness adopts a
claimed id when the triggering event carries one, and derives one otherwise.

This module holds string derivation only. It imports nothing from Zeta, so any
layer may use it.
"""

from __future__ import annotations

import uuid

QUEUE_ITEM_PREFIX = "qi_"
ATTEMPT_PREFIX = "att_"
RUN_PREFIX = "run_"


def safe_agent_id(agent_id: str) -> str:
    """Return an agent id that is safe inside a compound identifier."""
    return agent_id.replace(":", "_").replace(".", "_")


def queue_item_id(event_id: str, agent_id: str) -> str:
    """Return the queue item id that binds one event to one agent."""
    return f"{QUEUE_ITEM_PREFIX}{event_id}_{safe_agent_id(agent_id)}"


def pending_queue_item_id(event_id: str) -> str:
    """Return the unbound queue item id created for an ingress event."""
    return f"{QUEUE_ITEM_PREFIX}{event_id}"


def unhandled_queue_item_id(event_id: str) -> str:
    """Return the queue item id recorded when no agent accepts an event."""
    return f"{QUEUE_ITEM_PREFIX}{event_id}_unhandled"


def attempt_id(queue_item_id_value: str, attempt_number: int) -> str:
    """Return the id of one numbered execution try for a queue item."""
    return f"{ATTEMPT_PREFIX}{queue_item_id_value}_{attempt_number}"


def derived_run_id(attempt_id_value: str) -> str:
    """Return the run id derived from an attempt."""
    return f"{RUN_PREFIX}{attempt_id_value}"


def claimed_run_id() -> str:
    """Return a run id claimed before the run exists.

    A caller that must cancel or correlate a run needs its id in advance. No
    derived id exists that early, so the id is random.
    """
    return f"{RUN_PREFIX}{uuid.uuid4().hex}"


def run_id_for_attempt(claimed: str | None, attempt_id_value: str) -> str:
    """Adopt a run id claimed in advance, else derive one from the attempt."""
    return claimed or derived_run_id(attempt_id_value)


def agent_session_id(agent_id: str, event_id: str, *, session_scoped: bool) -> str:
    """Return the durable session id for one authored agent invocation.

    A session-scoped agent keeps one timeline across events. A one-shot agent
    gets a session per event, so unrelated events do not share timeline.
    """
    if session_scoped:
        return f"agent/{agent_id}"
    return f"agent/{agent_id}/{event_id}"


def queue_item_idempotency_key(
    event_id: str,
    target_agent: str,
    status: str,
    *,
    attempt_number: int | None = None,
) -> str:
    """Return the idempotency key for one queue item lifecycle fact."""
    key = f"queue_item:{event_id}:{target_agent}:{status}"
    if attempt_number is None:
        return key
    return f"{key}:{attempt_number}"


def unhandled_queue_item_idempotency_key(event_id: str) -> str:
    """Return the idempotency key recorded when no agent accepts an event."""
    return f"queue_item:{event_id}:unhandled"


def attempt_idempotency_key(
    queue_item_id_value: str,
    attempt_number: int,
    status: str,
) -> str:
    """Return the idempotency key for one attempt lifecycle fact."""
    return f"attempt:{queue_item_id_value}:{attempt_number}:{status}"
