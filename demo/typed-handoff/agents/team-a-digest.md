---
name: Release Digest (Team A)
description: Turns raw change entries into a typed release summary for other teams.
accepts:
  - release.notes.requested
publishes:
  - release.summary.ready
tools: []
---
Team A is preparing release notes for version {{ event.payload.version }}.
The raw change entries are:

{{ event.payload.changes }}

Rewrite each change as one short plain-English highlight sentence. Pick the
audience: "customers" if the changes are user-visible, "internal" otherwise.
Give the release a short title that includes the version number.

Hand the summary to Team B by calling the publish_event tool with
event_type "release.summary.ready" and a payload of the form
{"title": ..., "highlights": [...], "audience": ...}.

This run is a demonstration of the runtime's schema gate: first call
publish_event once with "audience": "the-world" (an invalid value) and report
the exact error you get back. Then publish the correct payload. Finish once
the correct publish is accepted.
