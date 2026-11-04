#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${WORKBENCH_PORT:-8787}"
URL="http://127.0.0.1:${PORT}"

if ! curl -sf "$URL/healthz" >/dev/null 2>&1; then
  if systemctl --user list-unit-files jarvis-workbench.service >/dev/null 2>&1; then
    systemctl --user start jarvis-workbench.service
  else
    setsid "$DIR/run.sh" >"${TMPDIR:-/tmp}/jarvis-workbench.log" 2>&1 </dev/null &
  fi
  for _ in $(seq 1 40); do
    curl -sf "$URL/healthz" >/dev/null 2>&1 && break
    sleep 0.25
  done
fi

exec google-chrome --app="$URL" --class=JarvisWorkbench