#!/bin/bash
# 安装 SyncClipboard。账号和服务器请在应用设置里由你自己填写，脚本不写密钥。
set -euo pipefail
if [ -d /Applications/SyncClipboard.app ]; then
  echo "SyncClipboard 已经装着。"
else
  if command -v brew >/dev/null 2>&1; then
    brew install --cask syncclipboard || brew install syncclipboard || true
  fi
  if [ ! -d /Applications/SyncClipboard.app ]; then
    echo "未能自动装上。请到 https://github.com/Jeric-X/SyncClipboard/releases 下载 Mac 版。"
    exit 1
  fi
fi
echo "请打开 SyncClipboard，在设置里填你自己的同步服务。不要把密码写进本仓库。"
echo "若总控板半自动发送会用到剪贴板，请避免其它工具长时间占用剪贴板。"
