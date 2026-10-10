#!/usr/bin/env bash
# Start/stop the customer-hosted AgntID runtime (public images, about 900 MB
# model download on first start).
#   ./scripts/agntid_runtime.sh up | status | logs | down
# After "up": open http://localhost:18080 and paste your bootstrap token there
# yourself. Never put that token in a file, a command or a chat.
set -euo pipefail
cd "$(dirname "$0")/.."
# Port 8080 is often taken (it is on this machine), so the runtime UI defaults to 18080.
export RUNTIME_PORT=${RUNTIME_PORT:-18080} MCP_PORT=${MCP_PORT:-8082}
touch agntid/.env  # optional GITHUB_TOKEN for the attached GitHub MCP server
COMPOSE=(docker compose -p agntid-runtime --env-file agntid/.env -f agntid/runtime-compose.yml -f agntid/runtime-override.yml)
case "${1:-status}" in
  up)     "${COMPOSE[@]}" up -d; echo "Runtime UI: http://localhost:$RUNTIME_PORT   MCP proxy: http://localhost:$MCP_PORT/mcp" ;;
  status) "${COMPOSE[@]}" ps; curl -fsS http://localhost:$RUNTIME_PORT/health && echo " <- runtime healthy" || echo "runtime not healthy yet" ;;
  logs)   "${COMPOSE[@]}" logs --tail=80 ;;
  down)   "${COMPOSE[@]}" down ;;   # keeps registration and model cache (no -v)
  *)      echo "usage: $0 up|status|logs|down"; exit 2 ;;
esac
