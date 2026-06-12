# Sigil zsh bindings. Core behavior lives in the `sigil` executable.
#
# This file should stay boring: it wires zsh lifecycle hooks and punctuation
# functions to the CLI. The agent step workflow keeps prompt insertion and
# shell-turn recording here, but delegates the model/tool loop to Python.

# Exported so `sigil doctor`, which runs as a child process, can tell that an
# ancestor shell loaded the binding.
export SIGIL_BINDING_LOADED="zsh"

# ── CLI Resolution ───────────────────────────────────────────────────────

# Resolve the CLI once at source time, fork-free: SIGIL_BIN lets tests, local
# checkouts, and packaged installs point the binding at a specific executable;
# otherwise the $commands hash answers without a subshell.
if [[ -n "${SIGIL_BIN:-}" ]]; then
  typeset -g __sigil_bin="$SIGIL_BIN"
else
  typeset -g __sigil_bin="${commands[sigil]:-sigil}"
fi

# ── Session And Terminal Context ─────────────────────────────────────────

zmodload zsh/datetime 2>/dev/null

# A session id scopes continuity files such as recent-turns.jsonl. The id is
# generated once per shell process and inherited by subprocesses so CLI calls from
# the same terminal window write to the same session directory. EPOCHREALTIME
# plus the pid is unique without forking uuidgen.
#
# The id is only valid on the pty that created it: tmux servers and nested
# terminals propagate exported variables across panes, so an inherited id
# whose recorded tty is not this shell's tty is regenerated. Same-pty
# subshells keep continuity; an id inherited without a recorded tty is a
# deliberate override and is kept.
if [[ -n "${SIGIL_SESSION_ID:-}" && -n "${SIGIL_SESSION_TTY:-}" \
      && -n "${TTY:-}" && "${SIGIL_SESSION_TTY}" != "${TTY}" ]]; then
  unset SIGIL_SESSION_ID
fi
if [[ -z "${SIGIL_SESSION_ID:-}" ]]; then
  if [[ -n "${EPOCHREALTIME:-}" ]]; then
    export SIGIL_SESSION_ID="${EPOCHREALTIME/./-}-$$"
  else
    __sigil_session_tty="${TTY:-tty}"
    export SIGIL_SESSION_ID="${__sigil_session_tty:t}-$$"
    unset __sigil_session_tty
  fi
fi
if [[ -n "${TTY:-}" ]]; then
  export SIGIL_SESSION_TTY="$TTY"
fi

# ── Prompt And History Helpers ───────────────────────────────────────────

__sigil_history_insert() {
  emulate -L zsh
  # Add a command to zsh history without executing it. Used when Sigil proposes
  # a command so normal history search can find it later.
  [[ -n "${1:-}" ]] || return 0
  print -s -- "$1" 2>/dev/null || true
}

__sigil_prompt_insert() {
  emulate -L zsh
  # zsh can preload editable text into the prompt buffer with print -z. This is
  # what makes comma recommendations inspectable instead of immediately run.
  [[ -n "${1:-}" ]] || return 0
  print -z -- "$1" 2>/dev/null || true
  __sigil_history_insert "$1"
}

__sigil_zeta_prompt_command() {
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 0
  print -r -- "+ $command"
}

__sigil_glyphs_enabled() {
  emulate -L zsh
  # `sigil install --no-glyphs` writes SIGIL_ENABLE_GLYPHS=0 before sourcing this
  # file. The named shell functions remain available either way.
  [[ "${SIGIL_ENABLE_GLYPHS:-1}" != "0" && "${SIGIL_ENABLE_GLYPHS:-1}" != "false" ]]
}

# ── Shell Turn Recording ─────────────────────────────────────────────────

# Every interactive command is recorded at the next prompt: command line,
# exit status, and cwd — never output. A leading space skips recording (the
# ignorespace convention); SIGIL_RECORD=0 disables recording entirely.
#
# Recording is a zero-fork spool append; the CLI ingests the spool at its
# next invocation, before anything reads recent turns or failure context.
# Forking the CLI here would add its startup time to every prompt draw.
typeset -g __sigil_zeta_current_command=""

__sigil_recording_enabled() {
  emulate -L zsh
  [[ "${SIGIL_RECORD:-1}" != "0" && "${SIGIL_RECORD:-1}" != "false" ]]
}

__sigil_zeta_recordable_command() {
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  [[ -n "${command//[[:space:]]/}" ]] || return 1
  case "$command" in
    [[:space:]]*|,*|+*|…|sigil|sigil\ *|*/sigil|*/sigil\ *|__sigil_*|sigil_*|noglob\ sigil_*|noglob\ ,*)
      return 1
      ;;
  esac
  return 0
}

__sigil_zeta_record_shell_turn() {
  emulate -L zsh
  # Field separator \x1f and record separator \x1e cannot appear in fields;
  # stray control bytes in pasted commands become spaces.
  local command="${1//[$'\x1e\x1f']/ }"
  local exit_status="$2"
  local dir="${SIGIL_SESSION_DIR:-${SIGIL_STATE_DIR:-$HOME/.sigil}/sessions/${SIGIL_SESSION_ID:-default}}"
  [[ -d "$dir" ]] || command mkdir -p -- "$dir" 2>/dev/null || return 0
  print -rn -- \
    "${EPOCHREALTIME:-}"$'\x1f'"${command}"$'\x1f'"${exit_status}"$'\x1f'"${PWD//[$'\x1e\x1f']/ }"$'\x1e' \
    >> "$dir/shell-turns.spool" 2>/dev/null || true
}

__sigil_zeta_before_command() {
  emulate -L zsh
  __sigil_zeta_current_command="${1:-}"
}

__sigil_zeta_after_command_before_prompt() {
  local exit_status=$?
  emulate -L zsh
  local command="$__sigil_zeta_current_command"
  __sigil_zeta_current_command=""
  if __sigil_recording_enabled && __sigil_zeta_recordable_command "$command"; then
    __sigil_zeta_record_shell_turn "$command" "$exit_status"
  fi
  return "$exit_status"
}

# ── Command Wrappers ─────────────────────────────────────────────────────

sigil_command() {
  emulate -L zsh
  # `, prompt`: read-only assistant answer. It does not stage commands or mutate
  # history; `,,` and `,,,` are the workflows that can hand a command back to zsh.
  if [[ "$#" == "0" ]]; then
    "$__sigil_bin" ask
  else
    "$__sigil_bin" ask "$*"
  fi
}

__sigil_step_turn() {
  emulate -L zsh
  local workflow="$1"
  shift || true
  local objective handoff_file step_status command
  local -a args
  args=()
  objective="$*"
  handoff_file="$(mktemp "${TMPDIR:-/tmp}/sigil-handoff.XXXXXX")" || return 1
  # Ctrl-C aborts this function mid-flight; the always block is the only
  # cleanup zsh still runs on that path.
  {
    if [[ -z "$objective" ]]; then
      args+=(--continue)
    else
      args+=("$objective")
    fi
    "$__sigil_bin" step --workflow "$workflow" --handoff-file "$handoff_file" "${args[@]}"
    step_status=$?
    if [[ "$step_status" == "0" && -s "$handoff_file" ]]; then
      # The handoff file holds the staged command verbatim.
      command="$(<"$handoff_file")"
      if [[ -n "$command" ]]; then
        __sigil_prompt_insert "$(__sigil_zeta_prompt_command "$command")"
      fi
    fi
  } always {
    rm -f "$handoff_file"
  }
  return "$step_status"
}

sigil_agent_step() {
  emulate -L zsh
  __sigil_step_turn "propose" "$@"
}

sigil_agent_step_auto() {
  emulate -L zsh
  __sigil_step_turn "do" "$@"
}

sigil_run() {
  emulate -L zsh
  # Explicit capture path: run exactly the argv the user provided, stream output
  # live, and let the CLI persist bounded stdout/stderr snippets.
  "$__sigil_bin" run "$@"
}

sigil_status() {
  emulate -L zsh
  "$__sigil_bin" status "$@"
}

# ── Raw Glyph Dispatch ───────────────────────────────────────────────────

# The accept-line widget is the single interactive entry point for glyph
# lines: it captures the raw buffer before zsh parses it, so quotes, parens,
# globs, `#`, and `!` in prompts never reach the parser or history
# expansion. The raw text is stashed and the buffer rewritten to a fixed
# dispatch line with no user text in it, accepted through the normal command
# loop — glyph work runs as a regular foreground job: Ctrl-Z, `jobs`, `fg`,
# `$?`, and preexec/precmd behave like for any other command.
# Outside zle the named functions dispatch; `+` stays widget-only.
typeset -g __sigil_glyph_dispatch_widget_installed="${__sigil_glyph_dispatch_widget_installed:-0}"
typeset -g __sigil_dispatch_glyph=""
typeset -g __sigil_dispatch_text=""
typeset -g __sigil_dispatch_line=""

__sigil_run_plus_capture_command() {
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  SIGIL_RUN_SHELL="${SIGIL_RUN_SHELL:-${SHELL:-zsh}}" "$__sigil_bin" run --shell "$command"
}

__sigil_glyph_split() {
  emulate -L zsh
  # Set reply=(glyph text) for a glyph line; fail for anything else. The
  # glyph must stand alone or be followed by whitespace, so words like
  # `,x` stay ordinary commands. `+` additionally needs a command.
  local buffer="${1:-}" glyph text
  case "$buffer" in
    '+'[[:space:]]*) glyph='+' ;;
    ',,,'|',,,'[[:space:]]*) glyph=',,,' ;;
    ',,'|',,'[[:space:]]*) glyph=',,' ;;
    ','|','[[:space:]]*) glyph=',' ;;
    '?'|'?'[[:space:]]*) glyph='?' ;;
    *) return 1 ;;
  esac
  text="${buffer#"$glyph"}"
  text="${text#"${text%%[![:space:]]*}"}"
  if [[ "$glyph" == '+' && -z "${text//[[:space:]]/}" ]]; then
    return 1
  fi
  reply=("$glyph" "$text")
  return 0
}

__sigil_dispatch() {
  emulate -L zsh
  local glyph="$__sigil_dispatch_glyph"
  local text="$__sigil_dispatch_text"
  local line="$__sigil_dispatch_line"
  typeset -g __sigil_dispatch_glyph=""
  typeset -g __sigil_dispatch_text=""
  typeset -g __sigil_dispatch_line=""
  # Inserting here, while the dispatch line itself is being executed,
  # replaces the rejected dispatch entry that lingers at the top of history
  # until the next command — up-arrow recalls the original immediately.
  [[ -n "$line" ]] && __sigil_history_insert "$line"
  case "$glyph" in
    '+') __sigil_run_plus_capture_command "$text" ;;
    ',') if [[ -n "$text" ]]; then sigil_command "$text"; else sigil_command; fi ;;
    ',,') if [[ -n "$text" ]]; then sigil_agent_step "$text"; else sigil_agent_step; fi ;;
    ',,,') if [[ -n "$text" ]]; then sigil_agent_step_auto "$text"; else sigil_agent_step_auto; fi ;;
    '?') if [[ -n "$text" ]]; then sigil_status "$text"; else sigil_status; fi ;;
    *) return 1 ;;
  esac
}

__sigil_accept_line_with_glyph_dispatch() {
  emulate -L zsh
  # Everything here runs inside the shell: a plain Enter press must not fork.
  local reply
  if ! __sigil_glyph_split "$BUFFER"; then
    zle __sigil_accept_line_without_glyph_dispatch
    return $?
  fi
  typeset -g __sigil_dispatch_glyph="${reply[1]}"
  typeset -g __sigil_dispatch_text="${reply[2]}"
  typeset -g __sigil_dispatch_line="$BUFFER"
  typeset -g __sigil_display_decorated=1
  # Display only: PREDISPLAY survives the final line render and is never
  # parsed, so the finalized line keeps showing what was typed while the
  # executed dispatch word renders as a dim trailer. region_highlight
  # offsets are buffer-relative; appending leaves other plugins' entries
  # alone.
  PREDISPLAY="$BUFFER "
  BUFFER="$__sigil_dispatch_word"
  CURSOR=$#BUFFER
  region_highlight+=("0 $#BUFFER fg=8")
  zle __sigil_accept_line_without_glyph_dispatch
}

# One dim character is all the machinery the finalized line shows; the
# spelled-out name is the fallback where the locale cannot render it.
typeset -g __sigil_dispatch_word="__sigil_dispatch"
if [[ "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" == *[Uu][Tt][Ff]*8* ]]; then
  __sigil_dispatch_word="…"
  function '…' { __sigil_dispatch "$@" }
fi

__sigil_clear_glyph_display() {
  emulate -L zsh
  # PREDISPLAY and region_highlight persist across zle sessions; without
  # this, the next prompt repaints the previous glyph line.
  [[ "${__sigil_display_decorated:-0}" == "1" ]] || return 0
  typeset -g __sigil_display_decorated=0
  PREDISPLAY=""
  region_highlight=()
}

__sigil_install_glyph_dispatch_widget() {
  emulate -L zsh
  [[ $- == *i* ]] || return 0
  [[ "$__sigil_glyph_dispatch_widget_installed" == "1" ]] && return 0
  zle -A accept-line __sigil_accept_line_without_glyph_dispatch 2>/dev/null || return 0
  zle -N accept-line __sigil_accept_line_with_glyph_dispatch 2>/dev/null || return 0
  autoload -Uz add-zle-hook-widget 2>/dev/null || true
  add-zle-hook-widget line-init __sigil_clear_glyph_display 2>/dev/null || true
  __sigil_glyph_dispatch_widget_installed=1
}

# ── Glyph Bindings ───────────────────────────────────────────────────────

if __sigil_glyphs_enabled; then
  # Function definitions make the punctuation usable from scripts and pipes,
  # and keep syntax highlighters treating glyph lines as valid commands.
  # Interactive lines never reach them: the accept-line widget captures the
  # raw buffer first. `+` is widget-only, so zsh never parses a `+ ...` line.
  function ',' { sigil_command "$@" }
  function ',,' { sigil_agent_step "$@" }
  function ',,,' { sigil_agent_step_auto "$@" }
  function '?' { sigil_status "$@" }

  # Aliases serve the non-widget paths only (eval, sourced snippets): alias
  # expansion runs before globbing, which is what lets a bare `?` reach the
  # function instead of filename generation. Interactive lines never get
  # this far — the widget rewrites them first.
  alias ','='noglob sigil_command'
  alias ',,'='noglob sigil_agent_step'
  alias ',,,'='noglob sigil_agent_step_auto'
  alias '?'='noglob sigil_status'

  __sigil_install_glyph_dispatch_widget

  # `+ cargo te<TAB>` completes like `cargo te<TAB>`: drop the glyph word
  # and re-enter completion, the way sudo/nohup completions do. Needs
  # compsys: sourced before compinit, `+` keeps default completion.
  _sigil_plus() {
    shift words
    (( CURRENT-- ))
    _normal
  }
  if (( ${+functions[compdef]} )); then
    compdef _sigil_plus '+' 2>/dev/null || true
  fi
fi

# ── zsh Command Lifecycle Hooks ──────────────────────────────────────────

__sigil_install_lifecycle_hooks() {
  emulate -L zsh
  autoload -Uz add-zsh-hook
  add-zsh-hook preexec __sigil_zeta_before_command
  add-zsh-hook precmd __sigil_zeta_after_command_before_prompt
  # $? at precmd entry is the user command's status only for the first hook;
  # any hook that runs earlier overwrites it with its own return status. Keep
  # the sigil hook first no matter when this file is sourced.
  precmd_functions=(
    __sigil_zeta_after_command_before_prompt
    ${precmd_functions:#__sigil_zeta_after_command_before_prompt}
  )
}
__sigil_install_lifecycle_hooks

# ── History Filtering ────────────────────────────────────────────────────

# Glyph lines are prompts, not commands the shell can re-run: they stay out
# of the history file but remain recallable with up-arrow in the session.
# The rewritten dispatch line is internal plumbing and is saved nowhere; the
# widget already inserted the original glyph line by hand.
if __sigil_glyphs_enabled; then
  __sigil_zshaddhistory() {
    emulate -L zsh
    local line="${1%%$'\n'}"
    case "$line" in
      __sigil_dispatch|…) return 1 ;;
      ,*|\?*|+*) return 2 ;;
    esac
    return 0
  }
  add-zsh-hook zshaddhistory __sigil_zshaddhistory
fi
