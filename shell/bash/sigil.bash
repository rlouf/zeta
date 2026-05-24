# Sigil bash bindings. Core behavior lives in the `sigil` executable.
#
# Bash cannot exactly match zsh's `print -z` buffer stack. These bindings get
# closest in two ways:
#   - punctuation commands print the selected proposal and add it to history
#   - the Readline widget below replaces the current buffer with the selected
#     proposal for review, without executing it

if [[ -n "${SIGIL_BIN:-}" ]]; then
  __sigil_bin="$SIGIL_BIN"
elif command -v sigil >/dev/null 2>&1; then
  __sigil_bin="sigil"
else
  __sigil_bin="sigil"
fi

__sigil_muted=$'\e[38;2;110;106;134m'
__sigil_reset=$'\e[0m'
__sigil_last_failed_history=""

if [[ -z "${SIGIL_SESSION_ID:-}" ]]; then
  if command -v uuidgen >/dev/null 2>&1; then
    export SIGIL_SESSION_ID="$(uuidgen)"
  else
    __sigil_tty="${TTY:-tty}"
    export SIGIL_SESSION_ID="${__sigil_tty##*/}-$$"
  fi
fi

__sigil_history_insert() {
  [[ $- == *i* ]] || return 0
  [[ -n "${1:-}" ]] || return 0
  builtin history -s "$1" 2>/dev/null || true
}

sigil_command() {
  local selected
  selected="$("$__sigil_bin" command --select "$*")" || return $?
  if [[ -n "$selected" ]]; then
    printf '%s\n' "$selected"
    __sigil_history_insert "$selected"
  fi
}

sigil_previous_command() {
  local selected
  selected="$("$__sigil_bin" command --previous --select)" || return $?
  if [[ -n "$selected" ]]; then
    printf '%s\n' "$selected"
    __sigil_history_insert "$selected"
  fi
}

sigil_question() {
  "$__sigil_bin" question "$*"
}

sigil_follow_up() {
  "$__sigil_bin" question --follow-up "$*"
}

sigil_fix() {
  local selected
  selected="$("$__sigil_bin" fix)" || return $?
  if [[ -n "$selected" ]]; then
    printf '%s\n' "$selected"
    __sigil_history_insert "$selected"
  fi
}

sigil_previous_fix() {
  local selected
  selected="$("$__sigil_bin" fix --previous)" || return $?
  if [[ -n "$selected" ]]; then
    printf '%s\n' "$selected"
    __sigil_history_insert "$selected"
  fi
}

function , { sigil_command "$*"; }
function ,, { sigil_previous_command "$*"; }
function ? { sigil_question "$*"; }
function ?? { sigil_follow_up "$*"; }
function ^ { sigil_fix "$*"; }
function ^^ { sigil_previous_fix "$*"; }

if [[ $- == *i* ]]; then
  alias ,='sigil_command'
  alias ,,='sigil_previous_command'
  alias '?'='sigil_question'
  alias '??'='sigil_follow_up'
  alias '^'='sigil_fix'
  alias '^^'='sigil_previous_fix'
fi

__sigil_trim_leading_spaces() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  printf '%s' "$value"
}

__sigil_block_readline() {
  printf '\n%s%s%s\n' "$__sigil_muted" "$1" "$__sigil_reset" >&2
  READLINE_LINE=""
  READLINE_POINT=0
}

__sigil_set_readline_buffer() {
  READLINE_LINE="${1:-}"
  READLINE_POINT=${#READLINE_LINE}
}

__sigil_readline_dispatch() {
  local b="${READLINE_LINE:-}"
  local rest selected

  if [[ "$b" == ,!* ]]; then
    __sigil_block_readline "> sigil ,! - blocked - bang requires sandbox"
    return 0
  elif [[ "$b" == ,,* ]]; then
    printf '\n' >&2
    selected="$("$__sigil_bin" command --previous --select)" || return $?
    __sigil_set_readline_buffer "$selected"
    return 0
  elif [[ "$b" == ,* ]]; then
    rest="${b#,}"
    rest="$(__sigil_trim_leading_spaces "$rest")"
    [[ -n "$rest" ]] || return 0
    printf '\n' >&2
    selected="$("$__sigil_bin" command --select "$rest")" || return $?
    __sigil_set_readline_buffer "$selected"
    return 0
  elif [[ "$b" == @!* || "$b" == @* ]]; then
    __sigil_block_readline "> sigil @ - blocked - no promotion mutation"
    return 0
  elif [[ "$b" == ^^* ]]; then
    printf '\n' >&2
    selected="$("$__sigil_bin" fix --previous)" || return $?
    __sigil_set_readline_buffer "$selected"
    return 0
  elif [[ "$b" == ^* ]]; then
    printf '\n' >&2
    selected="$("$__sigil_bin" fix)" || return $?
    __sigil_set_readline_buffer "$selected"
    return 0
  elif [[ "$b" == \?!* ]]; then
    __sigil_block_readline "> sigil ?! - blocked - no execute path"
    return 0
  elif [[ "$b" == \?\?* ]]; then
    rest="${b#??}"
    rest="$(__sigil_trim_leading_spaces "$rest")"
    [[ -n "$rest" ]] || return 0
    printf '\n' >&2
    READLINE_LINE=""
    READLINE_POINT=0
    "$__sigil_bin" question --follow-up "$rest"
    return $?
  elif [[ "$b" == \?* ]]; then
    rest="${b#?}"
    rest="$(__sigil_trim_leading_spaces "$rest")"
    [[ -n "$rest" ]] || return 0
    printf '\n' >&2
    READLINE_LINE=""
    READLINE_POINT=0
    "$__sigil_bin" question "$rest"
    return $?
  fi

  printf '\n%s%s%s\n' \
    "$__sigil_muted" \
    "> sigil readline - current buffer is not a Sigil glyph" \
    "$__sigil_reset" >&2
  return 0
}

__sigil_history_line() {
  local line
  line="$(HISTTIMEFORMAT= builtin history 1 2>/dev/null)" || return 1
  if [[ "$line" =~ ^[[:space:]]*[0-9]+[[:space:]]+(.*)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

__sigil_precmd() {
  local exit_status=$?
  local command

  command="$(__sigil_history_line)" || return "$exit_status"
  if [[ $exit_status -ne 0 && -n "$command" && "$command" != "$__sigil_last_failed_history" ]]; then
    case "$command" in
      ,*|\?*|\^*|@*|sigil\ *|__sigil_*) ;;
      *)
        "$__sigil_bin" record-failure --status "$exit_status" --cwd "$PWD" "$command" >/dev/null 2>&1 || true
        __sigil_last_failed_history="$command"
        ;;
    esac
  fi
  return "$exit_status"
}

__sigil_install_prompt_command() {
  [[ $- == *i* ]] || return 0

  local prompt_decl
  prompt_decl="$(declare -p PROMPT_COMMAND 2>/dev/null || true)"
  case "$prompt_decl" in
    declare\ -a*|declare\ -ax*)
      local item
      for item in "${PROMPT_COMMAND[@]}"; do
        [[ "$item" == "__sigil_precmd" ]] && return 0
      done
      PROMPT_COMMAND=(__sigil_precmd "${PROMPT_COMMAND[@]}")
      return 0
      ;;
  esac

  case ";${PROMPT_COMMAND:-};" in
    *";__sigil_precmd;"*) return 0 ;;
  esac
  if [[ -n "${PROMPT_COMMAND:-}" ]]; then
    PROMPT_COMMAND="__sigil_precmd; ${PROMPT_COMMAND}"
  else
    PROMPT_COMMAND="__sigil_precmd"
  fi
}

__sigil_install_readline_binding() {
  [[ $- == *i* ]] || return 0
  bind -x '"\C-x,": __sigil_readline_dispatch' 2>/dev/null || true
}

__sigil_install_prompt_command
__sigil_install_readline_binding
