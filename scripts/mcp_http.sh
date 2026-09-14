#!/usr/bin/env bash
# Serve Hardy's MCP server over Streamable HTTP so a remote-only client
# (claude.ai custom connectors, and therefore Claude in Chrome) can reach it.
# Claude Desktop and Claude Code speak stdio and do not need this.
#
# The transport is engine/silkscreen/mcp/http.py (stdlib, no download).
#
# Binds localhost only. claude.ai needs a public HTTPS URL, so put a tunnel in
# front (`cloudflared tunnel --url http://localhost:8788` or `ngrok http 8788`)
# and add https://<tunnel-host>/mcp at claude.ai/customize/connectors.
# Set MCP_HTTP_TOKEN to require `Authorization: Bearer <token>`; without it
# the endpoint is open, and generate_board spends your Gemini key, so do not
# leave an unauthenticated tunnel up unattended.
set -euo pipefail
cd "$(dirname "$0")/.."
export HARDY_REPO_ROOT="$PWD"
exec .venv/bin/python -m silkscreen.mcp.http --port "${MCP_HTTP_PORT:-8788}" "$@"
