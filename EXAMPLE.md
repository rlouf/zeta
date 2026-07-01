# Example emacs frontend

? Can you look at the files in this project and tell me what it's about?

Zeta:
  Yes. Looking across the project, **Commas is a terminal-native LLM assistant/runtime** built around zsh punctuation shortcuts and a Python CLI.
  
  At a high level, it does four things:
  
  1. **Shell UX**
     - Provides zsh bindings in `src/commas/bindings/commas.zsh`.
     - Lets a user type lightweight prefixes like:
       - `,` ask a question
       - `,,` propose reviewed work
       - `,,,` do one approved step
       - `+` run a command and record it
       - `?` show session status
  
  2. **Python CLI/runtime**
     - The installed command is `commas`, defined in `pyproject.toml`.
     - CLI modules live under `src/commas/cli/`: install, run, status, session, log, trace, step, model, events.
     - The CLI is described as the “boring” boundary that shell integrations call instead of reimplementing model calls, rendering, or state.
  
  3. **Agent + tool loop**
     - `src/commas/zeta/` contains the embedded agent runtime, called **Zeta**.
     - It runs native model tool-call loops, supports allowed tools, staged vs direct execution, model profiles, prompt building, prompt tracing, and timeline continuity.
     - Tool implementations include read/list/grep/write/edit/bash/query-log style operations under `src/commas/tools/` and `src/commas/zeta/tools/`.
  
  4. **Durable session/state/ledger system**
     - `src/commas/state.py`, `session.py`, `ledger.py`, `events.py`, and `protocols.py` define persistent records for turns, effects, shell handoffs, failures, and outcomes.
     - It records what the model saw, what it did, what was staged/executed, and what command/file effects occurred.
     - Docs like `docs/zeta-prompt-trace.md` show a strong emphasis on traceability: prompts are stored as durable object graphs, enabling replay, diffs, context compaction, and inspection.
  
  So the project is not just “an AI shell wrapper.” It’s closer to a **carefully sandboxed, inspectable LLM interaction layer for the terminal**, with separate workflows for asking, proposing, doing, and running commands, plus a trace/ledger system to keep the agent’s context and effects auditable.
