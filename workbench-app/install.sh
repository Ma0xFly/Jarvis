#!/usr/bin/env bash
# Install the standalone workbench as a systemd *user* service + desktop entry.
# No sudo, no system-wide changes.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${WORKBENCH_PORT:-8787}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/jarvis-workbench.service"

mkdir -p "$UNIT_DIR"
cat >"$UNIT" <<EOF
[Unit]
Description=Jarvis Workbench (kanban UI)
After=default.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=WORKBENCH_PORT=$PORT
ExecStart=$DIR/run.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now jarvis-workbench.service

APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
cat >"$APPS/jarvis-workbench.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Jarvis 工作台
Comment=待办 / 看板 / 已完成（读写本机 kanban.db）
Exec=$DIR/open.sh
Icon=$DIR/web/icon.svg
Terminal=false
Categories=Utility;Development;
StartupWMClass=JarvisWorkbench
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true

echo "已安装："
echo "  systemd user 服务  jarvis-workbench.service (127.0.0.1:$PORT)"
echo "  桌面入口          $APPS/jarvis-workbench.desktop"
systemctl --user --no-pager --lines=0 status jarvis-workbench.service | head -4