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
__sigil_last_recorded_history_id=""
__sigil_prompt_marker=""
__sigil_prompt_marker_active=0
__sigil_prompt_base=""
__sigil_capture_active=0
__sigil_capture_stdout_file=""
__sigil_capture_stderr_file=""
__sigil_capture_stdout_pipe=""
__sigil_capture_stderr_pipe=""
__sigil_capture_stdout_pid=""
__sigil_capture_stderr_pid=""
__sigil_in_precmd=0

if [[ -z "${SIGIL_SESSION_ID:-}" ]]; then
  if command -v uuidgen >/dev/null 2>&1; then
    export SIGIL_SESSION_ID="$(uuidgen)"
  else
    __sigil_tty="${TTY:-tty}"
    export SIGIL_SESSION_ID="${__sigil_tty##*/}-$$"
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
  if exec 9<>/dev/tty 2>/dev/null; then
    export SIGIL_TTY_FD=9
  fi
fi

__sigil_history_insert() {
  [[ -n "${1:-}" ]] || return 0
  builtin history -s "$1" 2>/dev/null || true
}

__sigil_insert_pending_handoff() {
  local command
  command="$("$__sigil_bin" handoff pop 2>/dev/null)" || return 0
  __sigil_history_insert "$command"
}

__sigil_stdin_is_pipe() {
  [[ -p /dev/stdin ]]
}

__sigil_glyphs_enabled() {
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

__sigil_prompt_marker_enabled() {
  [[ $- == *i* ]] || return 1
  [[ "${SIGIL_ENABLE_PROMPT_MARKER:-1}" != "0" && "${SIGIL_ENABLE_PROMPT_MARKER:-1}" != "false" ]]
}

__sigil_recordable_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  case "$command" in
    [[:space:]]*|,*|\?*|sigil\ *|__sigil_*)
      return 1
      ;;
  esac
  return 0
}

__sigil_turn_capture_enabled() {
  [[ "${SIGIL_ENABLE_TURN_CAPTURE:-1}" != "0" && "${SIGIL_ENABLE_TURN_CAPTURE:-1}" != "false" ]] || return 1
  if [[ "${SIGIL_ENABLE_TURN_CAPTURE:-}" != "1" && "${SIGIL_ENABLE_TURN_CAPTURE:-}" != "true" ]]; then
    [[ $- == *i* ]] || return 1
  fi
  command -v mktemp >/dev/null 2>&1 || return 1
  command -v mkfifo >/dev/null 2>&1 || return 1
  command -v tee >/dev/null 2>&1 || return 1
  command -v tail >/dev/null 2>&1 || return 1
  return 0
}

__sigil_capture_start() {
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
      PS1="$__sigil_prompt_base"
      __sigil_prompt_marker=""
      __sigil_prompt_marker_active=0
    fi
    return 0
  fi

  local current_prompt="${PS1:-}"
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

  PS1="${__sigil_prompt_marker}${__sigil_prompt_base}"
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

sigil_command_loop() {
  "$__sigil_bin" op ",,," "$@"
  local status=$?
  __sigil_insert_pending_handoff
  return "$status"
}

sigil_question() {
  "$__sigil_bin" op "?" "$@"
}

sigil_follow_up() {
  "$__sigil_bin" op "??" "$@"
}

sigil_question_loop() {
  "$__sigil_bin" op "???" "$@"
}

# ── Optional glyph functions ─────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  function , { sigil_command "$*"; }
  function ,, { sigil_execute_command "$*"; }
  function ,,, { sigil_command_loop "$*"; }
  function ? { sigil_question "$*"; }
  function ?? { sigil_follow_up "$*"; }
  function ??? { sigil_question_loop "$*"; }

  if [[ $- == *i* ]]; then
    alias ,='sigil_command'
    alias ,,='sigil_execute_command'
    alias ,,,='sigil_command_loop'
    alias '?'='sigil_question'
    alias '??'='sigil_follow_up'
    alias '???'='sigil_question_loop'
  fi
fi

# ── Failure recording ────────────────────────────────────────────────────

__sigil_history_entry() {
  local line
  line="$(HISTTIMEFORMAT= builtin history 1 2>/dev/null)" || return 1
  if [[ "$line" =~ ^[[:space:]]*([0-9]+)[[:space:]]+(.*)$ ]]; then
    printf '%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return 0
  fi
  return 1
}

__sigil_history_line() {
  local entry
  entry="$(__sigil_history_entry)" || return 1
  printf '%s\n' "${entry#*$'\t'}"
}

__sigil_precmd() {
  local exit_status=$?
  local entry history_id
  local command
  local record_args
  local stdout_snippet stderr_snippet

  __sigil_in_precmd=1
  __sigil_capture_stop

  if ! entry="$(__sigil_history_entry)"; then
    __sigil_capture_cleanup
    __sigil_refresh_prompt_marker
    __sigil_in_precmd=0
    return "$exit_status"
  fi
  history_id="${entry%%$'\t'*}"
  command="${entry#*$'\t'}"
  if [[ -z "$command" ]]; then
    __sigil_capture_cleanup
    __sigil_refresh_prompt_marker
    __sigil_in_precmd=0
    return "$exit_status"
  fi
  if [[ -n "$history_id" && "$history_id" == "$__sigil_last_recorded_history_id" ]]; then
    __sigil_capture_cleanup
    __sigil_refresh_prompt_marker
    __sigil_in_precmd=0
    return "$exit_status"
  fi
  if ! __sigil_recordable_command "$command"; then
    __sigil_capture_cleanup
    __sigil_refresh_prompt_marker
    __sigil_in_precmd=0
    return "$exit_status"
  fi
  stdout_snippet="${SIGIL_FAILURE_STDOUT:-}"
  stderr_snippet="${SIGIL_FAILURE_STDERR:-}"
  [[ -n "$stdout_snippet" ]] || stdout_snippet="$(__sigil_capture_file_snippet "$__sigil_capture_stdout_file")"
  [[ -n "$stderr_snippet" ]] || stderr_snippet="$(__sigil_capture_file_snippet "$__sigil_capture_stderr_file")"
  record_args=(record-turn --status "$exit_status" --cwd "$PWD")
  [[ -n "$stdout_snippet" ]] && record_args+=(--stdout-snippet "$stdout_snippet")
  [[ -n "$stderr_snippet" ]] && record_args+=(--stderr-snippet "$stderr_snippet")
  "$__sigil_bin" "${record_args[@]}" "$command" >/dev/null 2>&1 || true
  __sigil_last_recorded_history_id="$history_id"
  unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
  __sigil_capture_cleanup
  __sigil_refresh_prompt_marker
  __sigil_in_precmd=0
  return "$exit_status"
}

__sigil_debug_trap() {
  [[ $- == *i* ]] || return 0
  [[ $__sigil_in_precmd -eq 0 ]] || return 0
  [[ $__sigil_capture_active -eq 0 ]] || return 0
  __sigil_capture_start "$BASH_COMMAND"
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

__sigil_install_debug_trap() {
  [[ $- == *i* ]] || return 0
  __sigil_turn_capture_enabled || return 0
  [[ -z "$(trap -p DEBUG)" ]] || return 0
  trap '__sigil_debug_trap' DEBUG
}

__sigil_install_prompt_command
__sigil_install_debug_trap
