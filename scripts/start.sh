#!/bin/bash
# Starts the GitHub Code Explorer backend + frontend and opens the browser.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PROJECT_DIR="/Users/samuelshonubi/Documents/Dev/github-code-explorer"
LOG_DIR="$PROJECT_DIR/.run"
mkdir -p "$LOG_DIR"

API_PORT=8787
WEB_PORT=5173
WEB_DIR="$PROJECT_DIR/apps/web"
FRONTEND_DIST="$WEB_DIR/dist"

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

start_dev_server() {
  if ! is_port_open "$WEB_PORT"; then
    cd "$PROJECT_DIR/apps/web"
    nohup npm run dev -- --host 127.0.0.1 --port "$WEB_PORT" > "$LOG_DIR/web.log" 2>&1 &
    echo $! > "$LOG_DIR/web.pid"
    disown
  fi
  wait_for_port "$WEB_PORT" || true
  open "http://localhost:$WEB_PORT"
}

# Serving a prebuilt bundle means the UI can silently lag behind the source: edit a
# component, restart, and you are still looking at whatever was last built. So the build
# is treated as a cache — anything newer in the frontend sources invalidates it.
dist_is_stale() {
  [ ! -f "$FRONTEND_DIST/index.html" ] && return 0
  local newer
  newer=$(find "$WEB_DIR/src" "$WEB_DIR/index.html" "$WEB_DIR/package.json" \
    "$WEB_DIR/vite.config.ts" "$WEB_DIR/tsconfig.json" \
    -newer "$FRONTEND_DIST/index.html" -print -quit 2>/dev/null)
  [ -n "$newer" ]
}

if dist_is_stale; then
  echo "frontend build is missing or out of date — rebuilding" >> "$LOG_DIR/launcher.log"
  if ! (cd "$WEB_DIR" && npm run build >> "$LOG_DIR/web.log" 2>&1); then
    # A broken build must not serve a stale bundle as if it were current: fall back to
    # the dev server, which shows the real error instead of yesterday's UI.
    echo "npm run build failed — falling back to the dev server" >> "$LOG_DIR/launcher.log"
    start_dev_server
    exit 0
  fi
fi

wait_for_port "$API_PORT" || true
open "http://localhost:$API_PORT"
