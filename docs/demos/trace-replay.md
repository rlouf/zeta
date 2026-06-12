# Demo: prompt diff and traced replay

Every model request Zeta makes is stored as a content-addressed object
graph: a `prompt` object links the exact components — system prompt,
objective, timeline messages, tool descriptors — that produced it, and
records the hash of the final payload. This demo walks the two commands
that cash that design in: `trace diff` and `trace replay`.

All output below is real, captured against a recorded session. To run it
yourself, point `SIGIL_SESSION_ID` at any session with agent turns
(`sigil session list`) and have your model endpoint up.

## 1. Find a prompt

`trace log` is the front door; `--kind prompt` narrows it to requests:

```text
$ sigil trace log --kind prompt --limit 3
e4976fc1  prompt              10 components · ~8772 tok
2fc6c722  prompt              8 components · ~8331 tok
0c56ad85  prompt              6 components · ~8204 tok
```

Every id below is one of these 8-char prefixes — no full hashes to copy.

## 2. What changed between two prompts?

Components are content-addressed, so identical ids are identical bytes —
the diff only has to look inside components that actually changed.
`--stat` gives the shape of the change, one line per component:

```text
$ sigil trace diff 0c56ad85 2fc6c722 --stat
prompts 0c56ad85 → 2fc6c722
~ user_message        e77d66bd → 40d33f4e
~ assistant_message   b8e10d8a → 041d8af4
~ tool_result         f925578e → 903d7161
+ assistant_message   c6026a19  README.md has 553 lines.
+ user_message        3868b48f  Run the automatic tool loop until no more tool calls are needed.
= 3 unchanged
```

These two prompts straddle a step boundary: the objective changed
(`~ user_message`), and the previous step's exchange moved from
current-turn components into the historical timeline tail — same
messages, different component data — which is why the assistant message
and tool result changed ids rather than disappearing.

Drop `--stat` and changed components render as unified text diffs of
their stored messages — the regression-hunting view when the same
objective produced different prompts under different context policies.

## 3. Replay the prompt through the model boundary

`trace replay` rebuilds the request from the linked components — not
from a transcript, from the graph — verifies it against the recorded
payload hash, and resends it:

```text
$ sigil trace replay 13b60e1a
prompt   13b60e1a  payload verified
model    default -> local-model @ http://127.0.0.1:8080/v1/chat/completions

original db5d29fd
→ bash

replay   02e5a395
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
$ sigil trace tree 13b60e1a --down --depth 1
13b60e1a  prompt              4 components · ~7761 tok
├─ SigilModelResponse:v1
│  └─ db5d29fd  assistant_message   → bash
├─ SigilRunEvent:v1
│  └─ 07c71b83  run_event           assistant_message
├─ SigilRunEvent:v1
│  └─ 5526e09a  run_event           tool_result
├─ SigilTurnRecord:v1
│  └─ 1dfb27c6  turn                sigil.turn.v1
└─ SigilModelReplay:v1
   └─ 02e5a395  assistant_message   → bash
```

That closes the loop the design doc promises: "did the model fail
because the prompt was wrong, or despite a good prompt?" is now
answerable with three commands — diff the prompts, replay the suspect
one against another model, diff the answers — and every step of the
investigation is itself in the trace.
