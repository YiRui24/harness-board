#!/bin/bash
# 本机起三份互为同伴的假数据总控板：127.0.0.1:18765 / 18766 / 18767
cd "$(dirname "$0")"
pkill -f "[m]ock_run.py" 2>/dev/null || true
sleep 0.4
A='{"id":"pc-a","label":"电脑 A","host":"127.0.0.1","port":18765}'
B='{"id":"pc-b","label":"电脑 B","host":"127.0.0.1","port":18766}'
C='{"id":"pc-c","label":"电脑 C","host":"127.0.0.1","port":18767}'
nohup python3 mock_run.py pc-a "电脑 A" 18765 "[$B,$C]" > /tmp/harness-mock-a.log 2>&1 &
nohup python3 mock_run.py pc-b "电脑 B" 18766 "[$A,$C]" > /tmp/harness-mock-b.log 2>&1 &
nohup python3 mock_run.py pc-c "电脑 C" 18767 "[$A,$B]" > /tmp/harness-mock-c.log 2>&1 &
sleep 1
echo "打开 http://127.0.0.1:18765/ 查看（假数据，无真实设备）。"
