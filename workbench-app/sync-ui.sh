#!/usr/bin/env bash
# Copy the dashboard plugin's UI files into the standalone app.
# server.py / theme.css / index.html are standalone-only and untouched.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${WORKBENCH_PLUGIN_DIR:-$HOME/.hermes/plugins/workbench/dashboard}"
[ -f "$SRC/index.js" ] || { echo "找不到插件 UI: $SRC/index.js" >&2; exit 1; }
cp "$SRC/index.js" "$DIR/web/app.js"
cp "$SRC/style.css" "$DIR/web/style.css"
echo "已同步：app.js / style.css"
echo "重启服务：systemctl --user restart jarvis-workbench"