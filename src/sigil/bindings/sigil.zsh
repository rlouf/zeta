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

# ── Prompt And History Helpers ───────────────────────────────────────────

__sigil_history_insert() {
  emulate -L zsh
  # Add a command to zsh history without executing it. Used when Sigil proposes
  # a command so normal history search can find it later.
  [[ -n "${1:-}" ]] || return 0
  print -s -- "$1" 2>/dev/null || true
}

__sigil_prompt_insert() {
  emulate -L zsh
  # zsh can preload editable text into the prompt buffer with print -z. This is
  # what makes comma recommendations inspectable instead of immediately run.
  [[ -n "${1:-}" ]] || return 0
  print -z -- "$1" 2>/dev/null || true
  __sigil_history_insert "$1"
}

__sigil_zeta_prompt_command() {
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 0
  print -r -- "+ $command"
}

__sigil_glyphs_enabled() {
  emulate -L zsh
  # `sigil install --no-glyphs` writes SIGIL_ENABLE_GLYPHS=0 before sourcing this
  # file. The named shell functions remain available either way.
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

# ── Zeta Continuation Capture ────────────────────────────────────────────

# Capture stays open between a handoff and the `,,` that resumes it, but not
# for the life of the shell: it expires after SIGIL_ZETA_CAPTURE_TURNS (default
# 20) recorded commands so an abandoned handoff does not record ambiently.
typeset -g __sigil_zeta_capture_active="${__sigil_zeta_capture_active:-0}"
typeset -g __sigil_zeta_capture_remaining="${__sigil_zeta_capture_remaining:-0}"
typeset -g __sigil_zeta_current_command=""

__sigil_zeta_enable_capture() {
  emulate -L zsh
  local limit="${SIGIL_ZETA_CAPTURE_TURNS:-20}"
  [[ "$limit" == <-> ]] || limit=20
  __sigil_zeta_capture_active=1
  __sigil_zeta_capture_remaining="$limit"
}

__sigil_zeta_consume_capture() {
  emulate -L zsh
  __sigil_zeta_capture_active=0
  __sigil_zeta_capture_remaining=0
  __sigil_zeta_current_command=""
}

__sigil_zeta_recordable_command() {
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  [[ -n "${command//[[:space:]]/}" ]] || return 1
  case "$command" in
    [[:space:]]*|,*|+*|sigil\ *|__sigil_*|sigil_*|noglob\ sigil_*|noglob\ ,*)
      return 1
      ;;
  esac
  return 0
}

__sigil_zeta_record_shell_turn() {
  emulate -L zsh
  local command="$1"
  local exit_status="$2"
  "$__sigil_bin" handoff shell-turn \
    --command "$command" \
    --status "$exit_status" \
    --cwd "$PWD" >/dev/null 2>&1 || true
}

__sigil_zeta_before_command() {
  emulate -L zsh
  __sigil_zeta_current_command="${1:-}"
}

__sigil_zeta_after_command_before_prompt() {
  local exit_status=$?
  emulate -L zsh
  local command="$__sigil_zeta_current_command"
  __sigil_zeta_current_command=""
  if [[ "$__sigil_zeta_capture_active" == "1" ]] && __sigil_zeta_recordable_command "$command"; then
    __sigil_zeta_record_shell_turn "$command" "$exit_status"
    (( __sigil_zeta_capture_remaining-- ))
    if (( __sigil_zeta_capture_remaining <= 0 )); then
      __sigil_zeta_consume_capture
    fi
  fi
  return "$exit_status"
}

# ── Command Wrappers ─────────────────────────────────────────────────────

sigil_command() {
  emulate -L zsh
  # `, prompt`: read-only assistant answer. It does not stage commands or mutate
  # history; `,,` and `,,,` are the routes that can hand a command back to zsh.
  if [[ "$#" == "0" ]]; then
    "$__sigil_bin" ask
  else
    "$__sigil_bin" ask "$*"
  fi
}

__sigil_zeta_turn() {
  emulate -L zsh
  local glyph="$1"
  shift || true
  local objective handoff_file step_status command
  local -a args
  args=()
  objective="$*"
  handoff_file="$(mktemp "${TMPDIR:-/tmp}/sigil-handoff.XXXXXX")" || return 1
  # Ctrl-C during zeta-step aborts this function mid-flight; the always block
  # is the only cleanup zsh still runs on that path.
  {
    if [[ -z "$objective" ]]; then
      __sigil_zeta_consume_capture
      args+=(--continue)
    fi
    "$__sigil_bin" zeta-step --glyph "$glyph" --handoff-file "$handoff_file" "${args[@]}" "$objective"
    step_status=$?
    if [[ "$step_status" == "0" && -s "$handoff_file" ]]; then
      # The CLI writes the staged command verbatim; the substitution strips the
      # trailing newline.
      command="$(<"$handoff_file")"
      if [[ -n "$command" ]]; then
        __sigil_zeta_enable_capture
        __sigil_prompt_insert "$(__sigil_zeta_prompt_command "$command")"
      fi
    fi
  } always {
    rm -f "$handoff_file"
  }
  return "$step_status"
}

sigil_agent_step() {
  emulate -L zsh
  __sigil_zeta_turn ",," "$@"
}

sigil_agent_step_auto() {
  emulate -L zsh
  __sigil_zeta_turn ",,," "$@"
}

sigil_run() {
  emulate -L zsh
  # Explicit capture path: run exactly the argv the user provided, stream output
  # live, and let the CLI persist bounded stdout/stderr snippets.
  "$__sigil_bin" run "$@"
}

sigil_status() {
  emulate -L zsh
  "$__sigil_bin" status "$@"
}

# ── zsh Raw Plus Capture ─────────────────────────────────────────────────

# The accept-line widget is the only `+` path. It captures the raw buffer
# before zsh parses it and hands the whole line to `sigil run --shell`, so
# pipes, redirections, and multiline buffers stay part of the captured
# command. The command runs inside the line editor, which means no job
# control: Ctrl-Z cannot suspend it and it never appears in `jobs`. Outside
# zle (scripts, non-interactive shells) `+` does not dispatch; call
# `sigil_run` or the CLI directly there.
typeset -g __sigil_plus_capture_widget_installed="${__sigil_plus_capture_widget_installed:-0}"

__sigil_plus_capture_command() {
  emulate -L zsh
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
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  SIGIL_RUN_SHELL="${SIGIL_RUN_SHELL:-${SHELL:-zsh}}" "$__sigil_bin" run --shell "$command"
}

__sigil_run_plus_capture_line() {
  emulate -L zsh
  local command
  command="$(__sigil_plus_capture_command "${1:-}")" || return 1
  __sigil_run_plus_capture_command "$command"
}

__sigil_accept_line_with_plus_capture() {
  emulate -L zsh
  local command exit_status
  command="$(__sigil_plus_capture_command "$BUFFER")" || {
    zle __sigil_accept_line_without_plus_capture
    return $?
  }

  BUFFER=""
  CURSOR=0
  zle -I
  print -r --
  __sigil_run_plus_capture_command "$command"
  exit_status=$?
  zle reset-prompt
  return "$exit_status"
}

__sigil_install_plus_capture_widget() {
  emulate -L zsh
  [[ $- == *i* ]] || return 0
  [[ "$__sigil_plus_capture_widget_installed" == "1" ]] && return 0
  zle -A accept-line __sigil_accept_line_without_plus_capture 2>/dev/null || return 0
  zle -N accept-line __sigil_accept_line_with_plus_capture 2>/dev/null || return 0
  __sigil_plus_capture_widget_installed=1
}

# ── Glyph Bindings ───────────────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  # Function definitions make the punctuation usable in non-alias contexts.
  # `+` deliberately has neither a function nor an alias: the accept-line
  # widget is its only path, so a `+ ...` line always keeps shell grammar
  # intact instead of being parsed by zsh when the widget would not run.
  function ',' { sigil_command "$@" }
  function ',,' { sigil_agent_step "$@" }
  function ',,,' { sigil_agent_step_auto "$@" }
  function '?' { sigil_status "$@" }

  # Aliases keep zsh from treating user prompts as glob patterns before our
  # functions receive them.
  alias ','='noglob sigil_command'
  alias ',,'='noglob sigil_agent_step'
  alias ',,,'='noglob sigil_agent_step_auto'
  alias '?'='noglob sigil_status'

  __sigil_install_plus_capture_widget
fi

# ── zsh Command Lifecycle Hooks ──────────────────────────────────────────

__sigil_install_lifecycle_hooks() {
  emulate -L zsh
  autoload -Uz add-zsh-hook
  add-zsh-hook preexec __sigil_zeta_before_command
  add-zsh-hook precmd __sigil_zeta_after_command_before_prompt
  # $? at precmd entry is the user command's status only for the first hook;
  # any hook that runs earlier overwrites it with its own return status. Keep
  # the sigil hook first no matter when this file is sourced.
  precmd_functions=(
    __sigil_zeta_after_command_before_prompt
    ${precmd_functions:#__sigil_zeta_after_command_before_prompt}
  )
}
__sigil_install_lifecycle_hooks

# ── History Filtering ────────────────────────────────────────────────────

# The history file should stay a list of things the shell can re-run. Sigil
# instructions are prompts, not shell commands — but they stay on the internal
# history list (return 2) so up-arrow can recall and edit them in-session.
if __sigil_glyphs_enabled; then
  __sigil_zshaddhistory() {
    emulate -L zsh
    local line="${1%%$'\n'}"
    case "$line" in
      ,*|\?*|+*) return 2 ;;
    esac
    return 0
  }
  add-zsh-hook zshaddhistory __sigil_zshaddhistory
fi
