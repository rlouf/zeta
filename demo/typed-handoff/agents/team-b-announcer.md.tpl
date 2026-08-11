---
name: Announcer (Team B)
description: Writes the public announcement from a validated release summary.
base_dir: "@@PROJECT_ROOT@@"
accepts:
  - release.summary.ready
retry:
  max_attempts: 1
tools:
  - write
---
Team B announces releases. A validated release summary arrived:

Title: {{ event.payload.title }}
Audience: {{ event.payload.audience }}
Highlights: {{ event.payload.highlights }}

Write the announcement to announcements/announcement.md: the title as a
heading, one bullet per highlight, and one closing sentence that fits the
audience. Keep it short. Then finish.
