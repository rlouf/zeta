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

if [[ -z "${SIGIL_TTY_FD:-}" ]]; then
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

__sigil_glyphs_enabled() {
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

sigil_command() {
  local response command
  response="$("$__sigil_bin" op "," "$@")" || return $?
  print -r -- "$response"
  command="${response%%$'\n'*}"
  __sigil_history_insert "$command"
}

sigil_execute_command() {
  "$__sigil_bin" op ",," "$@"
}

sigil_question() {
  "$__sigil_bin" op "?" "$@"
}

sigil_follow_up() {
  "$__sigil_bin" ask --follow-up "$*"
}

sigil_fix() {
  "$__sigil_bin" op "^" "$@"
}

sigil_deep_fix() {
  "$__sigil_bin" op "^^" "$@"
}

if __sigil_glyphs_enabled; then
  function ',' { sigil_command "$*" }
  function ',,' { sigil_execute_command "$*" }
  function '?' { sigil_question "$*" }
  function '??' { sigil_follow_up "$*" }
  function '^' { sigil_fix "$*" }
  function '^^' { sigil_deep_fix "$*" }

  alias ','='noglob sigil_command'
  alias ',,'='noglob sigil_execute_command'
  alias '?'='noglob sigil_question'
  alias '??'='noglob sigil_follow_up'
  alias '^'='noglob sigil_fix'
  alias '^^'='noglob sigil_deep_fix'
fi

autoload -Uz add-zsh-hook
typeset -g __sigil_preexec_command=""

__sigil_preexec() {
  __sigil_preexec_command="$1"
}

__sigil_precmd() {
  local exit_status=$?
  if (( exit_status != 0 )) && [[ -n "$__sigil_preexec_command" ]]; then
    case "$__sigil_preexec_command" in
      ,*|\?*|\^*|sigil\ *|__sigil_*) __sigil_preexec_command=""; return ;;
    esac
    local record_args=(record-failure --status "$exit_status" --cwd "$PWD")
    [[ -n "${SIGIL_FAILURE_STDOUT:-}" ]] && record_args+=(--stdout-snippet "$SIGIL_FAILURE_STDOUT")
    [[ -n "${SIGIL_FAILURE_STDERR:-}" ]] && record_args+=(--stderr-snippet "$SIGIL_FAILURE_STDERR")
    "$__sigil_bin" "${record_args[@]}" "$__sigil_preexec_command" >/dev/null 2>&1
    unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
  fi
  __sigil_preexec_command=""
}

add-zsh-hook preexec __sigil_preexec
add-zsh-hook precmd __sigil_precmd

if __sigil_glyphs_enabled; then
  zshaddhistory() {
    emulate -L zsh
    local line="${1%%$'\n'}"
    case "$line" in
      ,*|\\\?*|\^*) return 1 ;;
    esac
    return 0
  }
fi
