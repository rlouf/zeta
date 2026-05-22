# Sigil zsh bindings. Core behavior lives in ../bin/sigil.

typeset -g __sigil_root="${${(%):-%x}:A:h:h}"
typeset -g __sigil_bin="$__sigil_root/bin/sigil"
typeset -g __sigil_muted=$'\e[38;2;110;106;134m'
typeset -g __sigil_reset=$'\e[0m'

sigil_command() {
  local selected
  selected="$("$__sigil_bin" command --select "$*")" || return $?
  [[ -n "$selected" ]] && print -z -- "$selected"
}

sigil_previous_command() {
  local selected
  selected="$("$__sigil_bin" previous-command --select)" || return $?
  [[ -n "$selected" ]] && print -z -- "$selected"
}

sigil_question() {
  "$__sigil_bin" question "$*"
}

function ',' { sigil_command "$*" }
function ',,' { sigil_previous_command "$*" }
function '?' { sigil_question "$*" }

__sigil_accept_line() {
  emulate -L zsh
  local b="$BUFFER" rest
  if [[ "$b" == ,* ]]; then
    if [[ "$b" == ,,* ]]; then
      BUFFER=",,"
    else
      rest="${b#,}"; rest="${rest## }"
      [[ -n "$rest" ]] && BUFFER=", ${(qqq)rest}"
    fi
  elif [[ "$b" == \?* ]]; then
    rest="${b#\?}"; rest="${rest## }"
    if [[ -n "$rest" ]]; then
      BUFFER=""
      zle -I
      print -r -- "${__sigil_muted}? ${rest}${__sigil_reset}"
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
    ,*|\\\?*) return 1 ;;
  esac
  return 0
}
