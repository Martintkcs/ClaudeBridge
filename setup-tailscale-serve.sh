#!/usr/bin/env bash
set -euo pipefail

PORT="${CLAUDE_BRIDGE_PORT:-8765}"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "Tailscale CLI was not found."
  echo "Install Tailscale for macOS, then make sure the 'tailscale' command is available."
  exit 1
fi

echo "Configuring Tailscale Serve for Claude Bridge on port $PORT..."
tailscale serve --yes --http 80 --bg "$PORT"
echo ""
echo "If this succeeded, open your MagicDNS URL:"
echo "  http://YOUR-MACHINE.YOUR-TAILNET.ts.net/"
