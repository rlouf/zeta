# Sigil zsh bindings. Core behavior lives in the `sigil` executable.

if [[ -n "${SIGIL_BIN:-}" ]]; then
  typeset -g __sigil_bin="$SIGIL_BIN"
elif command -v sigil >/dev/null 2>&1; then
  typeset -g __sigil_bin="sigil"
else
  typeset -g __sigil_bin="sigil"
fi
typeset -g __sigil_muted=$'\e[38;2;110;106;134m'
typeset -g __sigil_reset=$'\e[0m'
typeset -g __sigil_prompt_marker=""
typeset -g __sigil_prompt_marker_active=0
typeset -g __sigil_prompt_base=""
typeset -g __sigil_capture_active=0
typeset -g __sigil_capture_stdout_file=""
typeset -g __sigil_capture_stderr_file=""
typeset -g __sigil_capture_stdout_pipe=""
typeset -g __sigil_capture_stderr_pipe=""
typeset -g __sigil_capture_stdout_pid=""
typeset -g __sigil_capture_stderr_pid=""

if [[ -z "${SIGIL_SESSION_ID:-}" ]]; then
  if command -v uuidgen >/dev/null 2>&1; then
    export SIGIL_SESSION_ID="$(uuidgen)"
  else
    export SIGIL_SESSION_ID="${TTY:t:-tty}-$$"
  fi
fi

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

__sigil_stdin_is_pipe() {
  [[ -p /dev/stdin ]]
}

__sigil_history_insert() {
  [[ -n "${1:-}" ]] || return 0
  print -s -- "$1" 2>/dev/null || true
}

__sigil_prompt_insert() {
  [[ -n "${1:-}" ]] || return 0
  print -z -- "$1" 2>/dev/null || true
  __sigil_history_insert "$1"
}

__sigil_insert_pending_handoff() {
  local command
  command="$("$__sigil_bin" handoff pop 2>/dev/null)" || return 0
  __sigil_prompt_insert "$command"
}

__sigil_glyphs_enabled() {
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

__sigil_prompt_marker_enabled() {
  [[ -o interactive ]] || return 1
  [[ "${SIGIL_ENABLE_PROMPT_MARKER:-1}" != "0" && "${SIGIL_ENABLE_PROMPT_MARKER:-1}" != "false" ]]
}

__sigil_recordable_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  case "$command" in
    [[:space:]]*|,*|\?*|@*|sigil\ *|sigil_*|noglob\ sigil_*|command\ sigil_*|__sigil_*)
      return 1
      ;;
  esac
  return 0
}

__sigil_turn_capture_enabled() {
  [[ "${SIGIL_ENABLE_TURN_CAPTURE:-1}" != "0" && "${SIGIL_ENABLE_TURN_CAPTURE:-1}" != "false" ]] || return 1
  if [[ "${SIGIL_ENABLE_TURN_CAPTURE:-}" != "1" && "${SIGIL_ENABLE_TURN_CAPTURE:-}" != "true" ]]; then
    [[ -o interactive ]] || return 1
  fi
  command -v mktemp >/dev/null 2>&1 || return 1
  command -v mkfifo >/dev/null 2>&1 || return 1
  command -v tee >/dev/null 2>&1 || return 1
  command -v tail >/dev/null 2>&1 || return 1
  return 0
}

__sigil_capture_start() {
  setopt local_options no_bg_nice
  local command="${1:-}"
  __sigil_turn_capture_enabled || return 0
  __sigil_recordable_command "$command" || return 0
  [[ $__sigil_capture_active -eq 0 ]] || return 0

  local tmp_root="${TMPDIR:-/tmp}"
  __sigil_capture_stdout_file="$(mktemp "${tmp_root%/}/sigil-stdout.XXXXXX")" || return 0
  __sigil_capture_stderr_file="$(mktemp "${tmp_root%/}/sigil-stderr.XXXXXX")" || {
    rm -f "$__sigil_capture_stdout_file"
    __sigil_capture_stdout_file=""
    return 0
  }
  __sigil_capture_stdout_pipe="$(mktemp "${tmp_root%/}/sigil-stdout-pipe.XXXXXX")" || {
    __sigil_capture_cleanup
    return 0
  }
  __sigil_capture_stderr_pipe="$(mktemp "${tmp_root%/}/sigil-stderr-pipe.XXXXXX")" || {
    __sigil_capture_cleanup
    return 0
  }
  rm -f "$__sigil_capture_stdout_pipe" "$__sigil_capture_stderr_pipe"
  mkfifo "$__sigil_capture_stdout_pipe" || {
    __sigil_capture_cleanup
    return 0
  }
  mkfifo "$__sigil_capture_stderr_pipe" || {
    __sigil_capture_cleanup
    return 0
  }

  exec 7>&1 || return 0
  exec 8>&2 || {
    exec 7>&-
    return 0
  }
  tee "$__sigil_capture_stdout_file" < "$__sigil_capture_stdout_pipe" &
  __sigil_capture_stdout_pid="$!"
  tee "$__sigil_capture_stderr_file" < "$__sigil_capture_stderr_pipe" >&2 &
  __sigil_capture_stderr_pid="$!"
  exec > "$__sigil_capture_stdout_pipe"
  exec 2> "$__sigil_capture_stderr_pipe"
  rm -f "$__sigil_capture_stdout_pipe" "$__sigil_capture_stderr_pipe"
  __sigil_capture_active=1
}

__sigil_capture_stop() {
  [[ $__sigil_capture_active -eq 1 ]] || return 0
  exec 1>&7
  exec 2>&8
  exec 7>&-
  exec 8>&-
  [[ -n "$__sigil_capture_stdout_pid" ]] && wait "$__sigil_capture_stdout_pid" 2>/dev/null || true
  [[ -n "$__sigil_capture_stderr_pid" ]] && wait "$__sigil_capture_stderr_pid" 2>/dev/null || true
  __sigil_capture_stdout_pid=""
  __sigil_capture_stderr_pid=""
  __sigil_capture_active=0
}

__sigil_capture_file_snippet() {
  local snippet_path="${1:-}"
  local bytes="${SIGIL_TURN_CAPTURE_BYTES:-6000}"
  [[ -n "$snippet_path" && -s "$snippet_path" ]] || return 0
  tail -c "$bytes" "$snippet_path" 2>/dev/null || true
}

__sigil_capture_cleanup() {
  [[ -n "$__sigil_capture_stdout_file" ]] && rm -f "$__sigil_capture_stdout_file"
  [[ -n "$__sigil_capture_stderr_file" ]] && rm -f "$__sigil_capture_stderr_file"
  [[ -n "$__sigil_capture_stdout_pipe" ]] && rm -f "$__sigil_capture_stdout_pipe"
  [[ -n "$__sigil_capture_stderr_pipe" ]] && rm -f "$__sigil_capture_stderr_pipe"
  __sigil_capture_stdout_file=""
  __sigil_capture_stderr_file=""
  __sigil_capture_stdout_pipe=""
  __sigil_capture_stderr_pipe=""
}

__sigil_refresh_prompt_marker() {
  if ! __sigil_prompt_marker_enabled; then
    if [[ $__sigil_prompt_marker_active -eq 1 ]]; then
      PROMPT="$__sigil_prompt_base"
      PS1="$PROMPT"
      __sigil_prompt_marker=""
      __sigil_prompt_marker_active=0
    fi
    return 0
  fi

  local current_prompt="${PROMPT:-${PS1:-}}"
  if [[ $__sigil_prompt_marker_active -eq 1 ]]; then
    current_prompt="$__sigil_prompt_base"
  fi
  __sigil_prompt_base="$current_prompt"

  if "$__sigil_bin" status --json >/dev/null 2>&1; then
    __sigil_prompt_marker=""
    __sigil_prompt_marker_active=0
  else
    __sigil_prompt_marker="! "
    __sigil_prompt_marker_active=1
  fi

  PROMPT="${__sigil_prompt_marker}${__sigil_prompt_base}"
  PS1="$PROMPT"
}

sigil_command() {
  local response command
  response="$("$__sigil_bin" op "," "$@")" || return $?
  print -r -- "$response"
  command="${response%%$'\n'*}"
  __sigil_prompt_insert "$command"
}

sigil_agent_step() {
  "$__sigil_bin" op ",," "$@"
  local exit_status=$?
  __sigil_insert_pending_handoff
  return "$exit_status"
}

sigil_agent_step_auto() {
  "$__sigil_bin" op ",,," "$@"
  local exit_status=$?
  __sigil_insert_pending_handoff
  return "$exit_status"
}

sigil_execute_command() {
  sigil_agent_step "$@"
}

sigil_command_loop() {
  sigil_agent_step_auto "$@"
}

sigil_question() {
  "$__sigil_bin" op "?" "$@"
}

sigil_web_question() {
  "$__sigil_bin" op "??" "$@"
}

sigil_follow_up() {
  sigil_web_question "$@"
}

sigil_goal() {
  "$__sigil_bin" op "@" "$@"
  local exit_status=$?
  __sigil_insert_pending_handoff
  return "$exit_status"
}

sigil_goal_auto() {
  "$__sigil_bin" op "@@" "$@"
  local exit_status=$?
  __sigil_insert_pending_handoff
  return "$exit_status"
}

if __sigil_glyphs_enabled; then
  function ',' { sigil_command "$*" }
  function ',,' { sigil_agent_step "$*" }
  function ',,,' { sigil_agent_step_auto "$*" }
  function '?' { sigil_question "$*" }
  function '??' { sigil_web_question "$*" }
  function '@' { sigil_goal "$*" }
  function '@@' { sigil_goal_auto "$*" }

  alias ','='noglob sigil_command'
  alias ',,'='noglob sigil_agent_step'
  alias ',,,'='noglob sigil_agent_step_auto'
  alias '?'='noglob sigil_question'
  alias '??'='noglob sigil_web_question'
  alias '@'='noglob sigil_goal'
  alias '@@'='noglob sigil_goal_auto'
fi

autoload -Uz add-zsh-hook
typeset -g __sigil_preexec_command=""

__sigil_preexec() {
  __sigil_preexec_command="$1"
  __sigil_capture_start "$1"
}

__sigil_precmd() {
  local exit_status=$?
  local command="$__sigil_preexec_command"
  local stdout_snippet stderr_snippet
  __sigil_capture_stop
  __sigil_preexec_command=""
  if [[ -z "$command" ]]; then
    __sigil_capture_cleanup
    __sigil_refresh_prompt_marker
    return "$exit_status"
  fi
  if ! __sigil_recordable_command "$command"; then
    __sigil_capture_cleanup
    __sigil_refresh_prompt_marker
    return "$exit_status"
  fi
  stdout_snippet="${SIGIL_FAILURE_STDOUT:-}"
  stderr_snippet="${SIGIL_FAILURE_STDERR:-}"
  [[ -n "$stdout_snippet" ]] || stdout_snippet="$(__sigil_capture_file_snippet "$__sigil_capture_stdout_file")"
  [[ -n "$stderr_snippet" ]] || stderr_snippet="$(__sigil_capture_file_snippet "$__sigil_capture_stderr_file")"
  local record_args=(record-turn --status "$exit_status" --cwd "$PWD")
  [[ -n "$stdout_snippet" ]] && record_args+=(--stdout-snippet "$stdout_snippet")
  [[ -n "$stderr_snippet" ]] && record_args+=(--stderr-snippet "$stderr_snippet")
  "$__sigil_bin" "${record_args[@]}" "$command" >/dev/null 2>&1 || true
  unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
  __sigil_capture_cleanup
  __sigil_refresh_prompt_marker
  return "$exit_status"
}

add-zsh-hook preexec __sigil_preexec
add-zsh-hook precmd __sigil_precmd

if __sigil_glyphs_enabled; then
  zshaddhistory() {
    emulate -L zsh
    local line="${1%%$'\n'}"
    case "$line" in
      ,*|\\\?*) return 1 ;;
    esac
    return 0
  }
fi
