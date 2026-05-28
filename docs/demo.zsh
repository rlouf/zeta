# Setup for the README demo recording.
#
# Source this from an interactive zsh session before typing Sigil glyphs. It
# keeps the recording pinned to this checkout while still exercising the real
# shell binding and Python CLI.

emulate zsh

_sigil_demo_file="${(%):-%x}"
_sigil_demo_root="${_sigil_demo_file:A:h:h}"
_sigil_demo_bin="/private/tmp/sigil-demo-bin"

export SIGIL_STATE_DIR="${SIGIL_STATE_DIR:-/private/tmp/sigil-demo-state}"
export SIGIL_SESSION_ID="${SIGIL_SESSION_ID:-readme-demo}"
export SIGIL_BIN="${_sigil_demo_bin}/sigil"

rm -rf "$SIGIL_STATE_DIR"
mkdir -p "$_sigil_demo_bin"

cat > "$SIGIL_BIN" <<EOF
#!/usr/bin/env sh
cd "$_sigil_demo_root" || exit 1
exec uv run sigil "\$@"
EOF
chmod 755 "$SIGIL_BIN"

source "$_sigil_demo_root/src/sigil/shell/zsh/sigil.zsh"

unset _sigil_demo_file
unset _sigil_demo_root
unset _sigil_demo_bin
