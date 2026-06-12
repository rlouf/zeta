# Sigil notes

The core is hardened and the delegation ledger is queryable end to end:
ledger Stages 1–3 are landed, the trace explorer has its plumbing,
porcelain, and diff/replay (Stages A–C, graduated live), ask is folded
into step, the zsh binding is owned end to end (pty harness, zero-fork
spool, session-per-pty, raw glyph dispatch, `+` completion), and the
public CLI exit codes are named constants. What remains: web tools
(proposal below, open questions pending), the tool-contract CLI surface
(proposal below), ledger Stage 4 (durable/global/portable), and explorer
Stage D (cross-session scope, search).

## Decisions in force

- **Trust model: local user, local trust.** `,,,` is YOLO mode — nothing
  staged, no filesystem boundary — documented in the README with OS
  sandbox pointers (bubblewrap, sandbox-exec) for anyone who wants an
  enforced boundary. A cwd workspace boundary for write/edit (direct
  inside, staged handoff outside) stays available as a post-alpha option:
  both execution paths and the per-call dispatch point exist, but bash
  cannot honestly participate (its touched paths are statically
  undecidable) and the boundary forces a `,,`/`,,,` semantics decision.
- **Staging is a property of the tool contract.** `ToolSpec.effects`
  declares what each tool does, plugins included; undeclared effects
  count as mutating, and a mutating tool without a staging implementation
  is refused in propose mode. The ledger's effect records map `kind`
  straight from this vocabulary.
- **Recording: commands and exit codes, always.** Always-on shell
  recording is in place; the capture window is gone.
- **`session clear`: continuity dies, ledger survives.** Clear removes
  the session dir (trace store, bridged turn objects, `turn/` refs);
  `ledger.sqlite3` and `events.jsonl` are global and untouched.
- **Prompts carry the date, never the time.** `Today is YYYY-MM-DD
  (Weekday).` in every workflow's system prompt; a finer stamp would
  defeat the content-addressed component dedup.
- **Prompt content lives in the workflow layer; the runtime assembles.**
  `STEP_SYSTEM_PROMPT` (`workflows/step.py`, shared by do/propose) and
  `ASK_SYSTEM_PROMPT` (`workflows/ask.py`) own the personas;
  `zeta/prompt/system.py` renders scaffolding only — date line, tool
  protocol, skills, descriptors — and invents no content
  (`system_prompt()` with no base is assembly-only). Mirrors the
  roadmap's definition-as-artifact boundary: content changes with the
  product, assembly with the runtime. Step-path prompt bytes are
  unchanged — the same text is now passed explicitly, so trace
  components keep deduplicating.
- **Test infra:** coverage is measured in CI report-only (86%); a
  fail-under gate waits until the number stabilizes. The two patching
  idioms (`_patch.py` vs raw `monkeypatch`) coexist deliberately —
  converging ~145 call sites is churn without payoff.

## Deliberate non-fixes

- `summarize.count_lines` duplicates `structural_trim.line_count`;
  neither module is a natural home for both, so the 3-line function
  stays twice rather than coupling display to compaction.

## Shell binding findings (2026-06-12)

Durable facts from the own-zsh work, kept because they explain current
behavior:

- The `handoff shell-turn` CLI command and `append_shell_turn` are gone;
  the spool (`shell-turns.spool`, `\x1f`/`\x1e`-delimited) is the only
  binding→CLI recording path, claimed by rename, orphans recovered
  after 60s. Measured recording cost 0.05ms/command (was 35–45ms warm).
- zshaddhistory return-1 lines *linger* in internal history until the
  next command executes; the `+` widget therefore does not print -s —
  the line-init hook inserts the original line at the next prompt in
  the parent shell, which replaces the linger. First up-arrow recalls
  what was typed.
- The glyph aliases are load-bearing: alias expansion runs before
  globbing, which is what lets a bare `?` reach the function instead of
  filename generation, and `noglob` keeps unquoted glob characters in
  prompts literal. With pure command semantics they are the interactive
  path for the comma family.
- **Glyphs have pure command semantics (Remi, 2026-06-12, after the
  three-model comparison below).** `,`/`,,`/`,,,`/`?` are ordinary
  commands: zsh parses the line, the functions receive argv, shell
  quoting/expansion/redirects/pipes apply natively. Docs quote every
  example to teach the habit, and show `$(…)` interpolation as the
  payoff. The earlier mandatory-quote + refusal design (one commit's
  worth: bbc4cec) is superseded; the widget now captures only `+`,
  whose text is raw shell grammar that argv cannot carry (the sudo
  re-evaluation problem). Sharp edge inherited from zsh, documented in
  the README: `!` immediately before a closing double quote (`"fix
  it!"`) is zsh's `!"` history-mechanism sequence and eats the quote —
  single quotes for prompts with bangs.
- The accepted `+` line keeps showing what was typed: PREDISPLAY
  survives the final zle render (verified empirically) and is never
  parsed, so the widget sets it to the original buffer and dims the
  rewritten dispatch word with a buffer-relative `region_highlight`
  entry (character offsets, multibyte-safe). The dispatch word is a
  single `…` in UTF-8 locales — a function delegating to
  `__sigil_dispatch`, which stays the fallback word elsewhere — so the
  trailer is one dim character. Both words are excluded from history
  and from spool recording. Both persist across zle sessions, so a `zle-line-init` hook
  (chained via `add-zle-hook-widget`) clears them. Two related facts:
  the executed line is read from BUFFER *after* the widget chain
  returns — display and execution cannot be made to differ by buffer
  swapping — and a suspended external command launched from a function
  lists with empty text in `jobs` for any function, not just ours.
  zsh-syntax-highlighting may recolor the trailer (it rewrites
  region_highlight); degradation is cosmetic only.
- Harness lesson: macOS pty buffers are small; a blind sleep between
  pty writes can block the shell mid-write and make signals appear
  lost. `InteractiveZsh.settle()` drains while waiting.

## Decided 2026-06-12: glyph semantics — three models compared

**Resolution: Model 1** (pure command semantics), with quoted examples
throughout the docs and an expansion example showing what the shell buys.
The comparison is kept for the record.

Remi asked which behavior is the most Unix-friendly. The yardstick: a
Unix tool receives argv after the shell parses the line; quoting belongs
to the shell and is motivated, never required; double quotes interpolate,
single quotes are literal; composition is pipes and redirects. The three
candidate models, then a behavior matrix on concrete inputs.

**Model 1 — pure command semantics (most Unix).** Delete the widget for
the comma family: `,`/`,,`/`,,,`/`?` are ordinary commands reached
through the existing functions and `noglob` aliases. zsh parses the
line; the function receives argv. `+` keeps raw capture: argv cannot
faithfully carry arbitrary shell text (the sudo problem — re-joining
argv re-evaluates quoting), so the widget machinery survives, scoped to
`+` only. The dispatch word, stash, PREDISPLAY, dim trailer, line-init
hook, ellipsis function, and quote-refusal are deleted for the comma
family.

**Model 2 — current + shell-aligned quotes.** Keep mandatory quoting
and the refusal hint, but make the span follow shell semantics: double
quotes interpolate `$VAR` and `$(cmd)` at dispatch time; single quotes
stay literal. One deliberate deviation remains: history expansion (`!`)
is never performed inside the span — a safety improvement over the
shell, not a drift from it (bash users disable it with `set +H` for the
same reason).

**Model 3 — current as shipped.** Mandatory quoting; the span is always
literal regardless of quote type. Quoting selects nothing; it is only a
delimiter. Safest prompts, furthest from shell quoting semantics.

### Behavior matrix

| Input | 1: pure command | 2: aligned quotes | 3: shipped |
| --- | --- | --- | --- |
| `, fix the tests` | works, prompt = `fix the tests` | refused with hint | refused with hint |
| `, what's the deal` | `quote>` continuation prompt | refused with hint | refused with hint |
| `, "what's the deal"` | works | works | works |
| `, "fix it!!"` | **`!!` expands**: last command injected into the prompt, history rewritten | literal `!!` | literal `!!` |
| `, "explain $PATH"` | `$PATH` expands | `$PATH` expands | literal `$PATH` |
| `, "error: $(tail -1 e.log)"` | command runs, output in prompt | command runs, output in prompt | literal `$(…)` text |
| `, 'fix it!! $PATH'` | all literal | all literal | all literal |
| `, summarize > out.txt` | **redirects** (prompt = `summarize`) | refused with hint | refused with hint |
| `, "summarize" > out.txt` | redirects | redirects | redirects |
| `, "x" \| wc -l` | pipes | pipes | pipes |
| `, what do *.log files do` | works (`noglob` alias) | refused | refused |
| `, why (really)` | **parse error** near `(` | refused | refused |
| `, what does # mean` | `#` truncates under `interactive_comments` (oh-my-zsh default) | refused | refused |
| surprise execution in a prompt | backticks/`$(…)` anywhere unquoted or double-quoted | only inside double quotes, explicit | never |
| bare `,` / `,,` / `?` | works | works | works |
| `git diff \| , "review"` | works (alias path) | works (alias path) | works (alias path) |
| `+ cargo test \| tee log` | widget: whole line is the captured command | same | same |

### Consequences beyond parsing

- **Display.** Model 1: nothing decorated for the comma family — the
  typed line, history, and scrollback are simply real; no trailer. The
  `…` trailer remains only on `+` lines. Models 2/3: as today.
- **History.** Model 1: the shell owns it; histexpand rewrites the
  stored line (the `!!` row above also changes what up-arrow recalls).
  Models 2/3: the original typed line is restored at line-init.
- **Provenance.** Model 1 records post-expansion argv — what the model
  actually received; the typed form is gone if expansion occurred.
  Models 2/3 record the typed span (model 2: plus whatever `$(…)`
  produced inside it). All are honest at different layers.
- **Failure modes.** Model 1's sharp edges are the shell's own:
  `quote>`, parse errors, histexpand injection — familiar, documented,
  and identical to every other command. Models 2/3 replace them with
  one sigil-specific behavior (the refusal hint) that no other tool
  has.
- **Code.** Model 1 deletes roughly half the dispatch machinery and its
  tests (second rework in two days; the pty harness and `+` path stay).
  Model 2 adds a small, careful expansion step (`$`/`$(…)` only — no
  globbing, no histexpand) plus tests. Model 3 is zero change.
- **`jobs` text and Ctrl-Z** behave identically in all three (the
  empty-text quirk is zsh's function-job behavior, not ours).

### Assessment

Model 1 is the most Unix-friendly without qualification, and it matches
the project's stated posture (explicit, inspectable, "this file should
stay boring", CLI as the source of truth). Its real cost is one class
of accident: silent expansion in double-quoted prompts (`!!`, `$`,
backticks) sending unintended text to the model — the shell's knives,
kept sharp. Model 2 keeps exactly one guardrail (no histexpand, plus
the refusal teaching the quoted form) at the cost of one nonstandard
behavior. Model 3 is the safest and the least shell-like; its quoting
is sigil grammar, not zsh grammar.

The honest framing: 1 trusts the user with the shell, 2 files down one
knife, 3 replaces the knife block. Pick by product identity, not by
safety argument alone — all three are defensible.

---

# Proposal: Sigil tool contracts and reviewable writes

Direction: tools are Sigil capabilities, not Zeta internals. Zeta is one
orchestrator over them.

## Boundary

- **`sigil.tools` owns executable capabilities:** implementations,
  CLI behavior, validation, effects, mutation semantics, JSON result
  shape, and eventually staging/review mechanics.
- **`sigil.zeta` owns model-facing exposure:** `ToolSpec` (or
  `ZetaToolSpec`), JSON Schema for model calls, descriptor rendering,
  prompt wording, and model-call validation.
- Zeta can adapt Sigil tools into model tools, but the executable
  contract must also be callable as a CLI.
- The invariant: Zeta never has a tool schema that the CLI cannot
  validate and run.

The concrete built-ins already live in `src/sigil/tools/` with the
model-facing `ToolSpec` protocol and registry under `sigil.zeta.tools`;
what remains is the contract-backed CLI surface below.

## Contract surfaces

Each tool should expose the same four surfaces:

```sh
sigil tool metadata write
sigil tool schema write
sigil tool validate write --stdin
sigil tool run write --stdin
```

`validate` and `run` read JSON params from stdin or a params file. A
generic JSON-param interface is the first stable layer because tools and
plugins are dynamic:

```sh
echo '{"path":"x.txt","content":"hi"}' | sigil tool validate write
echo '{"path":"x.txt","content":"hi"}' | sigil tool run write
```

Friendly per-tool porcelain can come later, but the source of truth is
the contract-backed JSON path.

## Enforcing CLI options

Use one shared tool contract so the CLI and Zeta schema cannot drift:

- The contract defines args/options, required fields, defaults,
  effects, interactivity, description, and result expectations.
- CLI parsing is generated from or checked against the contract.
- Zeta JSON Schema is generated from or checked against the same
  contract.
- Runtime validation uses the same contract before execution.

Before a staged-write flag exists, both of these should fail:

```sh
sigil tool run write --staged ...
```

and any model call with a `staged` field. Later, adding staged writes is
one contract change, not separate CLI and Zeta changes.

## CLI adapter first, not a rewrite

Current code already has the right internal split: `ToolSpec`,
`analyze`, `run`, `stage`, `run_tool`, and CLI-backed plugins. First
implementation should add a CLI adapter over the existing registry:

```sh
sigil tool list
sigil tool metadata read
sigil tool schema read
sigil tool analyze write --json-params '{"path":"x.txt","content":"hi"}'
sigil tool validate write --json-params '{"path":"x.txt","content":"hi"}'
sigil tool run write --json-params '{"path":"x.txt","content":"hi"}'
```

For now, no `--staged`: `run` means direct execution, equivalent to the
auto-approved `,,,` workflow. Zeta can continue to call tools
in-process for speed as long as both paths share the same adapter/core
behavior. A hard subprocess boundary can come later.

## Built-ins as plugin-compatible CLIs

Expose built-ins with the same protocol expected from external tools:

```sh
sigil tool serve write --metadata
sigil tool serve write --schema
sigil tool serve write --validate   # JSON params on stdin
sigil tool serve write              # run JSON params on stdin
```

Then a built-in can be registered elsewhere as:

```toml
[[tools]]
kind = "command"
command = ["sigil", "tool", "serve", "write"]
```

This keeps the command-tool protocol real and testable.

## Python-library tools

Support in-process tool registration alongside binary tools.

TOML examples:

```toml
[[tools]]
kind = "command"
command = ["my-tool"]

[[tools]]
kind = "python"
module = "my_package.sigil_tools"
object = "TOOLS"
```

Installed packages can also expose entry points:

```toml
[project.entry-points."sigil.tools"]
my_package = "my_package.sigil_tools:sigil_tools"
```

The loaded object can be a list of contracts or a factory:

```python
TOOLS = [FETCH_ISSUE]

def sigil_tools():
    return [FETCH_ISSUE]
```

Python tools are trusted in-process code. Command tools run
out-of-process with timeout/stderr capture. Zeta should not care about
origin; it sees registered contracts.

## Reviewable writes outside Git

The write/edit mutation primitive should eventually own staging. That
keeps behavior consistent for CLI use, Zeta, plugins, and non-Git
directories.

Future invariant:

```text
No staged write mutates the real workspace.
Only staged apply mutates the real workspace.
```

Use three trees for proposal review:

- `base`: snapshot before agent edits
- `proposal`: agent-edited tree
- `accepted`: what will be applied to the real workspace

Diff generation does not require a Git repo:

```sh
git diff --no-index --color=always base/ proposal/
```

Pipe to `delta` when installed; otherwise fall back to colored diff or
plain unified diff.

## Review flow for non-Git dirs

The review flow should not require the real project to be a Git repo.
It should present the diff between `base` and `proposal`, let users
accept or reject changes into `accepted`, then apply only `accepted` to
the real workspace.

The useful unit is the proposal batch, not each write call. Let the
model finish a coherent proposal batch, then review the whole patch
once.

## Future staged write shape

Potential commands:

```sh
sigil tool run write --staged ...
sigil staged diff
sigil staged review
sigil staged apply
sigil staged discard
```

But `--staged` should be an enforced tool/runtime mode, not merely a
prompt convention. In `,,`, the runner should force mutating tools into
staged/proposal mode by construction; prompts can describe that mode but
must not be the safety mechanism.

---

# Proposal: web tools (web_search, web_fetch)

Give zeta eyes beyond the filesystem: `web_fetch` retrieves one URL as
text; `web_search` queries a search backend and returns ranked results
(title, URL, snippet) for `web_fetch` to follow.

## Observations

- The tool architecture absorbs this cleanly: one module per tool in
  `zeta/tools/` exporting `SCHEMA`, `SPEC`, `analyze`, `run`; one
  registration line in `BUILTIN_TOOL_IMPLS`. Validation, descriptors,
  the propose-mode contract, and registry plumbing are all generic.
- Both tools are read-only in the effects vocabulary (`read`/`search` ∈
  `READ_ONLY_EFFECT_KINDS`), so they run unstaged in every workflow —
  including ask — with no staging implementation needed. `mutates()`
  returns False; the contract machinery needs no change.
- The `Resource` literal in `tools/base.py` is `path|process|session` —
  no network member. Nothing validates it beyond the type hint
  (`protocols.py` never mentions resource), so adding `"url"` is a
  one-line honest extension; effect targets become the URL/query.
  Ledger tie-in for free: once effects land in `sigil.effect.v1`, "what
  it fetched" joins "what it saw".
- HTTP precedent is stdlib: `model.py` uses `urllib.request`, and the
  dependency list (click, jinja2, jsonschema, rich) is deliberately
  minimal. Both tools can be stdlib-only.
- There is no provider-side search to lean on: the model boundary is a
  bare OpenAI-compatible chat endpoint (llama.cpp on localhost by
  default). Search must be an HTTP call sigil makes itself, against
  some backend.
- Conventions to follow: result shape `{ok, content: [{type: "text",
  text}], metadata}`, ~12k char cap with `truncated`/`max_chars`
  metadata (grep), binary rejection (read), per-tool one-liners in
  `display/summarize.py` (dispatches on tool name), indicative
  `test_zeta_tool_*` tests in `test_zeta_tools.py` with monkeypatch —
  no network in tests.

## Contract decisions

1. **Builtin, not plugin.** General-purpose, wants the truncation
   conventions, summarize entries, and tests; the plugin path is for
   user-specific tooling.
2. **Stdlib HTTP, stdlib HTML.** `urllib.request` with a hard timeout
   (~15s), bounded read (cap bytes before decoding), http/https schemes
   only. Hosted providers are plain JSON over HTTPS — no SDK
   dependency. The floor for unproxied fetches: HTML→text via a small
   `html.parser` extractor (drop script/style, collapse whitespace);
   non-HTML text passes through; binary content-type is an error.
3. **One provider seam backs both tools.** Not a per-tool backend: a
   configured *web provider* exposes `search(query, objective, limit)`
   and optionally `extract(url)`; chosen by config/env, never by the
   model. Tool schemas stay provider-neutral (`web_search`: `query`,
   optional `objective` + `limit`; keyword-only backends ignore
   `objective`). web_fetch routes through the provider's extract when
   it has one, the urllib floor otherwise — so fetch always works,
   even unconfigured. Unconfigured web_search returns an error_result
   that says exactly what to set.
   - **Parallel (proposed v1 provider).** Search API: objective +
     queries → LLM-ready compressed excerpts in one round trip.
     Extract API: URL → markdown, handles JS-rendered pages and PDFs.
     They map onto web_search/web_fetch one-to-one. `PARALLEL_API_KEY`;
     ~$5/1k searches, free tier ~16k requests. The decisive argument
     for sigil: the default model is small and local — the multi-hop
     browse loop (search → pick → fetch → extract → repeat) is what it
     is worst at, and every hop is a model step; dense excerpts
     collapse the loop. SearXNG-scrape + naive extraction pushes
     quality onto the weakest component of the system.
   - **SearXNG (keyless, self-hosted, later or alongside).**
     `SIGIL_SEARXNG_URL`, JSON API, search only (fetch uses the urllib
     floor). Keeps a no-third-party option alive for the local-first
     posture.
4. **Network egress is a documented posture change.** Today `,` with a
   local model means nothing leaves the machine. Web tools break that:
   the agent can make outbound requests shaped by your prompt and files
   (prompt-injection exfiltration is the classic failure), and a hosted
   provider additionally sees every query and model-written objective —
   sharper than SearXNG, which is a box you run. Same answer as
   recording: stated contract, README section, opt-out — not silence.

## Work items (each step: tests → impl → docs → pre-commit)

1. `tools/base.py`: add `"url"` to `Resource`.
2. Provider seam: `zeta/tools/web.py` (or similar) — provider
   selection from config/env, the Parallel provider (search + extract,
   urllib JSON calls), and the urllib/`html.parser` fetch floor; tests
   with monkeypatched openers, no network.
3. `web_fetch`: `zeta/tools/web_fetch.py` (SCHEMA/SPEC/analyze/run,
   effects `("read",)`, target = URL); provider extract when
   configured, floor otherwise; tests (success, provider routing,
   redirect → final_url metadata, timeout, non-http scheme, binary
   content-type, truncation); register in `BUILTIN_TOOL_IMPLS`;
   `summarize.py` one-liner; README tool list.
4. `web_search`: `zeta/tools/web_search.py` over the seam; tests with
   a fake provider response (excerpts render as numbered
   title/URL/excerpt text, limit honored, unconfigured → instructive
   error, provider HTTP failure → error_result); register; summarize;
   README.
5. Enablement: add both to `ASK_TOOLS` (pending open question 2) and
   document the egress contract in the README.

## Open questions for Remi

1. v1 provider: Parallel only (proposal — covers both tools, free
   tier for the alpha), SearXNG only (no third party, no key), or
   both from the start? Exa/Tavily/Brave are same-shaped providers
   addable behind the seam whenever.
2. Default-on in ask (`,`), or opt-in via `--tools`/config? Proposal:
   default-on with the README contract plus a `SIGIL_WEB=0`-style
   opt-out — discoverability beats purity here, and the trust model is
   already local-user-local-trust.
3. web_fetch address policy: allow any http/https target (local trust,
   proposal), or refuse loopback/private ranges to blunt
   injection-driven probing of local services?
4. When web_search is unconfigured, is the tool still advertised to the
   model (proposal: yes, error teaches the user to configure it) or
   hidden from the descriptor list (no wasted model step)?

---

# Roadmap: delegation ledger

The trace of what you delegated becomes the product. `?` grows from a
one-bit status into the query surface over your entire delegation
history — what ran, under which contract, what it touched, what it cost,
what it saw. The successor to shell history.

Anti-goal: `?` stays instant and model-free. The ledger is plain data;
the NL layer sits on top and cites it, never replaces it.

Stages 1–3 are landed: records from every workflow chokepoint; the
global `ledger.sqlite3` index + `sigil log reindex`; the trace-graph
bridge (`turn/<id>` refs, one id namespace with `trace show`); the
query surface (`sigil log` with filters, `blame`, `log show`, `?` v2
with last/staged/today lines) and the `query_log` ask tool with cited
turn ids. Both graduation checks hold: rotation loses no
turn/effect/cost answer, and `, what did I delegate yesterday?`
answers with checkable citations.

## Stage 4 — Durable, global, portable

1. Cross-session by default: `sigil log` queries the machine-wide
   ledger; session scoping becomes a filter, not the universe (today
   everything is fragmented per `SIGIL_SESSION_ID`).
2. `sigil log export --since DATE` → portable bundle: the exported turn
   objects plus their graph closure (`graph_closure` exists) — prompts,
   components, tool results, effects in one self-contained set. Requires
   the Stage 2 bridge; makes every explorer query work on an imported
   bundle for free. The hinge to the trace-portability bet — the ledger
   is the natural unit of exchange, not raw transcripts.
3. Privacy policy as config, not accident: what is retained verbatim
   (objectives? answers?) vs hash-only; a `redact` operation that holds
   under the content-addressed model (replace blob, keep hash +
   tombstone).

Graduation: a bundle exported from one machine answers blame/show/saw
queries on another, with redaction honored.

---

# Roadmap: trace explorer

The ledger answers *what happened* — turns, effects, cost — and hands you
prompt ids. This roadmap makes the trace store answer *why* and *what it
saw* from those ids.

Stages A–C are landed: the forward index (`derivation_inputs`,
`derivations_for_input`), the resolver (ref → exact id → unique prefix,
shared with `log show`/`blame`), recency-ordered multi-kind `objects()`,
the porcelain (`trace log|show|tree`, plain text, shared one-line
renderers in `display/summarize.py`), and diff/replay (`trace diff`
with `--stat`, `trace replay` with `--model`/`--diff`, replays recorded
as `SigilModelReplay:v1` derivations; graduated live, walkthrough in
`docs/demos/trace-replay.md`). Known caveat: when many components of
the same kind change, kind-ordered diff pairing is positional — exact
for the same-objective regression-hunt case the roadmap targets,
approximate for prompts far apart in a conversation.

Structural facts to build on:

- Objects deliberately carry no timestamp (content-addressed, deduped);
  derivations carry `created_at` and order every listing.
- Content addressing makes diff almost free: identical component id =
  unchanged; only changed components need a text diff.
- Prompt objects store the payload content hash plus linked components;
  the exact request is reconstructible from the component closure, which
  is what replay and diff consume.

## Stage D — Scope: cross-session and search

1. `--session ID` (and `--all-sessions` where it makes sense) on the
   trace group. The store path becomes an explicit parameter
   (`default_store(session_id=...)`), not ambient state. Read-only opens
   of other sessions' stores.
2. `trace grep PATTERN [--kind K]` — SQLite LIKE scan over `data_json`
   first; upgrade to FTS5 only if real usage demands it, decided
   together with the shared-index question, not separately.

Graduation: "which session was I in when I asked about X last week" is
answerable from the CLI without opening sqlite3 by hand.
