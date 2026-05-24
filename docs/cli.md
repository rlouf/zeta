# Sigil CLI contract

Sigil's human-readable output is allowed to evolve. Machine-readable output is
available through explicit `--json` flags and should remain stable across
compatible releases.

Status, progress, warnings, and errors are written to stderr. JSON output is
written to stdout.

## Top-level examples

```sh
sigil command --select "find large files"
sigil question --json "what changed in this repo?"
sigil session show --json
```

## `sigil command --json`

Generates fresh command candidates without opening the selector.

```json
{
  "prompt": "find large files",
  "commands": [
    {
      "command": "find . -type f -size +100M",
      "note": "Find files larger than 100 MB under the current directory."
    }
  ]
}
```

Stable fields:

- `prompt`: original user prompt.
- `commands`: ordered candidate list.
- `commands[].command`: runnable shell command proposal.
- `commands[].note`: short explanation.

## `sigil command --previous --json`

Reopens the current session's previous command candidates.

```json
{
  "prompt": "find large files",
  "commands": [
    {
      "command": "find . -type f -size +100M",
      "note": "Find files larger than 100 MB under the current directory."
    }
  ],
  "glyph": ",,",
  "inputs": ["event-id"],
  "integrity": "local_model",
  "capability": "propose",
  "taint": ["model"],
  "provisional": false
}
```

Stable fields are the same as `sigil command --json`, plus trust metadata:

- `glyph`
- `inputs`
- `integrity`
- `capability`
- `taint`
- `provisional`

## `sigil question --json`

Runs the Pi question pipeline and emits one JSON object instead of rendering
Markdown through `glow`.

```json
{
  "ok": true,
  "type": "answer",
  "question": "what changed in this repo?",
  "prompt": "what changed in this repo?",
  "follow_up": false,
  "answer": "The repository changed ...",
  "answer_event_id": "event-id",
  "tools": [
    {
      "type": "tool_start",
      "tool": "web_search",
      "detail": "query",
      "args": {"query": "query"},
      "glyph": "?",
      "inputs": ["question-event-id"],
      "integrity": "web",
      "capability": "read",
      "taint": ["web"],
      "provisional": true
    }
  ],
  "malformed_events": 0,
  "security": {
    "glyph": "?",
    "inputs": ["question-event-id"],
    "integrity": "web",
    "capability": "read",
    "taint": ["web"],
    "provisional": true
  }
}
```

Stable fields:

- `ok`: `true` when the stream renderer completed successfully.
- `type`: currently always `"answer"`.
- `question`: user-visible question text.
- `prompt`: expanded prompt sent to Pi. For follow-ups, this includes context.
- `follow_up`: whether this was invoked through `sigil question --follow-up --json`.
- `answer`: concatenated assistant text.
- `answer_event_id`: event id for the stored answer, or `null` if no answer text
  was emitted.
- `tools`: ordered Pi tool trace events.
- `malformed_events`: count of malformed Pi JSON event lines ignored.
- `security`: trust metadata applied to the answer and tool trace.

`sigil question --follow-up --json` uses the same shape with `follow_up: true`.

## `sigil session --json`

`session` has four JSON forms:

```sh
sigil session show --json
sigil session path --json
sigil session list --json
sigil session clear --json
```

`show` returns the current session id, path, and parsed continuity files.
`path` returns the global state path, current session path, session id, and
global event log path. `list` returns known session directories and the files
present in each. `clear` returns the session files removed.

## Hidden plumbing commands

These commands are intentionally not shown in top-level help and are not stable
user-facing API:

- `sigil render-pi-stream`
- `sigil record-failure`

They exist so shell bindings can keep a small, explicit boundary with the Python
runtime.
