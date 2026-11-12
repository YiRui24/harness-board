#!/bin/bash
# 安装 Syncthing，并确保交接盘文件夹存在。设备配对请在 Syncthing 界面里由你自己完成（不要把设备 ID 写进仓库）。
set -euo pipefail
FOLDER="$HOME/交接盘"
if [ "${1:-}" = "--folder" ]; then FOLDER="$2"; fi
if ! command -v syncthing >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then brew install syncthing; else
    echo "请先安装 Homebrew，或从 https://syncthing.net/downloads 安装。" >&2; exit 1
  fi
fi
mkdir -p "$FOLDER/提示词模板"
brew services start syncthing 2>/dev/null || true
echo "交接盘目录：$FOLDER"
echo "请打开 http://127.0.0.1:8384/ ，把这个文件夹加进 Syncthing，并与其它电脑共享。"
echo "不要把设备 ID、API 密钥写进仓库。"
