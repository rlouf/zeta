# Demo: prompt diff and traced replay

Every model request Zeta makes is stored as a content-addressed object
graph: a `prompt` object links the exact components — system prompt,
objective, transcript messages, tool descriptors — that produced it, and
records the hash of the final payload. This demo walks the two commands
that cash that design in: `trace diff` and `trace replay`.

All output below is real, captured against a recorded session. To run it
yourself, point `SIGIL_SESSION_ID` at any session with agent turns
(`sigil session list`) and have your model endpoint up.

## 1. Find a prompt

`trace log` is the front door; `--kind prompt` narrows it to requests:

```text
$ sigil zeta trace log --kind prompt --limit 3
df3f195a  prompt              29 components · ~51880 tok
bcec7dc3  prompt              31 components · ~48320 tok
fd81e241  prompt              8 components · ~9743 tok
```

Every id below is one of these 8-char prefixes — no full hashes to copy.

## 2. What changed between two prompts?

Components are content-addressed, so identical ids are identical bytes —
the diff only has to look inside components that actually changed.
`--stat` gives the shape of the change, one line per component:

```text
$ sigil zeta trace diff fd81e241 bcec7dc3 --stat
prompts fd81e241 → bcec7dc3
~ transcript_message  062c859f → 62194743
~ transcript_message  4d546c00 → d96f59dc
~ transcript_message  db8bcdec → de5b5b75
~ transcript_message  556b9d30 → 02031c00
~ user_objective      9de7c00c → e228df6f
+ transcript_message  8c86697a  zeta.prompt_component.v1
+ transcript_message  bdeaf1d9  {"content":[{"text":"\"\"\"Skill discovery and prompt…
...
= 3 unchanged
```

Drop `--stat` and changed components render as unified text diffs of
their stored messages — the regression-hunting view when the same
objective produced different prompts under different context policies.

## 3. Replay the prompt through the model boundary

`trace replay` rebuilds the request from the linked components — not
from a transcript, from the graph — verifies it against the recorded
payload hash, and resends it:

```text
$ sigil zeta trace replay fd81e241
prompt   fd81e241  payload verified
model    default -> local-model @ http://127.0.0.1:8080/v1/chat/completions

original 254acd99
→ bash

replay   9113026c
→ bash
```

`payload verified` is the load-bearing line: the bytes sent to the model
just now are provably the bytes sent the first time. Here both the
original answer and the replay chose the same `bash` tool call. Use
`--model PROFILE` to replay against a different configured model, and
`--diff` to diff the two answers.

## 4. Replays are themselves traced

The replay is recorded with a `SigilModelReplay:v1` derivation, so the
forward walk from the prompt shows the original answer and every replay
side by side:

```text
$ sigil zeta trace tree fd81e241 --down --depth 1
fd81e241  prompt              8 components · ~9743 tok
├─ SigilModelResponse:v1
│  └─ 254acd99  assistant_message   → bash
├─ SigilRunEvent:v1
│  └─ f0069587  run_event           assistant_message
...
├─ SigilModelReplay:v1
│  └─ 9113026c  assistant_message   → bash
```

That closes the loop the design doc promises: "did the model fail
because the prompt was wrong, or despite a good prompt?" is now
answerable with three commands — diff the prompts, replay the suspect
one against another model, diff the answers — and every step of the
investigation is itself in the trace.
