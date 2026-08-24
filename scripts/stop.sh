#!/bin/bash
# Terminates the Provenance backend + frontend dev server (the "Stop" counterpart to
# start.sh). Prefers the graceful /shutdown endpoint — same path the in-app Quit button
# uses — so aria2 and the worker pool shut down cleanly; falls back to killing recorded
# PIDs directly if the API isn't responding.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$PROJECT_DIR/.run"
API_PORT=8787

is_port_open() {
  lsof -i "tcp:$1" -sTCP:LISTEN >/dev/null 2>&1
}

if is_port_open "$API_PORT" && curl -fsS -X POST "http://127.0.0.1:$API_PORT/shutdown" >/dev/null 2>&1; then
  echo "stop requested via /shutdown" >> "$LOG_DIR/launcher.log"
  exit 0
fi

# API unreachable — clean up whatever PIDs were last recorded.
echo "API not reachable — killing recorded PIDs directly" >> "$LOG_DIR/launcher.log"
for pid_file in "$LOG_DIR/api.pid" "$LOG_DIR/web.pid"; do
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file")"
    kill "$pid" 2>/dev/null
    rm -f "$pid_file"
  fi
done
