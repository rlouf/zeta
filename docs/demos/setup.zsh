# Setup for deterministic VHS demo recordings.
#
# The demos run the real Sigil CLI from this checkout. Only external runtime
# dependencies are shimmed: the model endpoint, Pi, and uv's test output.

emulate zsh
unsetopt bg_nice

_sigil_demo_file="${(%):-%x}"
_sigil_demo_root="${_sigil_demo_file:A:h:h:h}"
_sigil_demo_name="${1:-demo}"
_sigil_demo_base="/private/tmp/sigil-vhs-${_sigil_demo_name}"
_sigil_demo_bin="${_sigil_demo_base}/bin"
_sigil_demo_repo="${_sigil_demo_base}/repo"
_sigil_demo_venv_sigil="$_sigil_demo_root/.venv/bin/sigil"
_sigil_demo_uv="$(command -v uv || true)"

export SIGIL_STATE_DIR="${_sigil_demo_base}/state"
export SIGIL_SESSION_ID="${_sigil_demo_name}"
export SIGIL_BIN="${_sigil_demo_bin}/sigil"
export SIGIL_MODEL_NAME="sigil-demo-model"
export PATH="${_sigil_demo_bin}:$PATH"

rm -rf "$_sigil_demo_base"
mkdir -p "$_sigil_demo_bin" "$_sigil_demo_repo/src" "$_sigil_demo_repo/tests"

cat > "$_sigil_demo_bin/sigil" <<EOF
#!/usr/bin/env sh
if [ -x "$_sigil_demo_venv_sigil" ]; then
  exec "$_sigil_demo_venv_sigil" "\$@"
fi
if [ -n "$_sigil_demo_uv" ]; then
  exec "$_sigil_demo_uv" --project "$_sigil_demo_root" run sigil "\$@"
fi
PYTHONPATH="$_sigil_demo_root/src\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m sigil.cli "\$@"
EOF
chmod 755 "$_sigil_demo_bin/sigil"

cat > "$_sigil_demo_bin/pi" <<EOF
#!/usr/bin/env sh
exec python3 "$_sigil_demo_root/docs/demos/fake_pi.py" "\$@"
EOF
chmod 755 "$_sigil_demo_bin/pi"

cat > "$_sigil_demo_bin/uv" <<EOF
#!/usr/bin/env sh
exec python3 "$_sigil_demo_root/docs/demos/fake_uv.py" "\$@"
EOF
chmod 755 "$_sigil_demo_bin/uv"

python3 "$_sigil_demo_root/docs/demos/fake_model_server.py" \
  --port-file "$_sigil_demo_base/model-url" >/dev/null 2>&1 &
export SIGIL_DEMO_MODEL_PID="$!"
__sigil_demo_cleanup() {
  kill "$SIGIL_DEMO_MODEL_PID" >/dev/null 2>&1 || true
}
trap __sigil_demo_cleanup EXIT
for _sigil_demo_i in {1..50}; do
  [[ -s "$_sigil_demo_base/model-url" ]] && break
  sleep 0.05
done
export SIGIL_MODEL_URL="$(cat "$_sigil_demo_base/model-url")"

cat > "$_sigil_demo_repo/src/parser.py" <<'EOF'
def parse_value(value: str):
    return value
EOF

cat > "$_sigil_demo_repo/tests/test_parser.py" <<'EOF'
from src.parser import parse_value
EOF

cat > "$_sigil_demo_repo/README.md" <<'EOF'
# Demo Project

Small project used by Sigil's deterministic VHS recordings.
EOF

sigil_demo_make_parser_change() {
  cat > src/parser.py <<'EOF'
def parse_value(value: str):
    return int(value) if value.isdigit() else value
EOF
}

sigil_demo_make_test_change() {
  cat > tests/test_parser.py <<'EOF'
from src.parser import parse_value


def test_parse_value():
    assert parse_value("42") == 42
EOF
}

sigil_demo_last_lineage() {
  local event_id
  event_id="$(sigil events --json | python3 -c 'import json, sys; print(json.load(sys.stdin)[-1]["id"])')"
  sigil events lineage "$event_id" --json
}

cd "$_sigil_demo_repo" || return 1
git init -q
git config user.email demo@dottxt.ai
git config user.name "Sigil Demo"
git add .
git commit -q -m "initial demo project"

source "$_sigil_demo_root/src/sigil/shell/zsh/sigil.zsh"

# VHS drives the foreground command through stdin. The fd opened by the shell
# binding points at a terminal device VHS cannot answer reliably, so use stdin
# for non-piped confirmations in the recordings.
export SIGIL_TTY=/dev/stdin
unset SIGIL_TTY_FD

unset _sigil_demo_file
unset _sigil_demo_root
unset _sigil_demo_name
unset _sigil_demo_base
unset _sigil_demo_bin
unset _sigil_demo_repo
unset _sigil_demo_venv_sigil
unset _sigil_demo_uv
