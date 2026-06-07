# Sigil zsh bindings. Core behavior lives in the `sigil` executable.
#
# This file should stay boring: it wires zsh lifecycle hooks and punctuation
# functions to the CLI. The Zeta glyph route keeps prompt insertion and command
# capture here, but delegates the model/tool loop to Python.

export SIGIL_BINDING_LOADED="zsh"

# ── CLI Resolution ───────────────────────────────────────────────────────

# Resolve the CLI once at source time. SIGIL_BIN lets tests, local checkouts, and
# packaged installs point the binding at a specific executable without changing
# the user's PATH.
if [[ -n "${SIGIL_BIN:-}" ]]; then
  typeset -g __sigil_bin="$SIGIL_BIN"
elif command -v sigil >/dev/null 2>&1; then
  typeset -g __sigil_bin="$(command -v sigil)"
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
  if { exec {__sigil_confirmation_tty_fd}<>/dev/tty; } 2>/dev/null; then
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

__sigil_zeta_prompt_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 0
  print -r -- "+ $command"
}

__sigil_json_string() {
  python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"
}

__sigil_json_get() {
  python3 -c '
import json, sys
data = json.load(sys.stdin)
value = data
for part in sys.argv[1].split("."):
    if not isinstance(value, dict) or part not in value:
        value = ""
        break
    value = value[part]
if isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
elif value is not None:
    print(value)
' "$1"
}

__sigil_glyphs_enabled() {
  # `sigil install --no-glyphs` writes SIGIL_ENABLE_GLYPHS=0 before sourcing this
  # file. The named shell functions remain available either way.
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

# ── Zeta Continuation Capture ────────────────────────────────────────────

typeset -g __sigil_zeta_capture_active="${__sigil_zeta_capture_active:-0}"
typeset -g __sigil_zeta_current_command=""

__sigil_zeta_enable_capture() {
  __sigil_zeta_capture_active=1
}

__sigil_zeta_consume_capture() {
  __sigil_zeta_capture_active=0
  __sigil_zeta_current_command=""
}

__sigil_zeta_recordable_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  [[ -n "${command//[[:space:]]/}" ]] || return 1
  case "$command" in
    [[:space:]]*|,*|+*|sigil\ *|zeta\ *|__sigil_*|sigil_*|noglob\ sigil_*|noglob\ ,*|noglob\ +*)
      return 1
      ;;
  esac
  return 0
}

__sigil_zeta_record_shell_turn() {
  local command="$1"
  local exit_status="$2"
  local payload stdout_snippet stderr_snippet
  stdout_snippet="${SIGIL_FAILURE_STDOUT:-}"
  stderr_snippet="${SIGIL_FAILURE_STDERR:-}"
  payload="$(printf '{"command":%s,"status":%s,"cwd":%s,"stdout_snippet":%s,"stderr_snippet":%s}' \
    "$(__sigil_json_string "$command")" \
    "$exit_status" \
    "$(__sigil_json_string "$PWD")" \
    "$(__sigil_json_string "$stdout_snippet")" \
    "$(__sigil_json_string "$stderr_snippet")")"
  printf '%s\n' "$payload" | "$__sigil_bin" transcript shell-turn >/dev/null 2>&1 || true
  unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
}

__sigil_zeta_before_command() {
  __sigil_zeta_current_command="${1:-}"
}

__sigil_zeta_after_command_before_prompt() {
  local exit_status=$?
  local command="$__sigil_zeta_current_command"
  __sigil_zeta_current_command=""
  if [[ "$__sigil_zeta_capture_active" == "1" ]] && __sigil_zeta_recordable_command "$command"; then
    __sigil_zeta_record_shell_turn "$command" "$exit_status"
  fi
  return "$exit_status"
}

# ── Command Wrappers ─────────────────────────────────────────────────────

sigil_command() {
  # `, prompt`: read-only assistant answer. It does not stage commands or mutate
  # history; `,,` and `,,,` are the routes that can hand a command back to zsh.
  if [[ "$#" == "0" ]]; then
    "$__sigil_bin" ask
  else
    "$__sigil_bin" ask "$*"
  fi
}

__sigil_zeta_turn() {
  local glyph="$1"
  shift || true
  local objective handoff_file step_status command
  local -a args
  args=()
  objective="$*"
  handoff_file="$(mktemp "${TMPDIR:-/tmp}/sigil-handoff.XXXXXX")" || return 1
  if [[ -z "$objective" ]]; then
    __sigil_zeta_consume_capture
    args+=(--continue)
  fi
  "$__sigil_bin" zeta-step --glyph "$glyph" --handoff-file "$handoff_file" "${args[@]}" "$objective"
  step_status=$?
  if [[ "$step_status" == "0" && -s "$handoff_file" ]]; then
    command="$(__sigil_json_get command < "$handoff_file" 2>/dev/null || true)"
    if [[ -n "$command" ]]; then
      __sigil_zeta_enable_capture
      __sigil_prompt_insert "$(__sigil_zeta_prompt_command "$command")"
    fi
  fi
  rm -f "$handoff_file"
  return "$step_status"
}

sigil_agent_step() {
  __sigil_zeta_turn ",," "$@"
}

sigil_agent_step_auto() {
  __sigil_zeta_turn ",,," "$@"
}

sigil_run() {
  # Explicit capture path: run exactly the argv the user provided, stream output
  # live, and let the CLI persist bounded stdout/stderr snippets.
  "$__sigil_bin" run "$@"
}

sigil_status() {
  "$__sigil_bin" status "$@"
}

# ── zsh Raw Plus Capture ─────────────────────────────────────────────────

typeset -g __sigil_plus_capture_widget_installed="${__sigil_plus_capture_widget_installed:-0}"

__sigil_plus_capture_command() {
  local line="${1:-}"
  if [[ "$line" =~ '^\+[[:space:]]+(.+)$' ]]; then
    local command="${match[1]}"
    [[ -n "${command//[[:space:]]/}" ]] || return 1
    print -r -- "$command"
    return 0
  fi
  return 1
}

__sigil_run_plus_capture_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  SIGIL_RUN_SHELL="${SIGIL_RUN_SHELL:-${SHELL:-zsh}}" "$__sigil_bin" run --shell "$command"
}

__sigil_run_plus_capture_line() {
  local command
  command="$(__sigil_plus_capture_command "${1:-}")" || return 1
  __sigil_run_plus_capture_command "$command"
}

__sigil_accept_line_with_plus_capture() {
  local command status
  command="$(__sigil_plus_capture_command "$BUFFER")" || {
    zle __sigil_accept_line_without_plus_capture
    return $?
  }

  BUFFER=""
  CURSOR=0
  zle -I
  print -r --
  __sigil_run_plus_capture_command "$command"
  status=$?
  zle reset-prompt
  return "$status"
}

__sigil_install_plus_capture_widget() {
  [[ $- == *i* ]] || return 0
  [[ "$__sigil_plus_capture_widget_installed" == "1" ]] && return 0
  zle -A accept-line __sigil_accept_line_without_plus_capture 2>/dev/null || return 0
  zle -N accept-line __sigil_accept_line_with_plus_capture 2>/dev/null || return 0
  __sigil_plus_capture_widget_installed=1
}

# ── Glyph Bindings ───────────────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  # zsh treats bare `?` as a glob pattern before command dispatch. Disabling
  # that pattern keeps `?` available as a Sigil glyph.
  disable -p "?" 2>/dev/null || true

  # Function definitions make the punctuation usable in non-alias contexts.
  function ',' { sigil_command "$@" }
  function ',,' { sigil_agent_step "$@" }
  function ',,,' { sigil_agent_step_auto "$@" }
  function '+' { sigil_run "$@" }
  function '?' { sigil_status "$@" }

  # Aliases keep zsh from treating user prompts as glob patterns before our
  # functions receive them. `alias --` is required for `+` because zsh otherwise
  # parses the alias name as an option.
  alias ','='noglob sigil_command'
  alias ',,'='noglob sigil_agent_step'
  alias ',,,'='noglob sigil_agent_step_auto'
  alias -- '+'='noglob sigil_run'
  alias '?'='noglob sigil_status'

  __sigil_install_plus_capture_widget
fi

# ── zsh Command Lifecycle Hooks ──────────────────────────────────────────

autoload -Uz add-zsh-hook
add-zsh-hook preexec __sigil_zeta_before_command
add-zsh-hook precmd __sigil_zeta_after_command_before_prompt

# ── History Filtering ────────────────────────────────────────────────────

# Shell history should stay a list of things the shell can re-run. Sigil
# instructions are prompts, not shell commands.
if __sigil_glyphs_enabled; then
  __sigil_zshaddhistory() {
    emulate -L zsh
    local line="${1%%$'\n'}"
    case "$line" in
      ,*|\?*|+*) return 1 ;;
    esac
    return 0
  }
  add-zsh-hook zshaddhistory __sigil_zshaddhistory
fi
