#!/bin/bash
# 写入并加载 LaunchAgent，登录后隐藏启动总控板。
set -euo pipefail
PREFIX="${HARNESS_PREFIX:-$HOME/harness}"
if [ "${1:-}" = "--prefix" ]; then PREFIX="$2"; fi
LABEL="com.harness.board"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"
mkdir -p "$HOME/Library/LaunchAgents" "$PREFIX/logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$PREFIX/board/server.py</string>
    <string>--port</string>
    <string>8765</string>
  </array>
  <key>WorkingDirectory</key><string>$PREFIX/board</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HARNESS_BOARD_TOKEN_FILE</key><string>$PREFIX/board-token.txt</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PREFIX/logs/board.out.log</string>
  <key>StandardErrorPath</key><string>$PREFIX/logs/board.err.log</string>
</dict>
</plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
echo "已写入 $PLIST 并启动。停用：launchctl unload $PLIST"
