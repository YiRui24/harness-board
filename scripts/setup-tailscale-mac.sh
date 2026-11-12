#!/bin/bash
# 安装 Tailscale。登录必须你自己在图形界面或 tailscale up 里完成，脚本不会代登、不会保存账号。
set -euo pipefail
if command -v tailscale >/dev/null 2>&1 || [ -x /Applications/Tailscale.app/Contents/MacOS/tailscale ]; then
  echo "Tailscale 已经装着。"
else
  if ! command -v brew >/dev/null 2>&1; then
    echo "请先安装 Homebrew，或从 https://tailscale.com/download 安装 Tailscale。" >&2
    exit 1
  fi
  brew install --cask tailscale
fi
echo "请打开 Tailscale 并登录到你自己的尾巴网络。装好后在终端执行: tailscale ip -4"
echo "把得到的 100.x.x.x 填到其它电脑 config.json 的 peers.host，不要把账号或密钥写进仓库。"
