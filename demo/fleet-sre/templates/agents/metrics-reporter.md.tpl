---
name: Metrics Reporter
description: Summarizes the rows of a metrics report in one sentence.
session: per-event
base_dir: @@WORK_DIR@@
accepts:
- ops.metrics.report
retry:
  max_attempts: 2
  backoff_seconds: 0
---
A metrics report arrived from {{ event.payload.source }}.

Latest row: {{ event.payload.rows[0] }}
All rows:
{% for row in event.payload.rows %}
- {{ row }}
{% endfor %}

Summarize the rows in one short sentence.
