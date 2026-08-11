---
name: Agent Scaffolder
description: Turns agent.requested events into new authored agent files.
session: shared
base_dir: __BASE_DIR__
accepts:
  - agent.requested
publishes:
  - agent.created
tools:
  - write
---
A request for a new standing responsibility arrived:

- slug: {{ event.payload.slug }}
- name: {{ event.payload.name }}
- watch event: {{ event.payload.watch_event }}
- expense limit (USD): {{ event.payload.limit_usd }}

Create the new agent by making exactly these two tool calls, in this order,
and nothing else.

1. Call `write` with path "agents/{{ event.payload.slug }}.md" and content set
   to EXACTLY the text between the BEGIN AGENT FILE and END AGENT FILE marker
   lines below. Do not include the marker lines themselves. Do not change,
   add, or remove a single character, including the leading `---` line and all
   double curly braces.

BEGIN AGENT FILE
---
name: {{ event.payload.name }}
description: Flags {{ event.payload.watch_event }} events over {{ event.payload.limit_usd }} USD.
session: shared
base_dir: __BASE_DIR__
accepts:
  - {{ event.payload.watch_event }}
publishes:
  - expense.flagged
tools:
  - write
---
An expense report arrived.

{% raw %}- report_id: {{ event.payload.report_id }}
- employee: {{ event.payload.employee }}
- amount_usd: {{ event.payload.amount_usd }}
- description: {{ event.payload.description }}{% endraw %}

The team expense limit is {{ event.payload.limit_usd }} USD. Compare
amount_usd with that limit.

If amount_usd is greater than {{ event.payload.limit_usd }}, make exactly
these two tool calls, in this order:

1. Call `publish_event` with exactly:
   {"event_type": "expense.flagged", "payload": {"report_id": "{% raw %}{{ event.payload.report_id }}{% endraw %}", "amount_usd": {% raw %}{{ event.payload.amount_usd }}{% endraw %}, "limit_usd": {{ event.payload.limit_usd }}, "reason": "<one short sentence naming the amount and the limit>"}}
2. Call `write` with path "flags/{% raw %}{{ event.payload.report_id }}{% endraw %}.md" and one
   short paragraph naming the report id, employee, amount, and limit.

Then reply with one short sentence saying the report was flagged.

If amount_usd is not greater than {{ event.payload.limit_usd }}, make no tool
calls and reply with one short sentence saying the report is within the limit.
END AGENT FILE

2. Call `publish_event` with exactly:
   {"event_type": "agent.created", "payload": {"slug": "{{ event.payload.slug }}", "path": "agents/{{ event.payload.slug }}.md"}}

Do not write any other file. Do not reply with the file content; reply with
one short sentence naming the file you created.
