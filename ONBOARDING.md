# Onboarding plan

Goal: make zeta's onboarding unique by letting the product onboard the user.
The organizing principle: **the only step that must happen without a running
agent is getting one verified model.** Everything after that boundary is
delegated to an onboarding agent running inside zeta itself.

Slogan for the direction: *"You don't run the tutorial. An agent runs it for
you."*

## Observations on the current state

- `zeta new` (`zeta/src/zeta/cli/commands/new.py`) scaffolds the
  inbox-summarizer via `scaffold_inbox_summarizer_project`
  (`zeta/src/zeta/authoring/starter.py`), then prints next steps. Codex auth
  is checked as an afterthought: a single `before you start, run: codex login`
  line when `~/.codex/auth.json` is missing.
- Model selection lives in `zeta/src/zeta/models/profiles.py`. Without a
  `~/.zeta/models.toml` default, zeta falls back to the built-in Codex profile
  (`gpt-5.6-sol` over `codex-responses`). Writing a profile today means
  hand-editing TOML from `docs/concepts.md`.
- `zeta/src/zeta/models/endpoint.py` already has `endpoint_reachable` /
  `model_endpoint_open` probing for local chat-completions endpoints; it is
  used before runs, not during setup.
- `zeta/src/zeta/models/codex_auth.py` reuses the Codex CLI credential store
  and raises a clear "run `codex login`" error, but only at request time —
  deep inside a run.
- README quickstart has two footguns: the file must be created *after*
  `zeta serve` starts (the filesystem connector ignores pre-existing files),
  and the happy path needs five commands across two terminals.
- The scaffolded project contains only the inbox-summarizer; nothing greets
  the user or acts before the user does.

## Phase 1 — model onboarding (the only pre-agent step)

A small wizard, run by `zeta new` (and re-run by `zeta serve` when no working
model is found).

1. **Detect before asking.**
   - Codex: `codex_auth_path().exists()` and token not obviously stale.
   - Local: probe the usual OpenAI-compatible endpoints with
     `endpoint_reachable` (llama.cpp on `127.0.0.1:8080`, ollama on `11434`).
   - If exactly one candidate is found, propose it as the default; only ask
     when there is a genuine choice (codex login / detected local model /
     paste a URL).
2. **Write the profile.** Persist the choice to `~/.zeta/models.toml` with
   `default = true` so the user never hand-edits TOML to get started.
3. **Verify with a real round-trip.** One tiny generation against the chosen
   profile before declaring success. Setup fails here, with the fix printed,
   or not at all — never five minutes later inside an opaque agent run.

Non-interactive contexts (`--yes`, CI) take the detected default or fail with
the same actionable message.

## Phase 2 — the handoff is an event

The verification generation does not print `OK`. Its success emits a
`zeta.onboarding.model-ready` event, which is exactly what wakes the
onboarding agent on the first `zeta serve`. The boundary between "setup" and
"product" is an event — zeta's own vocabulary. The user watches setup
dissolve into the runtime.

Implementation sketch: `zeta new` seeds the event (or the onboarding agent
declares a `catchup: latest` schedule that fires immediately on first serve);
the verification result rides in the payload so the agent's first words can
mention which model just came alive.

## Phase 3 — the agent runs the tutorial

`zeta new` scaffolds a second agent, `agents/onboarding.md`, alongside the
inbox-summarizer. Its responsibility: onboard the user, then get out of the
way.

- **First heartbeat.** On `zeta.onboarding.model-ready` it writes
  `welcome.md` into the project — what it is, what the inbox-summarizer is
  watching, what to try next — before the user has typed anything. Acting
  unprompted demonstrates "ambient" better than any README paragraph.
- **Tutorial chained through real work.** `zeta new` seeds `inbox/` with a
  first file whose summary — produced by the inbox-summarizer doing its
  actual job — ends with the next instruction ("drop your own file; then run
  `zeta traces log` to see how I did this one"). Each artifact teaches the
  next step; the tutorial is content flowing through the real event loop.
- **End on the receipt, not the result.** The final beat points at
  `zeta traces log --session agent/inbox-summarizer`: the exact events,
  prompts, and tool calls behind the file that just appeared. Every
  quickstart ends with output; ours ends with proof.
- **TUI first-run.** `zeta` with no subcommand on a fresh project opens
  attached to the onboarding agent's session, so the first minute is watching
  an agent think and act live.
- **Retirement.** The agent's last instruction tells the user how to delete
  `agents/onboarding.md` (or it offers to do it itself) once onboarding is
  done. It is a real agent, not a mode: if it feels flaky, the product is
  flaky — honest dogfooding.

Later symmetry: adding a *second* model never needs the wizard. The
onboarding agent can edit `models.toml` with its file tools and demo
`zeta traces replay --model` to compare profiles. Only the first model is
special, because it is the one that brings the agents to life.

## Friction fixes (independently worth doing)

- Seed `inbox/` at scaffold time and let the starter's first poll process
  pre-existing files, so `zeta serve` alone produces the first result — no
  two-terminal dance, no "create the file after serve starts" caveat.
- Check model auth at `zeta serve` startup too, printing the one-line fix at
  the moment it blocks instead of failing inside a run.
- Collapse the happy path to two commands:
  `uv tool install zeta-os && zeta new ~/zeta-demo`, then `zeta serve`.

## Open questions

- Does the filesystem connector's "ignore pre-existing files" rule become
  configurable per connector, or does the starter seed the first event
  directly instead?
- Is `zeta.onboarding.model-ready` a real durable event in the log (my
  preference: yes — the receipt should include setup) or a synthetic
  trigger?
- Wizard UX: plain `click` prompts in `zeta new`, or first-run inside the
  TUI? Plain prompts are simpler and work over SSH; TUI is flashier.
- Should the onboarding agent be able to run `codex login`-style external
  commands, or only ever touch files and events? (I lean: files and events
  only; keep external auth in the wizard.)

## Suggested implementation order

1. Friction fixes (seeding, serve-time auth check) — small, testable, no new
   surface.
2. Model wizard in `zeta new` + `models.toml` writer + verification
   round-trip.
3. `zeta.onboarding.model-ready` event + onboarding agent scaffold.
4. Tutorial content and TUI first-run attach.

Each step lands with tests first, per the usual workflow.
