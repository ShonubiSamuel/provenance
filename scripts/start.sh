#!/bin/bash
# Starts the GitHub Code Explorer backend + frontend and opens the browser.
set -euo pipefail

PROJECT_DIR="/Users/samuelshonubi/Documents/Dev/github-code-explorer"
LOG_DIR="$PROJECT_DIR/.run"
mkdir -p "$LOG_DIR"

API_PORT=8787
WEB_PORT=5173

is_port_open() {
  lsof -i "tcp:$1" -sTCP:LISTEN >/dev/null 2>&1
}

wait_for_port() {
  local port="$1"
  local tries=60
  while [ "$tries" -gt 0 ]; do
    if is_port_open "$port"; then
      return 0
    fi
    sleep 1
    tries=$((tries - 1))
  done
  return 1
}

# PIDs are recorded so the app's own Quit button can stop both halves — see
# POST /shutdown. Without them the backend can only ever stop itself, leaving the
# frontend running against nothing.
if ! is_port_open "$API_PORT"; then
  cd "$PROJECT_DIR"
  nohup "$PROJECT_DIR/.venv/bin/uvicorn" apps.api.main:app --port "$API_PORT" \
    > "$LOG_DIR/api.log" 2>&1 &
  echo $! > "$LOG_DIR/api.pid"
  disown
fi

if ! is_port_open "$WEB_PORT"; then
  cd "$PROJECT_DIR/apps/web"
  nohup npm run dev -- --port "$WEB_PORT" > "$LOG_DIR/web.log" 2>&1 &
  echo $! > "$LOG_DIR/web.pid"
  disown
fi

wait_for_port "$WEB_PORT" || true

open "http://localhost:$WEB_PORT"
