# Sigil bash bindings. Core behavior lives in the `sigil` executable.

if [[ -n "${SIGIL_BIN:-}" ]]; then
  __sigil_bin="$SIGIL_BIN"
elif command -v sigil >/dev/null 2>&1; then
  __sigil_bin="$(command -v sigil)"
else
  __sigil_bin="sigil"
fi

if [[ -n "${ZETA_BIN:-}" ]]; then
  __zeta_bin="$ZETA_BIN"
elif command -v zeta >/dev/null 2>&1; then
  __zeta_bin="$(command -v zeta)"
elif [[ "$__sigil_bin" == */* && -x "$(dirname "$__sigil_bin")/zeta" ]]; then
  __zeta_bin="$(dirname "$__sigil_bin")/zeta"
else
  __zeta_bin="zeta"
fi

__sigil_clear_legacy_capture_state() {
  local legacy_capture_active="${__sigil_capture_active:-0}"
  local debug_trap
  debug_trap="$(trap -p DEBUG 2>/dev/null || true)"
  case "$debug_trap" in
    *__sigil_debug_trap*)
      trap - DEBUG
      ;;
  esac

  unset -f __sigil_debug_trap __sigil_install_debug_trap \
    __sigil_turn_capture_enabled __sigil_capture_skipped_command \
    __sigil_capture_start __sigil_capture_stop __sigil_capture_stop_reader \
    __sigil_capture_stop_readers __sigil_capture_file_snippet \
    __sigil_capture_cleanup 2>/dev/null || true
  unset __sigil_capture_active __sigil_capture_stdout_file \
    __sigil_capture_stderr_file __sigil_capture_stdout_pipe \
    __sigil_capture_stderr_pipe __sigil_capture_stdout_pid \
    __sigil_capture_stderr_pid 2>/dev/null || true

  if [[ $- == *i* && "$legacy_capture_active" == "1" ]]; then
    exec 1>/dev/tty 2>/dev/tty || true
  fi
}

__sigil_clear_legacy_capture_state
unset -f __sigil_clear_legacy_capture_state 2>/dev/null || true

__sigil_last_recorded_history_id=""
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
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

__sigil_recordable_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  case "$command" in
    [[:space:]]*|,*|\?*|+*|@*|sigil\ *|sigil_*|noglob\ sigil_*|command\ sigil_*|__sigil_*)
      return 1
      ;;
  esac
  return 0
}

__sigil_record_turn() {
  local exit_status="$1"
  local command="$2"
  local stdout_snippet stderr_snippet
  local -a record_args
  stdout_snippet="${SIGIL_FAILURE_STDOUT:-}"
  stderr_snippet="${SIGIL_FAILURE_STDERR:-}"
  record_args=(record-turn --status "$exit_status" --cwd "$PWD")
  [[ -n "$stdout_snippet" ]] && record_args+=(--stdout-snippet "$stdout_snippet")
  [[ -n "$stderr_snippet" ]] && record_args+=(--stderr-snippet "$stderr_snippet")
  "$__sigil_bin" "${record_args[@]}" "$command" >/dev/null 2>&1 || true
}

__sigil_precmd_done() {
  local exit_status="$1"
  __sigil_in_precmd=0
  return "$exit_status"
}

# ── Command wrappers ─────────────────────────────────────────────────────

sigil_command() {
  local response command
  response="$("$__sigil_bin" op "," "$@")" || return $?
  printf '%s\n' "$response"
  command="${response%%$'\n'*}"
  __sigil_history_insert "$command"
}

__sigil_zeta_append() {
  printf '%s\n' "$1" | "$__zeta_bin" transcript append 2>/dev/null || true
}

__sigil_zeta_turn() {
  local objective request events event event_type text name input analysis result command reason artifact
  local tool_call_record tool_call_id
  local step
  objective="$*"
  __sigil_zeta_append "$(printf '{"type":"user_message","content":%s}' "$(__sigil_json_string "$objective")")" >/dev/null
  for step in 1 2 3 4 5 6 7 8; do
    request="$(printf '{"objective":%s}' "$(__sigil_json_string "$objective")")"
    events="$(printf '%s\n' "$request" | "$__zeta_bin" model stream)" || return $?
    while IFS= read -r event; do
      [[ -n "$event" ]] || continue
      event_type="$(printf '%s\n' "$event" | __sigil_json_get type)"
      case "$event_type" in
        assistant_delta)
          text="$(printf '%s\n' "$event" | __sigil_json_get text)"
          [[ -n "$text" ]] && printf '%s\n' "$text"
          ;;
        final)
          return 0
          ;;
        tool_call)
          name="$(printf '%s\n' "$event" | __sigil_json_get name)"
          input="$(printf '%s\n' "$event" | __sigil_json_get input)"
          tool_call_record="$(__sigil_zeta_append "$(printf '{"type":"tool_call","name":%s,"input":%s}' "$(__sigil_json_string "$name")" "$input")")"
          tool_call_id="$(printf '%s\n' "$tool_call_record" | __sigil_json_get id)"
          analysis="$(printf '%s\n' "$input" | "$__zeta_bin" tool "$name" --analyze)" || return $?
          __sigil_zeta_append "$(printf '{"type":"tool_analysis","tool_call_id":%s,"name":%s,"analysis":%s}' "$(__sigil_json_string "$tool_call_id")" "$(__sigil_json_string "$name")" "$analysis")" >/dev/null
          result="$(printf '%s\n' "$input" | "$__zeta_bin" tool "$name")" || return $?
          __sigil_zeta_append "$(printf '{"type":"tool_result","tool_call_id":%s,"name":%s,"result":%s}' "$(__sigil_json_string "$tool_call_id")" "$(__sigil_json_string "$name")" "$result")" >/dev/null
          command="$(printf '%s\n' "$result" | __sigil_json_get handoff.command)"
          if [[ -n "$command" ]]; then
            reason="$(printf '%s\n' "$result" | __sigil_json_get handoff.reason)"
            artifact="$(printf '%s\n' "$result" | __sigil_json_get handoff.artifact)"
            [[ -n "$reason" ]] && printf '%s\n' "$reason"
            [[ -n "$artifact" ]] && printf '%s\n' "artifact: $artifact"
            __sigil_history_insert "$command"
            return 0
          fi
          break
          ;;
        error)
          printf '%s\n' "$(printf '%s\n' "$event" | __sigil_json_get message)"
          return 1
          ;;
      esac
    done <<< "$events"
  done
  printf '%s\n' "Zeta stopped after reaching the step budget."
  return 1
}

__sigil_op() {
  local op="$1"
  shift
  "$__sigil_bin" op "$op" "$@"
}

sigil_agent_step() {
  __sigil_zeta_turn "$@"
}

sigil_agent_step_auto() {
  __sigil_zeta_turn "$@"
}

sigil_question() {
  "$__sigil_bin" op "?" "$@"
}

sigil_web_question() {
  "$__sigil_bin" op "??" "$@"
}

sigil_run() {
  "$__sigil_bin" run "$@"
}

sigil_goal() {
  __sigil_op "@" "$@"
}

sigil_goal_auto() {
  __sigil_op "@@" "$@"
}

# ── Optional glyph functions ─────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  function , { sigil_command "$*"; }
  function ,, { sigil_agent_step "$*"; }
  function ,,, { sigil_agent_step_auto "$*"; }
  function ? { sigil_question "$*"; }
  function ?? { sigil_web_question "$*"; }
  function + { sigil_run "$@"; }
  function @ { sigil_goal "$*"; }
  function @@ { sigil_goal_auto "$*"; }

  if [[ $- == *i* ]]; then
    alias ,='sigil_command'
    alias ,,='sigil_agent_step'
    alias ,,,='sigil_agent_step_auto'
    alias '?'='sigil_question'
    alias '??'='sigil_web_question'
    alias +='sigil_run'
    alias @='sigil_goal'
    alias @@='sigil_goal_auto'
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

  __sigil_in_precmd=1

  if ! entry="$(__sigil_history_entry)"; then
    __sigil_precmd_done "$exit_status"
    return $?
  fi
  history_id="${entry%%$'\t'*}"
  command="${entry#*$'\t'}"
  if [[ -z "$command" ]]; then
    __sigil_precmd_done "$exit_status"
    return $?
  fi
  if [[ -n "$history_id" && "$history_id" == "$__sigil_last_recorded_history_id" ]]; then
    __sigil_precmd_done "$exit_status"
    return $?
  fi
  if ! __sigil_recordable_command "$command"; then
    __sigil_precmd_done "$exit_status"
    return $?
  fi
  __sigil_record_turn "$exit_status" "$command"
  __sigil_last_recorded_history_id="$history_id"
  unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
  __sigil_precmd_done "$exit_status"
  return $?
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
