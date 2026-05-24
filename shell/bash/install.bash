#!/usr/bin/env bash
set -euo pipefail

repo_url="${SIGIL_REPO_URL:-https://raw.githubusercontent.com/rlouf/sigil/main}"
install_dir="${SIGIL_SHELL_DIR:-$HOME/.sigil/shell/bash}"
binding_path="$install_dir/sigil.bash"
bashrc="${SIGIL_BASH_RC:-$HOME/.bashrc}"

mkdir -p "$install_dir"

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$repo_url/shell/bash/sigil.bash" -o "$binding_path"
else
  printf '%s\n' "sigil install: curl is required to fetch shell/bash/sigil.bash" >&2
  exit 1
fi

chmod 644 "$binding_path"

snippet='
# Sigil
if [[ -r "$HOME/.sigil/shell/bash/sigil.bash" ]]; then
  source "$HOME/.sigil/shell/bash/sigil.bash"
fi
'

touch "$bashrc"
if ! grep -Fq "$HOME/.sigil/shell/bash/sigil.bash" "$bashrc"; then
  printf '%s\n' "$snippet" >> "$bashrc"
fi

printf '%s\n' "installed Sigil bash binding at $binding_path"
printf '%s\n' "restart your shell or run: source $bashrc"

for dep in sigil fzf glow pi; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    printf '%s\n' "warning: '$dep' is not on PATH" >&2
  fi
done

