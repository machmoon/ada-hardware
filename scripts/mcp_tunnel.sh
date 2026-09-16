#!/usr/bin/env bash
# Bring up Ada for claude.ai / Claude in Chrome in one go: the token-gated
# Streamable HTTP bridge (scripts/mcp_http.sh) plus a cloudflared quick
# tunnel, then print the connector URL.
#
# A quick tunnel gets a new hostname every run, so after a restart the
# connector at claude.ai/customize/connectors has to be edited to the URL
# this prints. The token is kept at ~/.kaleo/mcp-http-token (0600) and
# reused, so only the host part changes.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v cloudflared >/dev/null || { echo "cloudflared missing: brew install cloudflared" >&2; exit 2; }
mkdir -p ~/.kaleo
TOKEN_FILE=~/.kaleo/mcp-http-token
[ -s "$TOKEN_FILE" ] || { openssl rand -hex 24 > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"; }
export MCP_HTTP_TOKEN="$(cat "$TOKEN_FILE")"
PORT="${MCP_HTTP_PORT:-8788}"
pkill -f "silkscreen.mcp.http" 2>/dev/null || true
pkill -f "cloudflared tunnel --url http://127.0.0.1:$PORT" 2>/dev/null || true
nohup ./scripts/mcp_http.sh > /tmp/hardy-bridge.log 2>&1 &
nohup cloudflared tunnel --url "http://127.0.0.1:$PORT" > /tmp/hardy-tunnel.log 2>&1 &
for _ in $(seq 1 40); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/hardy-tunnel.log | head -1 || true)
  [ -n "$URL" ] && break; sleep 1
done
[ -n "${URL:-}" ] || { echo "tunnel did not come up; see /tmp/hardy-tunnel.log" >&2; exit 1; }
echo "$URL" > ~/.kaleo/mcp-http-url
echo "connector URL (the URL is the credential; paste it at claude.ai/customize/connectors):"
echo "$URL/mcp/$MCP_HTTP_TOKEN"
echo "logs: /tmp/hardy-bridge.log /tmp/hardy-tunnel.log"
