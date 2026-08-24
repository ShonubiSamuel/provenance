#!/bin/bash
# Builds the two double-clickable macOS launchers at the repo root:
#   "Provenance.app"       — starts the backend + frontend and opens the browser
#   "Provenance (Stop).app" — terminates both
# Both are thin AppleScript wrappers around scripts/start.sh / scripts/stop.sh, compiled
# with osacompile. Regenerate after moving the repo (the launch command is an absolute
# path baked in at compile time) or after renaming the project.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

build_launcher() {
  local app_name="$1" script="$2" log="$3"
  local tmp
  tmp="$(mktemp /tmp/provenance-launcher-XXXXXX.applescript)"
  printf 'do shell script "%s/scripts/%s > %s/.run/%s 2>&1 &"\n' \
    "$PROJECT_DIR" "$script" "$PROJECT_DIR" "$log" > "$tmp"
  rm -rf "$PROJECT_DIR/$app_name"
  osacompile -o "$PROJECT_DIR/$app_name" "$tmp"
  rm -f "$tmp"
  echo "built $app_name"
}

mkdir -p "$PROJECT_DIR/.run"
build_launcher "Provenance.app" "start.sh" "launcher.log"
build_launcher "Provenance (Stop).app" "stop.sh" "launcher.log"
