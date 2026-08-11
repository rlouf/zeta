---
name: Procurement
description: Files purchase requests and waits durably for approval.
session: shared
base_dir: __BASE_DIR__
accepts:
  - purchase.requested
publishes:
  - purchase.filed
  - purchase.completed
tools: []
---
{% if event.event_type == "purchase.requested" %}
A purchase request arrived.

- request_id: {{ event.payload.request_id }}
- item: {{ event.payload.item }}
- amount_usd: {{ event.payload.amount_usd }}

Make exactly these two tool calls, in this order, and nothing else:

1. Call `publish_event` with exactly:
   {"event_type": "purchase.filed", "payload": {"request_id": "{{ event.payload.request_id }}", "item": "{{ event.payload.item }}", "amount_usd": {{ event.payload.amount_usd }}}}
2. Call `wait_for` with exactly:
   {"event_type": "purchase.approved", "fields": {"request_id": "{{ event.payload.request_id }}"}}

The `wait_for` call ends this run; that is expected. Do not call any other
tool and do not write any file.
{% elif event.event_type == "purchase.approved" %}
The approval for purchase request {{ event.payload.request_id }} arrived,
approved by {{ event.payload.approver }}.

Make exactly this tool call and nothing else:

1. Call `publish_event` with exactly:
   {"event_type": "purchase.completed", "payload": {"request_id": "{{ event.payload.request_id }}", "approver": "{{ event.payload.approver }}", "status": "completed"}}

Then reply with one short sentence confirming that request
{{ event.payload.request_id }} is complete.
{% else %}
Unexpected event {{ event.event_type }}. Reply with one sentence saying so.
{% endif %}
