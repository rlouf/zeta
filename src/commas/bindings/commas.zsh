# Commas zsh bindings. Core behavior lives in the `commas` executable.
#
# This file should stay boring: it wires zsh lifecycle hooks and punctuation
# functions to the CLI. The agent step workflow keeps prompt insertion and
# shell-turn recording here, but delegates the model/tool loop to Python.

# Exported so `commas doctor`, which runs as a child process, can tell that an
# ancestor shell loaded the binding.
export COMMAS_BINDING_LOADED="zsh"

# ── CLI Resolution ───────────────────────────────────────────────────────

# Resolve the CLI once at source time, fork-free: COMMAS_BIN lets tests, local
# checkouts, and packaged installs point the binding at a specific executable;
# otherwise the $commands hash answers without a subshell.
if [[ -n "${COMMAS_BIN:-}" ]]; then
  typeset -g __commas_bin="$COMMAS_BIN"
else
  typeset -g __commas_bin="${commands[commas]:-commas}"
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
if [[ -n "${COMMAS_SESSION_ID:-}" && -n "${COMMAS_SESSION_TTY:-}" \
      && -n "${TTY:-}" && "${COMMAS_SESSION_TTY}" != "${TTY}" ]]; then
  unset COMMAS_SESSION_ID
fi
if [[ -z "${COMMAS_SESSION_ID:-}" ]]; then
  if [[ -n "${EPOCHREALTIME:-}" ]]; then
    export COMMAS_SESSION_ID="${EPOCHREALTIME/./-}-$$"
  else
    __commas_session_tty="${TTY:-tty}"
    export COMMAS_SESSION_ID="${__commas_session_tty:t}-$$"
    unset __commas_session_tty
  fi
fi
if [[ -n "${TTY:-}" ]]; then
  export COMMAS_SESSION_TTY="$TTY"
fi

# ── Prompt And History Helpers ───────────────────────────────────────────

__commas_history_insert() {
  emulate -L zsh
  # Add a command to zsh history without executing it. Used when Commas proposes
  # a command so normal history search can find it later.
  [[ -n "${1:-}" ]] || return 0
  print -s -- "$1" 2>/dev/null || true
}

__commas_prompt_insert() {
  emulate -L zsh
  # zsh can preload editable text into the prompt buffer with print -z. This is
  # what makes comma recommendations inspectable instead of immediately run.
  [[ -n "${1:-}" ]] || return 0
  print -z -- "$1" 2>/dev/null || true
  __commas_history_insert "$1"
}

__commas_zeta_prompt_command() {
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 0
  print -r -- "+ $command"
}

__commas_glyphs_enabled() {
  emulate -L zsh
  # `commas install --no-glyphs` writes COMMAS_ENABLE_GLYPHS=0 before sourcing this
  # file. The named shell functions remain available either way.
  [[ "${COMMAS_ENABLE_GLYPHS:-1}" != "0" && "${COMMAS_ENABLE_GLYPHS:-1}" != "false" ]]
}

# ── Shell Turn Recording ─────────────────────────────────────────────────

# Every interactive command is recorded at the next prompt: command line,
# exit status, and cwd — never output. A leading space skips recording (the
# ignorespace convention); COMMAS_RECORD=0 disables recording entirely.
#
# Recording is a zero-fork spool append; the CLI ingests the spool at its
# next invocation, before anything reads recent turns or failure context.
# Forking the CLI here would add its startup time to every prompt draw.
typeset -g __commas_zeta_current_command=""

__commas_recording_enabled() {
  emulate -L zsh
  [[ "${COMMAS_RECORD:-1}" != "0" && "${COMMAS_RECORD:-1}" != "false" ]]
}

__commas_zeta_recordable_command() {
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  [[ -n "${command//[[:space:]]/}" ]] || return 1
  case "$command" in
    [[:space:]]*|,*|+*|…|commas|commas\ *|*/commas|*/commas\ *|__commas_*|commas_*|noglob\ commas_*|noglob\ ,*)
      return 1
      ;;
  esac
  return 0
}

__commas_zeta_record_shell_turn() {
  emulate -L zsh
  # Field separator \x1f and record separator \x1e cannot appear in fields;
  # stray control bytes in pasted commands become spaces.
  local command="${1//[$'\x1e\x1f']/ }"
  local exit_status="$2"
  local dir="${COMMAS_SESSION_DIR:-${COMMAS_STATE_DIR:-$HOME/.commas}/sessions/${COMMAS_SESSION_ID:-default}}"
  [[ -d "$dir" ]] || command mkdir -p -- "$dir" 2>/dev/null || return 0
  print -rn -- \
    "${EPOCHREALTIME:-}"$'\x1f'"${command}"$'\x1f'"${exit_status}"$'\x1f'"${PWD//[$'\x1e\x1f']/ }"$'\x1e' \
    >> "$dir/shell-turns.spool" 2>/dev/null || true
}

__commas_zeta_before_command() {
  emulate -L zsh
  __commas_zeta_current_command="${1:-}"
}

__commas_zeta_after_command_before_prompt() {
  local exit_status=$?
  emulate -L zsh
  local command="$__commas_zeta_current_command"
  __commas_zeta_current_command=""
  if __commas_recording_enabled && __commas_zeta_recordable_command "$command"; then
    __commas_zeta_record_shell_turn "$command" "$exit_status"
  fi
  return "$exit_status"
}

# ── Command Wrappers ─────────────────────────────────────────────────────

commas_command() {
  emulate -L zsh
  # `, prompt`: read-only assistant answer. It does not stage commands or mutate
  # history; `,,` and `,,,` are the workflows that can hand a command back to zsh.
  # A bare `,` composes the question in $EDITOR (CLI-side).
  if [[ "$#" == "0" ]]; then
    "$__commas_bin" ask
  else
    "$__commas_bin" ask "$*"
  fi
}

__commas_step_turn() {
  emulate -L zsh
  local workflow="$1"
  shift || true
  local objective handoff_file step_status command
  local -a args
  args=()
  # `--continue` is only ever passed by the `+` resume path; a bare glyph
  # sends no positional and the CLI composes the objective in $EDITOR.
  if [[ "${1:-}" == "--continue" ]]; then
    args+=(--continue)
    shift
  fi
  objective="$*"
  handoff_file="$(mktemp "${TMPDIR:-/tmp}/commas-handoff.XXXXXX")" || return 1
  # Ctrl-C aborts this function mid-flight; the always block is the only
  # cleanup zsh still runs on that path.
  {
    if [[ -n "$objective" ]]; then
      args+=("$objective")
    fi
    "$__commas_bin" step --workflow "$workflow" --handoff-file "$handoff_file" "${args[@]}"
    step_status=$?
    if [[ "$step_status" == "0" && -s "$handoff_file" ]]; then
      # The handoff file holds the staged command verbatim.
      command="$(<"$handoff_file")"
      if [[ -n "$command" ]]; then
        # Running the staged command through `+` resumes this workflow.
        typeset -g __commas_resume_workflow="$workflow"
        __commas_prompt_insert "$(__commas_zeta_prompt_command "$command")"
      fi
    fi
  } always {
    rm -f "$handoff_file"
  }
  return "$step_status"
}

commas_agent_step() {
  emulate -L zsh
  __commas_step_turn "propose" "$@"
}

commas_agent_step_auto() {
  emulate -L zsh
  __commas_step_turn "do" "$@"
}

commas_run() {
  emulate -L zsh
  # Explicit capture path: run exactly the argv the user provided, stream output
  # live, and let the CLI persist bounded stdout/stderr snippets.
  "$__commas_bin" run "$@"
}

commas_status() {
  emulate -L zsh
  "$__commas_bin" status "$@"
}

# ── Raw Plus Capture ─────────────────────────────────────────────────────

# The comma glyphs and `?` are ordinary commands: zsh parses the line, the
# functions below receive argv, and shell quoting works exactly as for any
# other tool — double quotes interpolate, single quotes are literal,
# redirects and pipes compose. The accept-line widget captures only `+`
# lines, whose text is raw shell grammar for `commas run --shell` and cannot
# travel through argv without being re-evaluated. The captured text is
# stashed and the buffer rewritten to a fixed dispatch word accepted through
# the normal command loop, so a `+` command is a regular foreground job:
# Ctrl-Z, `jobs`, `fg`, `$?`, and preexec/precmd all behave normally.
typeset -g __commas_glyph_dispatch_widget_installed="${__commas_glyph_dispatch_widget_installed:-0}"
typeset -g __commas_dispatch_text=""
typeset -g __commas_dispatch_line=""
typeset -g __commas_resume_workflow=""

__commas_run_plus_capture_command() {
  emulate -L zsh
  local command="${1:-}"
  [[ -n "$command" ]] || return 1
  local run_status resume_file=""
  local -a resume_args
  resume_args=()
  # Running the staged handoff command resumes the step that staged it.
  # The CLI writes the marker only when this command matches the pending
  # handoff, so unrelated `+` commands stay plain capture.
  # COMMAS_AUTO_CONTINUE=0 opts out.
  if [[ "${COMMAS_AUTO_CONTINUE:-1}" != "0" ]]; then
    resume_file="$(mktemp "${TMPDIR:-/tmp}/commas-resume.XXXXXX")" 2>/dev/null || resume_file=""
    [[ -n "$resume_file" ]] && resume_args=(--resume-file "$resume_file")
  fi
  {
    COMMAS_RUN_SHELL="${COMMAS_RUN_SHELL:-${SHELL:-zsh}}" "$__commas_bin" run "${resume_args[@]}" --shell "$command"
    run_status=$?
    if [[ -n "$resume_file" && -s "$resume_file" ]]; then
      __commas_step_turn "${__commas_resume_workflow:-propose}" --continue
    fi
  } always {
    [[ -n "$resume_file" ]] && rm -f "$resume_file"
  }
  return "$run_status"
}

__commas_glyph_split() {
  emulate -L zsh
  # Set reply=(text) for a `+` line with a non-blank command; fail for
  # anything else. `+` must be followed by whitespace, so words like `+x`
  # stay ordinary commands.
  local buffer="${1:-}"
  [[ "$buffer" == '+'[[:space:]]* ]] || return 1
  local text="${buffer#+}"
  text="${text#"${text%%[![:space:]]*}"}"
  [[ -n "${text//[[:space:]]/}" ]] || return 1
  reply=("$text")
  return 0
}

__commas_dispatch() {
  emulate -L zsh
  # The stash is read here but cleared at the next line-init, which runs in
  # the parent shell whatever subshell this lands in.
  __commas_run_plus_capture_command "$__commas_dispatch_text"
}

__commas_accept_line_with_glyph_dispatch() {
  emulate -L zsh
  # Everything here runs inside the shell: a plain Enter press must not fork.
  local reply
  if ! __commas_glyph_split "$BUFFER"; then
    zle __commas_accept_line_without_glyph_dispatch
    return $?
  fi
  typeset -g __commas_dispatch_text="${reply[1]}"
  typeset -g __commas_dispatch_line="$BUFFER"
  typeset -g __commas_display_decorated=1
  # Display only: PREDISPLAY survives the final line render and is never
  # parsed, so the finalized line keeps showing what was typed while the
  # executed dispatch word renders as a dim trailer. region_highlight
  # offsets are buffer-relative; appending leaves other plugins' entries
  # alone.
  PREDISPLAY="$BUFFER "
  BUFFER="$__commas_dispatch_word"
  CURSOR=$#BUFFER
  region_highlight+=("0 $#__commas_dispatch_word fg=8")
  zle __commas_accept_line_without_glyph_dispatch
}

# One dim character is all the machinery the finalized line shows; the
# spelled-out name is the fallback where the locale cannot render it.
typeset -g __commas_dispatch_word="__commas_dispatch"
if [[ "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" == *[Uu][Tt][Ff]*8* ]]; then
  __commas_dispatch_word="…"
  function '…' { __commas_dispatch "$@" }
fi

__commas_clear_glyph_display() {
  emulate -L zsh
  # Runs at the next line-init, in the parent shell. Inserting the original
  # line here replaces the rejected dispatch line that lingers at the top
  # of history, so up-arrow recalls what was typed. PREDISPLAY and
  # region_highlight persist across zle sessions; without clearing, the
  # next prompt repaints the previous `+` line.
  [[ "${__commas_display_decorated:-0}" == "1" ]] || return 0
  typeset -g __commas_display_decorated=0
  [[ -n "$__commas_dispatch_line" ]] && __commas_history_insert "$__commas_dispatch_line"
  typeset -g __commas_dispatch_text=""
  typeset -g __commas_dispatch_line=""
  PREDISPLAY=""
  region_highlight=()
}

__commas_install_glyph_dispatch_widget() {
  emulate -L zsh
  [[ $- == *i* ]] || return 0
  [[ "$__commas_glyph_dispatch_widget_installed" == "1" ]] && return 0
  zle -A accept-line __commas_accept_line_without_glyph_dispatch 2>/dev/null || return 0
  zle -N accept-line __commas_accept_line_with_glyph_dispatch 2>/dev/null || return 0
  autoload -Uz add-zle-hook-widget 2>/dev/null || true
  add-zle-hook-widget line-init __commas_clear_glyph_display 2>/dev/null || true
  __commas_glyph_dispatch_widget_installed=1
}

# ── Glyph Bindings ───────────────────────────────────────────────────────

if __commas_glyphs_enabled; then
  # The comma glyphs and `?` are ordinary commands: these functions are the
  # interactive path, and what makes highlighters treat glyph lines as
  # valid. `+` is widget-only, so zsh never parses a `+ ...` line.
  function ',' { commas_command "$@" }
  function ',,' { commas_agent_step "$@" }
  function ',,,' { commas_agent_step_auto "$@" }
  function '?' { commas_status "$@" }

  # Alias expansion runs before globbing, which is what lets a bare `?`
  # reach the function instead of filename generation, and noglob keeps
  # unquoted glob characters in prompts literal.
  alias ','='noglob commas_command'
  alias ',,'='noglob commas_agent_step'
  alias ',,,'='noglob commas_agent_step_auto'
  alias '?'='noglob commas_status'

  __commas_install_glyph_dispatch_widget

  # `+ cargo te<TAB>` completes like `cargo te<TAB>`: drop the glyph word
  # and re-enter completion, the way sudo/nohup completions do. Needs
  # compsys: sourced before compinit, `+` keeps default completion.
  _commas_plus() {
    shift words
    (( CURRENT-- ))
    _normal
  }
  if (( ${+functions[compdef]} )); then
    compdef _commas_plus '+' 2>/dev/null || true
  fi
fi

# ── zsh Command Lifecycle Hooks ──────────────────────────────────────────

__commas_install_lifecycle_hooks() {
  emulate -L zsh
  autoload -Uz add-zsh-hook
  add-zsh-hook preexec __commas_zeta_before_command
  add-zsh-hook precmd __commas_zeta_after_command_before_prompt
  # $? at precmd entry is the user command's status only for the first hook;
  # any hook that runs earlier overwrites it with its own return status. Keep
  # the commas hook first no matter when this file is sourced.
  precmd_functions=(
    __commas_zeta_after_command_before_prompt
    ${precmd_functions:#__commas_zeta_after_command_before_prompt}
  )
}
__commas_install_lifecycle_hooks

# ── History Filtering ────────────────────────────────────────────────────

# Glyph lines are prompts, not commands the shell can re-run: they stay out
# of the history file but remain recallable with up-arrow in the session.
# The rewritten dispatch line is internal plumbing and is saved nowhere; the
# widget already inserted the original glyph line by hand.
if __commas_glyphs_enabled; then
  __commas_zshaddhistory() {
    emulate -L zsh
    local line="${1%%$'\n'}"
    case "$line" in
      __commas_dispatch|…) return 1 ;;
      ,*|\?*|+*) return 2 ;;
    esac
    return 0
  }
  add-zsh-hook zshaddhistory __commas_zshaddhistory
fi
