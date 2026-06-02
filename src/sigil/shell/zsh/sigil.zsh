# Sigil zsh bindings. Core behavior lives in the `sigil` executable.
#
# This file should stay boring: it wires zsh lifecycle hooks and punctuation
# functions to the CLI. The Zeta glyph route is the exception: zsh owns that
# control loop and calls the Zeta CLI for model, tool, and transcript services.

# ── CLI Resolution ───────────────────────────────────────────────────────

# Resolve the CLI once at source time. SIGIL_BIN lets tests, local checkouts, and
# packaged installs point the binding at a specific executable without changing
# the user's PATH.
if [[ -n "${SIGIL_BIN:-}" ]]; then
  typeset -g __sigil_bin="$SIGIL_BIN"
elif command -v sigil >/dev/null 2>&1; then
  typeset -g __sigil_bin="$(command -v sigil)"
else
  typeset -g __sigil_bin="sigil"
fi

if [[ -n "${ZETA_BIN:-}" ]]; then
  typeset -g __zeta_bin="$ZETA_BIN"
elif command -v zeta >/dev/null 2>&1; then
  typeset -g __zeta_bin="$(command -v zeta)"
elif [[ "$__sigil_bin" == */* && -x "${__sigil_bin:h}/zeta" ]]; then
  typeset -g __zeta_bin="${__sigil_bin:h}/zeta"
else
  typeset -g __zeta_bin="zeta"
fi

# ── Session And Terminal Context ─────────────────────────────────────────

# A session id scopes continuity files such as recent-turns.jsonl. The id is
# generated once per shell process and inherited by subprocesses so CLI calls from
# the same terminal window write to the same session directory.
if [[ -z "${SIGIL_SESSION_ID:-}" ]]; then
  if command -v uuidgen >/dev/null 2>&1; then
    export SIGIL_SESSION_ID="$(uuidgen)"
  else
    __sigil_session_tty="${TTY:-tty}"
    export SIGIL_SESSION_ID="${__sigil_session_tty:t}-$$"
    unset __sigil_session_tty
  fi
fi

# Keep a stable terminal path/fd around for CLI prompts that need to ask the user
# even when stdin/stdout are part of a pipeline. These are not used to capture
# command output.
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
if [[ -z "${ZETA_TTY_FD:-}" && -n "${SIGIL_TTY_FD:-}" ]]; then
  export ZETA_TTY_FD="$SIGIL_TTY_FD"
fi

# ── Prompt And History Helpers ───────────────────────────────────────────

__sigil_history_insert() {
  # Add a command to zsh history without executing it. Used when Sigil proposes
  # a command so normal history search can find it later.
  [[ -n "${1:-}" ]] || return 0
  print -s -- "$1" 2>/dev/null || true
}

__sigil_prompt_insert() {
  # zsh can preload editable text into the prompt buffer with print -z. This is
  # what makes comma recommendations inspectable instead of immediately run.
  [[ -n "${1:-}" ]] || return 0
  print -z -- "$1" 2>/dev/null || true
  __sigil_history_insert "$1"
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
  # `sigil install --no-glyphs` writes SIGIL_ENABLE_GLYPHS=0 before sourcing this
  # file. The named shell functions remain available either way.
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

# ── Command Wrappers ─────────────────────────────────────────────────────

sigil_command() {
  # `, prompt`: ask the model for one command, print the response, and insert the
  # first line back into the editable prompt buffer.
  local response command
  response="$("$__sigil_bin" op "," "$@")" || return $?
  print -r -- "$response"
  command="${response%%$'\n'*}"
  __sigil_prompt_insert "$command"
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
    print -r -- $'\033[38;2;110;106;134m'"$1"$'\033[0m'
  else
    print -r -- "$1"
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
    __sigil_muted_print "❯ ${(r:5:)name}  $detail"
  else
    __sigil_muted_print "❯ $name"
  fi
}

__sigil_zeta_turn() {
  local objective request events event event_type text name input analysis result command reason artifact
  local tool_call_record tool_call_id
  local step continue_step
  objective="$*"
  continue_step=0
  if [[ -z "$objective" ]]; then
    continue_step=1
    objective="Continue the active Zeta step. Use recent shell activity as the result of the command(s) the user chose to run after the last handoff. If no relevant shell turn appears, ask for the command result instead of inventing it."
  fi
  if [[ "$continue_step" == "1" ]]; then
    "$__zeta_bin" transcript shell-result >/dev/null 2>&1 || true
  fi
  __sigil_zeta_append "$(printf '{"type":"user_message","content":%s}' "$(__sigil_json_string "$objective")")" >/dev/null
  for step in {1..8}; do
    request="$(printf '{"objective":%s}' "$(__sigil_json_string "$objective")")"
    events="$(__sigil_zeta_model_stream "$request")" || return $?
    while IFS= read -r event; do
      [[ -n "$event" ]] || continue
      event_type="$(printf '%s\n' "$event" | __sigil_json_get type)"
      case "$event_type" in
        assistant_delta)
          text="$(printf '%s\n' "$event" | __sigil_json_get text)"
          [[ -n "$text" ]] && print -r -- "$text"
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
          command="$(printf '%s\n' "$result" | __sigil_json_get handoff.command)"
          if [[ -n "$command" ]]; then
            reason="$(printf '%s\n' "$result" | __sigil_json_get handoff.reason)"
            artifact="$(printf '%s\n' "$result" | __sigil_json_get handoff.artifact)"
            [[ -n "$reason" ]] && print -r -- "$reason"
            [[ -n "$artifact" ]] && print -r -- "artifact: $artifact"
            __sigil_prompt_insert "$command"
            return 0
          fi
          break
          ;;
        error)
          print -r -- "$(printf '%s\n' "$event" | __sigil_json_get message)"
          return 1
          ;;
      esac
    done <<< "$events"
  done
  print -r -- "Zeta stopped after reaching the step budget."
  return 1
}

__sigil_op() {
  # Shared helper for agent/goal routes. They run through the generic operator
  # CLI.
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
  # Explicit capture path: run exactly the argv the user provided, stream output
  # live, and let the CLI persist bounded stdout/stderr snippets.
  "$__sigil_bin" run "$@"
}

sigil_goal() {
  __sigil_op "@" "$@"
}

sigil_goal_auto() {
  __sigil_op "@@" "$@"
}

# ── Glyph Bindings ───────────────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  # Function definitions make the punctuation usable in non-alias contexts.
  function ',' { sigil_command "$@" }
  function ',,' { sigil_agent_step "$@" }
  function ',,,' { sigil_agent_step_auto "$@" }
  function '?' { sigil_question "$@" }
  function '??' { sigil_web_question "$@" }
  function '+' { sigil_run "$@" }
  function '@' { sigil_goal "$@" }
  function '@@' { sigil_goal_auto "$@" }

  # Aliases keep zsh from treating user prompts as glob patterns before our
  # functions receive them. `alias --` is required for `+` because zsh otherwise
  # parses the alias name as an option.
  alias ','='noglob sigil_command'
  alias ',,'='noglob sigil_agent_step'
  alias ',,,'='noglob sigil_agent_step_auto'
  alias '?'='noglob sigil_question'
  alias '??'='noglob sigil_web_question'
  alias -- '+'='noglob sigil_run'
  alias '@'='noglob sigil_goal'
  alias '@@'='noglob sigil_goal_auto'
fi

# ── zsh Command Lifecycle Hooks ──────────────────────────────────────────

autoload -Uz add-zsh-hook
typeset -g __sigil_current_command=""

__sigil_before_command() {
  # preexec runs before the command executes.
  # zsh gives us the command line before execution.
  __sigil_current_command="$1"
}

add-zsh-hook preexec __sigil_before_command

__sigil_recordable_command() {
  local command="${1:-}"
  [[ -n "$command" ]] || return 1

  case "$command" in
    # Match shell history convention: leading-space commands are private.
    [[:space:]]*)
      return 1
      ;;
    # Sigil glyph routes are prompts/instructions, not ordinary shell commands.
    # The CLI paths they call record their own structured events.
    ,*|\?*|+*|@*)
      return 1
      ;;
    # Avoid recursive bookkeeping for direct Sigil calls and internal wrappers.
    sigil\ *|sigil_*|noglob\ sigil_*|command\ sigil_*|__sigil_*)
      return 1
      ;;
  esac

  return 0
}


__sigil_record_turn() {
  local exit_status="$1"
  local command="$2"
  local stdout_snippet stderr_snippet
  # Snippets come from explicit paths such as `sigil run` used to record the
  # output of commands.
  stdout_snippet="${SIGIL_FAILURE_STDOUT:-}"
  stderr_snippet="${SIGIL_FAILURE_STDERR:-}"
  local record_args=(record-turn --status "$exit_status" --cwd "$PWD")
  [[ -n "$stdout_snippet" ]] && record_args+=(--stdout-snippet "$stdout_snippet")
  [[ -n "$stderr_snippet" ]] && record_args+=(--stderr-snippet "$stderr_snippet")
  # Recording must never perturb the user's shell. All CLI output is silenced and
  # failures are ignored; losing a telemetry event is preferable to breaking the
  # prompt after every command.
  "$__sigil_bin" "${record_args[@]}" "$command" >/dev/null 2>&1 || true
}

__sigil_after_command_before_prompt() {
  # precmd runs after the command finishes and before the next prompt. At this
  # point `$?` is the command's exit status and `$PWD` is the final cwd. We pair
  # those with the command line captured in preexec.
  local exit_status=$?
  local command="$__sigil_current_command"
  __sigil_current_command=""
  if [[ -z "$command" ]]; then
    # No preceding command, e.g. first prompt in a new shell.
    return "$exit_status"
  fi
  if ! __sigil_recordable_command "$command"; then
    # Sigil's own punctuation routes are handled by their explicit CLI paths.
    return "$exit_status"
  fi
  __sigil_record_turn "$exit_status" "$command"
  unset SIGIL_FAILURE_STDOUT SIGIL_FAILURE_STDERR
  return "$exit_status"
}

add-zsh-hook precmd __sigil_after_command_before_prompt

# ── History Filtering ────────────────────────────────────────────────────

# Shell history should stay a list of things the shell can re-run. Sigil
# instructions are prompts, not shell commands.
if __sigil_glyphs_enabled; then
  __sigil_zshaddhistory() {
    emulate -L zsh
    local line="${1%%$'\n'}"
    case "$line" in
      ,*|\?*|\\\?*|+*|@*) return 1 ;;
    esac
    return 0
  }
  add-zsh-hook zshaddhistory __sigil_zshaddhistory
fi
