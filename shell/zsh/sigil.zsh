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

sigil_command() {
  local selected
  selected="$("$__sigil_bin" command --select "$*")" || return $?
  [[ -n "$selected" ]] && print -z -- "$selected"
}

sigil_previous_command() {
  local selected
  selected="$("$__sigil_bin" command --previous --select)" || return $?
  [[ -n "$selected" ]] && print -z -- "$selected"
}

sigil_question() {
  "$__sigil_bin" question "$*"
}

sigil_follow_up() {
  "$__sigil_bin" question --follow-up "$*"
}

__sigil_select_fix() {
  "$__sigil_bin" fix
}

__sigil_select_previous_fix() {
  "$__sigil_bin" fix --previous
}

sigil_fix() {
  local selected
  selected="$(__sigil_select_fix)" || return $?
  [[ -n "$selected" ]] && print -z -- "$selected"
}

sigil_previous_fix() {
  local selected
  selected="$(__sigil_select_previous_fix)" || return $?
  [[ -n "$selected" ]] && print -z -- "$selected"
}

sigil_summary() {
  "$__sigil_bin" summary "$*"
}

function ',' { sigil_command "$*" }
function ',,' { sigil_previous_command "$*" }
function '?' { sigil_question "$*" }
function '??' { sigil_follow_up "$*" }
function '^' { sigil_fix "$*" }
function '^^' { sigil_previous_fix "$*" }
function '@.' { sigil_summary "$*" }

autoload -Uz add-zsh-hook
typeset -g __sigil_preexec_command=""

__sigil_preexec() {
  __sigil_preexec_command="$1"
}

__sigil_precmd() {
  local exit_status=$?
  if (( exit_status != 0 )) && [[ -n "$__sigil_preexec_command" ]]; then
    case "$__sigil_preexec_command" in
      ,*|\?*|\^*|@*|sigil\ *|__sigil_*) __sigil_preexec_command=""; return ;;
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

__sigil_accept_line() {
  emulate -L zsh
  local b="$BUFFER" rest
  if [[ "$b" == ,* ]]; then
    if [[ "$b" == ,!* ]]; then
      zle -I
      print -u2 -- "${__sigil_muted}❯ sigil ,! · blocked · bang requires sandbox${__sigil_reset}"
      zle reset-prompt
      return
    elif [[ "$b" == ,,* ]]; then
      BUFFER=",,"
    else
      rest="${b#,}"; rest="${rest## }"
      [[ -n "$rest" ]] && BUFFER=", ${(qqq)rest}"
    fi
  elif [[ "$b" == @.* ]]; then
    rest="${b#@.}"; rest="${rest## }"
    zle -I
    BUFFER=""
    sigil_summary "$rest"
    zle reset-prompt
    return
  elif [[ "$b" == @!* || "$b" == @* ]]; then
    zle -I
    print -u2 -- "${__sigil_muted}❯ sigil @ · blocked · no promotion mutation${__sigil_reset}"
    zle reset-prompt
    return
  elif [[ "$b" == \^\^* ]]; then
    local selected
    BUFFER="^^"
    zle -I
    BUFFER=""
    selected="$(__sigil_select_previous_fix)" || { zle reset-prompt; return }
    BUFFER="$selected"
    CURSOR=${#BUFFER}
    zle reset-prompt
    return
  elif [[ "$b" == \^* ]]; then
    local selected
    BUFFER="^"
    zle -I
    BUFFER=""
    selected="$(__sigil_select_fix)" || { zle reset-prompt; return }
    BUFFER="$selected"
    CURSOR=${#BUFFER}
    zle reset-prompt
    return
  elif [[ "$b" == \?!* ]]; then
    zle -I
    print -u2 -- "${__sigil_muted}❯ pi ?!    · blocked · no execute path${__sigil_reset}"
    zle reset-prompt
    return
  elif [[ "$b" == \?\?* ]]; then
    rest="${b#\?\?}"; rest="${rest## }"
    if [[ -n "$rest" ]]; then
      BUFFER="?? ${(qqq)rest}"
      zle -I
      BUFFER=""
      sigil_follow_up "$rest"
      zle reset-prompt
      return
    fi
  elif [[ "$b" == \?* ]]; then
    rest="${b#\?}"; rest="${rest## }"
    if [[ -n "$rest" ]]; then
      BUFFER="? ${(qqq)rest}"
      zle -I
      BUFFER=""
      sigil_question "$rest"
      zle reset-prompt
      return
    fi
  fi
  zle .accept-line
}
zle -N accept-line __sigil_accept_line

zshaddhistory() {
  emulate -L zsh
  local line="${1%%$'\n'}"
  case "$line" in
    ,*|\\\?*|\^*|@*) return 1 ;;
  esac
  return 0
}
