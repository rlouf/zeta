# Sigil bash bindings. Core behavior lives in the `sigil` executable.

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
  [[ -n "${1:-}" ]] || return 0
  builtin history -s "$1" 2>/dev/null || true
}

__sigil_stdin_is_pipe() {
  [[ -p /dev/stdin ]]
}

__sigil_glyphs_enabled() {
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

# ── Command wrappers ─────────────────────────────────────────────────────

sigil_command() {
  local response command
  response="$("$__sigil_bin" op "," "$@")" || return $?
  printf '%s\n' "$response"
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

# ── Optional glyph functions ─────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  function , { sigil_command "$*"; }
  function ,, { sigil_execute_command "$*"; }
  function ? { sigil_question "$*"; }
  function ?? { sigil_follow_up "$*"; }
  function ^ { sigil_fix "$*"; }
  function ^^ { sigil_deep_fix "$*"; }

  if [[ $- == *i* ]]; then
    alias ,='sigil_command'
    alias ,,='sigil_execute_command'
    alias '?'='sigil_question'
    alias '??'='sigil_follow_up'
    alias '^'='sigil_fix'
    alias '^^'='sigil_deep_fix'
  fi
fi

# ── Failure recording ────────────────────────────────────────────────────

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
  local record_args

  command="$(__sigil_history_line)" || return "$exit_status"
  if [[ $exit_status -ne 0 && -n "$command" && "$command" != "$__sigil_last_failed_history" ]]; then
    case "$command" in
      ,*|\?*|\^*|sigil\ *|__sigil_*) ;;
      *)
        record_args=(record-failure --status "$exit_status" --cwd "$PWD")
        [[ -n "${SIGIL_FAILURE_STDOUT:-}" ]] && record_args+=(--stdout-snippet "$SIGIL_FAILURE_STDOUT")
        [[ -n "${SIGIL_FAILURE_STDERR:-}" ]] && record_args+=(--stderr-snippet "$SIGIL_FAILURE_STDERR")
        "$__sigil_bin" "${record_args[@]}" "$command" >/dev/null 2>&1 || true
        __sigil_last_failed_history="$command"
        unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
        ;;
    esac
  fi
  return "$exit_status"
}

# ── Installation ─────────────────────────────────────────────────────────

__sigil_install_prompt_command() {
  [[ $- == *i* ]] || return 0

  local prompt_decl
  prompt_decl="$(declare -p PROMPT_COMMAND 2>/dev/null || true)"
  case "$prompt_decl" in
    declare\ -a*|declare\ -ax*)
      local item has_precmd=0
      local new_prompt_command=()
      for item in "${PROMPT_COMMAND[@]}"; do
        [[ "$item" == "__sigil_prompt_setup" ]] && continue
        [[ "$item" == "__sigil_precmd" ]] && has_precmd=1
        new_prompt_command+=("$item")
      done
      if [[ $has_precmd -eq 1 ]]; then
        PROMPT_COMMAND=("${new_prompt_command[@]}")
      else
        PROMPT_COMMAND=(__sigil_precmd "${new_prompt_command[@]}")
      fi
      return 0
      ;;
  esac

  local prompt_command="${PROMPT_COMMAND:-}"
  prompt_command="${prompt_command//__sigil_prompt_setup; /}"
  prompt_command="${prompt_command//; __sigil_prompt_setup/}"
  prompt_command="${prompt_command//__sigil_prompt_setup/}"

  case ";${prompt_command};" in
    *";__sigil_precmd;"*) return 0 ;;
  esac
  if [[ -n "$prompt_command" ]]; then
    PROMPT_COMMAND="__sigil_precmd; ${prompt_command}"
  else
    PROMPT_COMMAND="__sigil_precmd"
  fi
}

__sigil_install_prompt_command
