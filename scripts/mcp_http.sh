#!/usr/bin/env bash
# Serve Hardy's MCP server over Streamable HTTP so a remote-only client
# (claude.ai custom connectors, and therefore Claude in Chrome) can reach it.
# Claude Desktop and Claude Code speak stdio and do not need this.
#
# Bridge: supergateway (github.com/supercorp-ai/supergateway), the stdio->HTTP
# adapter the MCP community uses; nothing here is bespoke.
#
# Binds localhost only. claude.ai needs a public HTTPS URL, so put a tunnel in
# front (`cloudflared tunnel --url http://localhost:8788` or `ngrok http 8788`)
# and add https://<tunnel-host>/mcp at claude.ai/customize/connectors.
# The endpoint has no auth of its own and generate_board spends your Gemini
# key, so do not leave a tunnel up unattended.
set -euo pipefail
cd "$(dirname "$0")/.."
export HARDY_REPO_ROOT="$PWD"
PORT="${MCP_HTTP_PORT:-8788}"
exec npx -y supergateway \
  --stdio "$PWD/.venv/bin/silkscreen-mcp" \
  --outputTransport streamableHttp \
  --port "$PORT" --host 127.0.0.1 \
  --streamableHttpPath /mcp
