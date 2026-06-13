#!/usr/bin/env bash
set -euo pipefail

NO_START=0
if [[ "${1:-}" == "--no-start" ]]; then
  NO_START=1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CLAUDE_BRIDGE_PORT:-8765}"
CONFIG_PATH="$PROJECT_ROOT/config.json"

step() {
  printf '[Claude Bridge] %s\n' "$1"
}

warn() {
  printf '[Claude Bridge] %s\n' "$1" >&2
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

find_tailscale() {
  if command -v tailscale >/dev/null 2>&1; then
    command -v tailscale
    return 0
  fi
  return 1
}

cd "$PROJECT_ROOT"

PYTHON_BIN="$(find_python || true)"
if [[ -z "${PYTHON_BIN:-}" ]]; then
  warn "Python was not found. Install Python 3.10+ and try again."
  exit 1
fi

step "Preparing local config..."
if [[ ! -f "$CONFIG_PATH" ]]; then
  "$PYTHON_BIN" -c "import app; print(app.CONFIG['token'])" >/dev/null
fi

TOKEN="$("$PYTHON_BIN" -c "import json; print(json.load(open('config.json', encoding='utf-8'))['token'])")"
TAILSCALE_BIN="$(find_tailscale || true)"

if [[ -n "${TAILSCALE_BIN:-}" ]]; then
  step "Checking Tailscale..."
  if "$TAILSCALE_BIN" status >/dev/null 2>&1; then
    step "Configuring Tailscale Serve for port $PORT..."
    if "$TAILSCALE_BIN" serve --yes --http 80 --bg "$PORT"; then
      step "Tailscale Serve is pointing to http://127.0.0.1:$PORT/"
    else
      warn "Tailscale Serve could not be configured from this shell."
      warn "Run once manually: tailscale serve --yes --http 80 --bg $PORT"
    fi
  else
    warn "Tailscale CLI is installed, but the device is not connected."
    warn "Open the Tailscale app and sign in, or run: tailscale up"
  fi
else
  warn "Tailscale CLI was not found. Local access will still work."
  warn "Install Tailscale and make the 'tailscale' command available for remote access."
fi

printf '\nOpen locally:\n'
printf '  http://127.0.0.1:%s/\n\n' "$PORT"
printf 'Open from your phone through Tailscale:\n'
printf '  http://YOUR-MACHINE.YOUR-TAILNET.ts.net/\n\n'
printf 'Login token:\n'
printf '  %s\n\n' "$TOKEN"

if [[ "$NO_START" == "1" ]]; then
  step "Setup check finished. The app was not started because --no-start was used."
  exit 0
fi

step "Starting Claude Bridge..."
exec "$PYTHON_BIN" -u "$PROJECT_ROOT/app.py" --host 0.0.0.0 --port "$PORT"
