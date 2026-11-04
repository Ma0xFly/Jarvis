#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${WORKBENCH_PYTHON:-}"
if [ -z "$PY" ]; then
  PY="$(ls -d "$HOME"/.hermes/tools/python-3.14.9*/bin/python3 2>/dev/null | head -1 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  PY="$(ls -d "$HOME"/.hermes/tools/python-3.14*/bin/python3 2>/dev/null | head -1 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "找不到 Hermes 运行时 Python（~/.hermes/tools/python-3.14*/bin/python3）" >&2
  exit 1
fi
exec "$PY" -I "$DIR/server.py" "$@"