# Sigil bash bindings. Core behavior lives in the `sigil` executable.

export SIGIL_BINDING_LOADED="bash"

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
if [[ -z "${ZETA_TTY_FD:-}" && -n "${SIGIL_TTY_FD:-}" ]]; then
  export ZETA_TTY_FD="$SIGIL_TTY_FD"
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

# ── Zeta continuation capture ────────────────────────────────────────────

__sigil_zeta_capture_active="${__sigil_zeta_capture_active:-0}"
__sigil_zeta_last_history_id="${__sigil_zeta_last_history_id:-}"

__sigil_zeta_enable_capture() {
  __sigil_zeta_capture_active=1
}

__sigil_zeta_consume_capture() {
  __sigil_zeta_capture_active=0
}

__sigil_zeta_recordable_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  [[ "$command" =~ [^[:space:]] ]] || return 1
  case "$command" in
    [[:space:]]*|,*|+*|sigil\ *|zeta\ *|__sigil_*|sigil_*|noglob\ sigil_*|noglob\ ,*|noglob\ +*)
      return 1
      ;;
  esac
  return 0
}

__sigil_zeta_record_shell_turn() {
  local command="$1"
  local status="$2"
  local payload stdout_snippet stderr_snippet
  stdout_snippet="${SIGIL_FAILURE_STDOUT:-}"
  stderr_snippet="${SIGIL_FAILURE_STDERR:-}"
  payload="$(printf '{"command":%s,"status":%s,"cwd":%s,"stdout_snippet":%s,"stderr_snippet":%s}' \
    "$(__sigil_json_string "$command")" \
    "$status" \
    "$(__sigil_json_string "$PWD")" \
    "$(__sigil_json_string "$stdout_snippet")" \
    "$(__sigil_json_string "$stderr_snippet")")"
  printf '%s\n' "$payload" | "$__sigil_bin" transcript shell-turn >/dev/null 2>&1 || true
  unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
}

__sigil_zeta_prompt_capture() {
  local status=$?
  local entry history_id command
  [[ "$__sigil_zeta_capture_active" == "1" ]] || return "$status"
  entry="$(__sigil_history_entry)" || return "$status"
  history_id="${entry%%$'\t'*}"
  command="${entry#*$'\t'}"
  [[ -n "$history_id" && "$history_id" != "$__sigil_zeta_last_history_id" ]] || return "$status"
  __sigil_zeta_last_history_id="$history_id"
  if __sigil_zeta_recordable_command "$command"; then
    __sigil_zeta_record_shell_turn "$command" "$status"
  fi
  return "$status"
}

__sigil_install_zeta_prompt_capture() {
  case "${PROMPT_COMMAND:-}" in
    *__sigil_zeta_prompt_capture*) return 0 ;;
    "")
      PROMPT_COMMAND="__sigil_zeta_prompt_capture"
      ;;
    *)
      PROMPT_COMMAND="__sigil_zeta_prompt_capture; $PROMPT_COMMAND"
      ;;
  esac
}

# ── Command wrappers ─────────────────────────────────────────────────────

sigil_command() {
  if [[ "$#" == "0" ]]; then
    "$__sigil_bin" ask
  else
    "$__sigil_bin" ask "$*"
  fi
}

__sigil_zeta_append() {
  printf '%s\n' "$1" | "$__zeta_bin" transcript append 2>/dev/null || true
}

__sigil_zeta_tool_detail() {
  local name="$1"
  case "$name" in
    read|edit|write)
      __sigil_json_get path
      ;;
    bash)
      __sigil_json_get command
      ;;
    grep)
      __sigil_json_get pattern
      ;;
    ls)
      __sigil_json_get path
      ;;
  esac
}

__sigil_muted_print() {
  if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    printf '\033[38;2;110;106;134m%s\033[0m\n' "$1"
  else
    printf '%s\n' "$1"
  fi
}

__sigil_zeta_spinner_start() {
  __sigil_zeta_spinner_pid=""
  [[ -t 2 ]] || return 0
  [[ "${ZETA_SPINNER:-1}" != "0" && "${ZETA_SPINNER:-1}" != "false" ]] || return 0
  (
    while :; do
      for frame in thinking thinking. thinking.. thinking...; do
        if [[ -z "${NO_COLOR:-}" ]]; then
          printf '\r\033[K\033[38;2;110;106;134m❯ %s\033[0m' "$frame" >&2
        else
          printf '\r\033[K❯ %s' "$frame" >&2
        fi
        sleep 0.35
      done
    done
  ) &
  __sigil_zeta_spinner_pid="$!"
}

__sigil_zeta_spinner_stop() {
  [[ -n "${__sigil_zeta_spinner_pid:-}" ]] || return 0
  kill "$__sigil_zeta_spinner_pid" >/dev/null 2>&1 || true
  wait "$__sigil_zeta_spinner_pid" 2>/dev/null || true
  printf '\r\033[K' >&2
  __sigil_zeta_spinner_pid=""
}

__sigil_zeta_model_stream() {
  local request="$1"
  local rc
  __sigil_zeta_spinner_start
  printf '%s\n' "$request" | "$__zeta_bin" model stream
  rc=$?
  __sigil_zeta_spinner_stop
  return "$rc"
}

__sigil_zeta_tool_start() {
  local name="$1"
  local input="$2"
  local detail
  detail="$(printf '%s\n' "$input" | __sigil_zeta_tool_detail "$name")"
  if [[ -n "$detail" ]]; then
    __sigil_muted_print "$(printf '❯ %-5s  %s' "$name" "$detail")"
  else
    __sigil_muted_print "❯ $name"
  fi
}

__sigil_zeta_result_summary() {
  local name="$1"
  "$__sigil_bin" display tool-result "$name"
}

__sigil_zeta_shell_result_summary() {
  "$__sigil_bin" display shell-result
}

__sigil_zeta_render_result_summary() {
  local name="$1"
  local result="$2"
  local summary line
  summary="$(printf '%s\n' "$result" | __sigil_zeta_result_summary "$name" 2>/dev/null || true)"
  [[ -n "$summary" ]] || return 0
  while IFS= read -r line; do
    [[ -n "$line" ]] && __sigil_muted_print "  $line"
  done <<< "$summary"
}

__sigil_zeta_render_shell_result_summary() {
  local event="$1"
  local summary line
  summary="$(printf '%s\n' "$event" | __sigil_zeta_shell_result_summary 2>/dev/null || true)"
  [[ -n "$summary" ]] || return 0
  while IFS= read -r line; do
    [[ -n "$line" ]] && __sigil_muted_print "$line"
  done <<< "$summary"
}

__sigil_zeta_turn() {
  local objective request events event event_type text name input analysis result command artifact
  local shell_result_event
  local tool_call_record tool_call_id
  local step continue_step
  objective="$*"
  continue_step=0
  if [[ -z "$objective" ]]; then
    continue_step=1
    objective="Continue the active Zeta step. Read the latest zeta.shell_handoff_result.v1 transcript event as the source of truth for what the user ran after the last shell handoff. If the outcome is cancelled, do not assume the proposed command ran; continue from the recorded shell_turns and explain the cancellation plainly if it matters. If no relevant shell turn appears, ask for the command result instead of inventing it."
  fi
  if [[ "$continue_step" == "1" ]]; then
    __sigil_zeta_consume_capture
    shell_result_event="$("$__sigil_bin" transcript shell-result 2>/dev/null || true)"
    [[ -n "$shell_result_event" ]] && __sigil_zeta_render_shell_result_summary "$shell_result_event"
  fi
  __sigil_zeta_append "$(printf '{"type":"user_message","content":%s}' "$(__sigil_json_string "$objective")")" >/dev/null
  for step in 1 2 3 4 5 6 7 8; do
    request="$(printf '{"objective":%s}' "$(__sigil_json_string "$objective")")"
    events="$(__sigil_zeta_model_stream "$request")" || return $?
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
          __sigil_zeta_tool_start "$name" "$input"
          analysis="$(printf '%s\n' "$input" | "$__zeta_bin" tool "$name" --analyze)" || return $?
          __sigil_zeta_append "$(printf '{"type":"tool_analysis","tool_call_id":%s,"name":%s,"analysis":%s}' "$(__sigil_json_string "$tool_call_id")" "$(__sigil_json_string "$name")" "$analysis")" >/dev/null
          result="$(printf '%s\n' "$input" | "$__zeta_bin" tool "$name")" || return $?
          __sigil_zeta_append "$(printf '{"type":"tool_result","tool_call_id":%s,"name":%s,"result":%s}' "$(__sigil_json_string "$tool_call_id")" "$(__sigil_json_string "$name")" "$result")" >/dev/null
          __sigil_zeta_render_result_summary "$name" "$result"
          command="$(printf '%s\n' "$result" | __sigil_json_get handoff.command)"
          if [[ -n "$command" ]]; then
            artifact="$(printf '%s\n' "$result" | __sigil_json_get handoff.artifact)"
            [[ -n "$artifact" ]] && printf '%s\n' "artifact: $artifact"
            __sigil_zeta_enable_capture
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

sigil_agent_step() {
  __sigil_zeta_turn "$@"
}

sigil_agent_step_auto() {
  __sigil_zeta_turn "$@"
}

sigil_run() {
  "$__sigil_bin" run "$@"
}

if [[ $- == *i* ]]; then
  __sigil_install_zeta_prompt_capture
fi

# ── Optional glyph functions ─────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  function , { sigil_command "$*"; }
  function ,, { sigil_agent_step "$*"; }
  function ,,, { sigil_agent_step_auto "$*"; }
  function + { sigil_run "$@"; }

  if [[ $- == *i* ]]; then
    alias ,='sigil_command'
    alias ,,='sigil_agent_step'
    alias ,,,='sigil_agent_step_auto'
    alias +='sigil_run'
  fi
fi

# ── History helpers ──────────────────────────────────────────────────────

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
