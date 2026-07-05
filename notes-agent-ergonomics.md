# Agent-setup ergonomics

Goal: make standing up a new agent nearly free. Today, adding one input source
cost a Python connector package (pyproject + `zeta.event_connectors` entry point +
`uv pip install -e`), a per-source event schema, `__VAULT__` templating with an
`install.sh` substitution step + a `.vault-path` state file, and launchd. Almost
none of that is the agent's actual logic. Four changes remove almost all of it:

1. **`base_dir`** — agents use vault-relative paths; no path injection/templating.
2. **`connectors/` auto-discovery** — drop a `.py` in, it's picked up; no packaging.
3. **Filesystem connector** (core) — one generic "watch a dir → emit `file.created`"
   so every future input reuses it instead of a bespoke connector.
4. **Scaffolder** — `zeta agent new "<description>"` writes a valid agent file.

End state: a new agent is one self-contained markdown file dropped in `agents/`;
a new input is one file dropped in `agents/connectors/` (or a dumb collector that
writes into `inbox/`). No install step, no packaging, no templating.

---

## 1. `base_dir` (per-agent base directory for relative paths)

**Why:** the prompt can only reference the `event` root, so the vault path can't
be a template var — today it's baked in as literal absolute text via a substitution
step. Instead, let the agent use relative paths (`inbox/`, `relationships/prospects/`)
resolved against a per-agent base directory.

**Seam:** file tools are pure `params -> result`; `TransitionalInProcessHost.call`
(`capabilities/host.py:130`) does `del ctx`. `base_dir` must arrive as ambient
context. That is the `cwd` field the multi-host **M0-A `ToolCallContext`** already
plans (`notes.md`). So `base_dir` is a *slice* of that work, not a competing seam.

**Design:**
- `AgentSpec.base_dir: Path | None` from frontmatter (`agents/spec.py:load_spec`,
  supported-field set at `spec.py:20`). `~`-expanded, stored absolute.
- Flow it into the execution context and onto `ToolCallContext.cwd` when the
  invocation is built (`capabilities/execution.py` `invoke_hosted_capability`).
- In-process resolution (works today, async-safe): a `contextvars.ContextVar`
  `_base_dir`. `TransitionalInProcessHost.call` sets it from `ctx.cwd` for the
  duration of the call (replaces the `del ctx`). File tools (`read`, `write`,
  `edit`, `ls`, `grep`) route their path field through one shared
  `resolve_path(p)` = `p if absolute else base_dir / p`. `bash` uses `base_dir`
  as its subprocess `cwd`.
- Forward-compatible: when the subprocess host lands (M1), it launches with real
  `cwd = ctx.cwd`; `resolve_path` becomes a no-op there. Same `base_dir` frontmatter,
  no agent change.

**Tests (first):**
- `spec` parses `base_dir`, expands `~`, rejects a non-absolute-after-expand value.
- `resolve_path` joins relative, passes absolute through, with base set/unset.
- a `read`/`write` through the in-process host with `ctx.cwd` set lands under base.
- concurrency: two calls with different bases don't leak (contextvar is task-local).

**Decision:** land the minimal `cwd` on the context now as a subset of M0-A (this
*advances* the multi-host plan), or gate `base_dir` behind M0-A landing first.
Recommendation: land the `cwd` slice now — it's small and unblocks this.

---

## 2. `connectors/` auto-discovery

**Why:** a local connector shouldn't need a package + entry point + reinstall. Mirror
how `agents/` and `agents/skills/` already load from the filesystem.

**Design:**
- Scan `agents/connectors/*.py`. For each: import by file path (importlib.util
  spec_from_file_location), find the `EventConnector` — either a module-level
  `EventConnector` instance, or a zero-arg factory named `connector` /
  `<stem>_event_connector` (mirror `load_entry_point_event_connector` at
  `resources.py:191`).
- Directory drop-ins are **auto-enabled** (dropping the file = enabling it).
  `connectors.yaml` stays only for enabling *pip-installed* entry-point connectors.
- `load_connector_registry` (`resources.py:108`) gains the directory scan alongside
  `event_connector_entry_points`; connector-id uniqueness still enforced by the
  registry.

**Tests (first):**
- a `.py` in `agents/connectors/` exposing an `EventConnector` is discovered +
  registered without an entry point.
- a bad module (no `EventConnector`) raises `ResourceError` naming the file.
- entry-point + directory connectors coexist; duplicate id is a clear error.

**Decision:** location `agents/connectors/` (consistent with `agents/skills`,
`agents/events`) vs project-root `connectors/`. Recommendation: `agents/connectors/`.

---

## 3. Filesystem connector (core, drop-in)

**Why:** one generic watcher so no future input needs its own connector. Voice
Memos, reMarkable, screenshots, clippings all become "get a file into a dir."

**Design (model on `connectors/slack.py`, polled like voicememos):**
- Event `file.created` `{path, name, dir}`; schema + filter schema `{dir, glob?}`.
- Polled ingress reads `binding.filter` for the watched dir + glob; emits one
  `file.created` per new file. Reuse the voicememos **watermark** (`since` + `seen`)
  so it never floods and is TCC-safe. `idempotency_key: "file:{path}"`.
- No push ingress, no egress.
- Ships in-tree; also serves as the reference for `agents/connectors/` drop-ins.

**Tests (first):** new file after watermark → one event; pre-watermark ignored;
glob filter honored; re-scan doesn't re-emit; watermark set on empty/unreadable dir.

---

## 4. Scaffolder — `zeta agent new`

**Why:** authoring frontmatter + JSON event schema by hand is the remaining tax.

**Design:**
- `zeta agent new "<description>" [--name SLUG] [--base-dir PATH]` (click command
  in `zetad/cli.py`, the group at `cli.py:37`).
- Generate `agents/<slug>.md` with valid frontmatter (name, description, accepts,
  tools, skills, base_dir) + a Jinja body referencing `event`. **Validate before
  writing** via `load_spec` so it can never emit an invalid agent.
- v1 can be a deterministic template (no model). v2: LLM-driven from the description
  via the worker's chat-completions path (like `turbo agents add --prompt`).

**Tests (first):** generated file passes `load_spec`/`validate_agent_project`;
slug rules enforced; `--name`/`--base-dir` honored; refuses to overwrite.

**Decision:** deterministic template first, or LLM-driven immediately?

---

## 5. Refactor the voice pipeline onto the inbox model (`~/daemons`)

Converge every source on `inbox/`:
- **Voice collector** (dumb): transcribe new memos → write `inbox/<date> <slug>.md`.
  Runs as an Apple **Shortcut** (can reach Voice Memos + run a script) to dodge the
  launchd Full Disk Access problem entirely. No longer a Zeta connector.
- **Filesystem connector** watches `inbox/` (a normal vault folder — no TCC), emits
  `file.created`.
- **`inbox-filer` agent** = today's `note-filer`, generalized: `base_dir: ~/vaults/CEO`,
  `accepts: [file.created]`, relative paths, reads the dropped file and routes it
  (client note / daily / leave in inbox). One agent serves voice, reMarkable, etc.
- Delete: voicememos connector package, `__VAULT__`, `install.sh` substitution,
  `.vault-path`. `install.sh` shrinks to: enable filesystem connector + drop the agent.

---

## Sequencing (each tests-first, pre-commit clean before moving on)

1. [DONE] `base_dir` — 4 commits: frontmatter parse; resolve_path (task-local
   contextvar); thread base_dir → AgentConfig → CapabilityExecutionContext + host
   activation; file tools (read/write/edit/ls/grep/bash) resolve against it.
   624 tests green.
2. [DONE] `agents/connectors/` auto-discovery — drop-in .py (instance or factory),
   auto-enabled, coexists with entry-point connectors; bad module errors clearly.
3. [DONE] Filesystem connector — core `filesystem` connector + entry point; polled,
   watermark-based `file.created` with dir+glob filter. 634 tests green.
4. [DONE] Refactor `~/daemons` (own git repo now): voicememos connector relocated
   to `agents/connectors/voicememos.py` drop-in (no package/entry point/connectors.yaml);
   note-filer uses `base_dir: ~/vaults/CEO` + relative paths (no more `__VAULT__`,
   substitution, or `.vault-path`); install.sh slimmed. Voice collector KEPT
   (Parakeet, per Remi). Inbox/filesystem wiring deferred. Verified end-to-end on
   a scratch vault. (Also fixed a Zeta bug found here: directory connector modules
   must be registered in sys.modules before exec so @dataclass works.)
5. [DONE] Scaffolder — `zeta agent new <slug>` (template-first), validates via
   load_spec, refuses overwrite. 641 tests green.

## Status: COMPLETE. Branch `agent-ergonomics` (zeta), unpushed. `~/daemons` on its
## own branch/repo. Nothing merged to main.

## Decisions for Remi
- `base_dir`: land the `cwd` slice now vs gate behind M0-A? --> now
- connectors dir: `agents/connectors/` vs project-root `connectors/`? --> agents/connectors/
- scaffolder: template-first vs LLM-first? --> template-first
- voice collector: Apple Shortcut vs a cron script with Full Disk Access? -->
  Discuss, which is easier to set up?
