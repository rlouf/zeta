#!/usr/bin/env zsh
set -euo pipefail

repo_url="${SIGIL_REPO_URL:-https://raw.githubusercontent.com/rlouf/sigil/main}"
install_dir="${SIGIL_SHELL_DIR:-$HOME/.sigil/shell/zsh}"
binding_path="$install_dir/sigil.zsh"
zshrc="${ZDOTDIR:-$HOME}/.zshrc"

mkdir -p "$install_dir"

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$repo_url/shell/zsh/sigil.zsh" -o "$binding_path"
else
  print -u2 "sigil install: curl is required to fetch shell/zsh/sigil.zsh"
  exit 1
fi

chmod 644 "$binding_path"

snippet='
# Sigil
if [[ -r "$HOME/.sigil/shell/zsh/sigil.zsh" ]]; then
  source "$HOME/.sigil/shell/zsh/sigil.zsh"
fi
'

touch "$zshrc"
if ! grep -Fq "$HOME/.sigil/shell/zsh/sigil.zsh" "$zshrc"; then
  print -r -- "$snippet" >> "$zshrc"
fi

print "installed Sigil zsh binding at $binding_path"
print "restart your shell or run: source $zshrc"

for dep in sigil fzf glow pi; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    print -u2 "warning: '$dep' is not on PATH"
  fi
done
