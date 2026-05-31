# Sigil zsh bindings. Core behavior lives in the `sigil` executable.
#
# This file should stay boring: it wires zsh lifecycle hooks and punctuation
# functions to the CLI, but it should not implement model logic or command
# execution policy itself. In particular, ordinary shell commands are not
# wrapped or redirected here.

# ── CLI Resolution ───────────────────────────────────────────────────────

# Resolve the CLI once at source time. SIGIL_BIN lets tests, local checkouts, and
# packaged installs point the binding at a specific executable without changing
# the user's PATH.
if [[ -n "${SIGIL_BIN:-}" ]]; then
  typeset -g __sigil_bin="$SIGIL_BIN"
elif command -v sigil >/dev/null 2>&1; then
  typeset -g __sigil_bin="sigil"
else
  typeset -g __sigil_bin="sigil"
fi

# ── Session And Terminal Context ─────────────────────────────────────────

# A session id scopes continuity files such as recent-turns.jsonl. The id is
# generated once per shell process and inherited by subprocesses so CLI calls from
# the same terminal window write to the same session directory.
if [[ -z "${SIGIL_SESSION_ID:-}" ]]; then
  if command -v uuidgen >/dev/null 2>&1; then
    export SIGIL_SESSION_ID="$(uuidgen)"
  else
    __sigil_session_tty="${TTY:-tty}"
    export SIGIL_SESSION_ID="${__sigil_session_tty:t}-$$"
    unset __sigil_session_tty
  fi
fi

# Keep a stable terminal path/fd around for CLI prompts that need to ask the user
# even when stdin/stdout are part of a pipeline. These are not used to capture
# command output.
if [[ -z "${SIGIL_TTY:-}" ]]; then
  if [[ -n "${TTY:-}" ]]; then
    export SIGIL_TTY="$TTY"
  else
    __sigil_tty_path="$(tty 2>/dev/null || true)"
    [[ -n "$__sigil_tty_path" && "$__sigil_tty_path" != "not a tty" ]] && export SIGIL_TTY="$__sigil_tty_path"
  fi
fi

if [[ -z "${SIGIL_TTY_FD:-}" && ( -t 0 || -t 1 || -t 2 ) ]]; then
  if exec {__sigil_confirmation_tty_fd}<>/dev/tty 2>/dev/null; then
    export SIGIL_TTY_FD="$__sigil_confirmation_tty_fd"
  fi
fi

# ── Prompt And History Helpers ───────────────────────────────────────────

__sigil_history_insert() {
  # Add a command to zsh history without executing it. Used when Sigil proposes
  # a command so normal history search can find it later.
  [[ -n "${1:-}" ]] || return 0
  print -s -- "$1" 2>/dev/null || true
}

__sigil_prompt_insert() {
  # zsh can preload editable text into the prompt buffer with print -z. This is
  # what makes comma recommendations inspectable instead of immediately run.
  [[ -n "${1:-}" ]] || return 0
  print -z -- "$1" 2>/dev/null || true
  __sigil_history_insert "$1"
}

__sigil_glyphs_enabled() {
  # `sigil install --no-glyphs` writes SIGIL_ENABLE_GLYPHS=0 before sourcing this
  # file. The named shell functions remain available either way.
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

# ── Command Wrappers ─────────────────────────────────────────────────────

sigil_command() {
  # `, prompt`: ask the model for one command, print the response, and insert the
  # first line back into the editable prompt buffer.
  local response command
  response="$("$__sigil_bin" op "," "$@")" || return $?
  print -r -- "$response"
  command="${response%%$'\n'*}"
  __sigil_prompt_insert "$command"
}

__sigil_op() {
  # Shared helper for agent/goal routes. They run through the generic operator
  # CLI; command approval for Pi shell tools happens inside that foreground run.
  local op="$1"
  shift
  "$__sigil_bin" op "$op" "$@"
}

sigil_agent_step() {
  __sigil_op ",," "$@"
}

sigil_agent_step_auto() {
  __sigil_op ",,," "$@"
}

sigil_question() {
  "$__sigil_bin" op "?" "$@"
}

sigil_web_question() {
  "$__sigil_bin" op "??" "$@"
}

sigil_run() {
  # Explicit capture path: run exactly the argv the user provided, stream output
  # live, and let the CLI persist bounded stdout/stderr snippets.
  "$__sigil_bin" run "$@"
}

sigil_goal() {
  __sigil_op "@" "$@"
}

sigil_goal_auto() {
  __sigil_op "@@" "$@"
}

# ── Glyph Bindings ───────────────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  # Function definitions make the punctuation usable in non-alias contexts.
  function ',' { sigil_command "$@" }
  function ',,' { sigil_agent_step "$@" }
  function ',,,' { sigil_agent_step_auto "$@" }
  function '?' { sigil_question "$@" }
  function '??' { sigil_web_question "$@" }
  function '+' { sigil_run "$@" }
  function '@' { sigil_goal "$@" }
  function '@@' { sigil_goal_auto "$@" }

  # Aliases keep zsh from treating user prompts as glob patterns before our
  # functions receive them. `alias --` is required for `+` because zsh otherwise
  # parses the alias name as an option.
  alias ','='noglob sigil_command'
  alias ',,'='noglob sigil_agent_step'
  alias ',,,'='noglob sigil_agent_step_auto'
  alias '?'='noglob sigil_question'
  alias '??'='noglob sigil_web_question'
  alias -- '+'='noglob sigil_run'
  alias '@'='noglob sigil_goal'
  alias '@@'='noglob sigil_goal_auto'
fi

# ── zsh Command Lifecycle Hooks ──────────────────────────────────────────

autoload -Uz add-zsh-hook
typeset -g __sigil_current_command=""

__sigil_before_command() {
  # preexec runs before the command executes.
  # zsh gives us the command line before execution.
  __sigil_current_command="$1"
}

add-zsh-hook preexec __sigil_before_command

__sigil_recordable_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 1

  case "$command" in
    # Match shell history convention: leading-space commands are private.
    [[:space:]]*)
      return 1
      ;;
    # Sigil glyph routes are prompts/instructions, not ordinary shell commands.
    # The CLI paths they call record their own structured events.
    ,*|\?*|+*|@*)
      return 1
      ;;
    # Avoid recursive bookkeeping for direct Sigil calls and internal wrappers.
    sigil\ *|sigil_*|noglob\ sigil_*|command\ sigil_*|__sigil_*)
      return 1
      ;;
  esac

  return 0
}


__sigil_record_turn() {
  local exit_status="$1"
  local command="$2"
  local stdout_snippet stderr_snippet
  # Snippets come from explicit paths such as `sigil run` used to record the
  # output of commands.
  stdout_snippet="${SIGIL_FAILURE_STDOUT:-}"
  stderr_snippet="${SIGIL_FAILURE_STDERR:-}"
  local record_args=(record-turn --status "$exit_status" --cwd "$PWD")
  [[ -n "$stdout_snippet" ]] && record_args+=(--stdout-snippet "$stdout_snippet")
  [[ -n "$stderr_snippet" ]] && record_args+=(--stderr-snippet "$stderr_snippet")
  # Recording must never perturb the user's shell. All CLI output is silenced and
  # failures are ignored; losing a telemetry event is preferable to breaking the
  # prompt after every command.
  "$__sigil_bin" "${record_args[@]}" "$command" >/dev/null 2>&1 || true
}

__sigil_after_command_before_prompt() {
  # precmd runs after the command finishes and before the next prompt. At this
  # point `$?` is the command's exit status and `$PWD` is the final cwd. We pair
  # those with the command line captured in preexec.
  local exit_status=$?
  local command="$__sigil_current_command"
  __sigil_current_command=""
  if [[ -z "$command" ]]; then
    # No preceding command, e.g. first prompt in a new shell.
    return "$exit_status"
  fi
  if ! __sigil_recordable_command "$command"; then
    # Sigil's own punctuation routes are handled by their explicit CLI paths.
    return "$exit_status"
  fi
  __sigil_record_turn "$exit_status" "$command"
  unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
  return "$exit_status"
}

add-zsh-hook precmd __sigil_after_command_before_prompt

# ── History Filtering ────────────────────────────────────────────────────

# Shell history should stay a list of things the shell can re-run. Sigil
# instructions are prompts, not shell commands.
if __sigil_glyphs_enabled; then
  __sigil_zshaddhistory() {
    emulate -L zsh
    local line="${1%%$'\n'}"
    case "$line" in
      ,*|\?*|\\\?*|+*|@*) return 1 ;;
    esac
    return 0
  }
  add-zsh-hook zshaddhistory __sigil_zshaddhistory
fi
