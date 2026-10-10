#!/usr/bin/env bash
# Starts the GitHub MCP server (:8090), backend (:8000), canary listener (:9999) and UI (:5173). Ctrl-C stops all.
set -euo pipefail
cd "$(dirname "$0")/.."
trap 'kill 0' EXIT
.venv/bin/python -m prguard_mcp.server &   # PRGuard's GitHub MCP server (:8090)
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000 --log-level warning &
.venv/bin/uvicorn canary_listener.app:app --host 127.0.0.1 --port 9999 --log-level warning &
npm --prefix frontend run dev &
wait
