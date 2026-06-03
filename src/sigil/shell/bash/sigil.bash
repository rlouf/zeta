# Sigil bash bindings. Core behavior lives in the `sigil` executable.

export SIGIL_BINDING_LOADED="bash"

if [[ -n "${SIGIL_BIN:-}" ]]; then
  __sigil_bin="$SIGIL_BIN"
elif command -v sigil >/dev/null 2>&1; then
  __sigil_bin="$(command -v sigil)"
else
  __sigil_bin="sigil"
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

__sigil_zeta_turn() {
  local glyph="$1"
  shift || true
  local objective handoff_file step_status command
  local args=()
  objective="$*"
  handoff_file="$(mktemp "${TMPDIR:-/tmp}/sigil-handoff.XXXXXX")" || return 1
  if [[ -z "$objective" ]]; then
    __sigil_zeta_consume_capture
    args+=(--continue)
  fi
  "$__sigil_bin" zeta-step --glyph "$glyph" --handoff-file "$handoff_file" "${args[@]}" "$objective"
  step_status=$?
  if [[ "$step_status" == "0" && -s "$handoff_file" ]]; then
    command="$(__sigil_json_get command < "$handoff_file" 2>/dev/null || true)"
    if [[ -n "$command" ]]; then
      __sigil_zeta_enable_capture
      __sigil_history_insert "$command"
    fi
  fi
  rm -f "$handoff_file"
  return "$step_status"
}

sigil_agent_step() {
  __sigil_zeta_turn ",," "$@"
}

sigil_agent_step_auto() {
  __sigil_zeta_turn ",,," "$@"
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
