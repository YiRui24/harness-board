#!/bin/bash
# 一键安装 Harness 总控板（Mac）：拷文件、生成配置和口令、写入 LaunchAgent 并启动（不弹窗）。
# 用法：
#   bash scripts/install-mac.sh
#   bash scripts/install-mac.sh --prefix "$HOME/harness" --id macbook --label "我的 Mac"
#   bash scripts/install-mac.sh --setup-env    # 顺带装 Tailscale / Syncthing / SyncClipboard（需你自己登录）
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${HARNESS_PREFIX:-$HOME/harness}"
ID=""; LABEL=""; SETUP_ENV=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;;
    --id) ID="$2"; shift 2;;
    --label) LABEL="$2"; shift 2;;
    --setup-env) SETUP_ENV=1; shift;;
    -h|--help) sed -n '2,8p' "$0"; exit 0;;
    *) echo "未知参数: $1" >&2; exit 1;;
  esac
done

echo "安装到 $PREFIX"
mkdir -p "$PREFIX/board/data" "$PREFIX/board/icons" "$PREFIX/logs"
cp -R "$HERE/board/." "$PREFIX/board/"
if [ -f "$HERE/config.example.json" ] && [ ! -f "$PREFIX/config.json" ]; then
  cp "$HERE/config.example.json" "$PREFIX/config.json"
  HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s || echo mac)"
  ID="${ID:-$HOST}"
  LABEL="${LABEL:-$ID}"
  python3 -c 'import json,sys
p,i,l=sys.argv[1],sys.argv[2],sys.argv[3]
d=json.load(open(p,encoding="utf-8"))
d["self"]=i; d["label"]=l
d["peers"]=[x for x in (d.get("peers") or []) if not str((x or {}).get("host") or "").startswith("<")]
json.dump(d, open(p,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
open(p,"a").write("\n")' "$PREFIX/config.json" "$ID" "$LABEL"
  chmod 600 "$PREFIX/config.json"
  echo "已生成 $PREFIX/config.json（请把同伴的 Tailscale IP 填进 peers）"
fi
python3 "$PREFIX/board/ensure-token.py" "$PREFIX/board-token.txt" >/dev/null
chmod 600 "$PREFIX/board-token.txt"
echo "本机口令已写在 $PREFIX/board-token.txt（不要提交、不要发给外人）"

if [ "$SETUP_ENV" = 1 ]; then
  bash "$HERE/scripts/setup-tailscale-mac.sh" || true
  bash "$HERE/scripts/setup-syncthing-mac.sh" --folder "${HARNESS_HANDOVER:-$HOME/交接盘}" || true
  bash "$HERE/scripts/setup-syncclipboard-mac.sh" || true
fi

bash "$HERE/scripts/autostart-mac.sh" --prefix "$PREFIX"
echo "装好了。本机打开：http://127.0.0.1:8765/"
echo "从其它电脑用 Tailscale 打开时，要用 board-token.txt 里的口令登录。"
echo "接着把其它电脑的 Tailscale IP 写进 $PREFIX/config.json 的 peers，再在那几台同样安装一次。"
