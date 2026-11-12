Windows 自启由 scripts/autostart-windows.ps1 写入计划任务 \Harness\Board，
动作为 wscript.exe //B hidden-run.vbs start-board.ps1，不会弹出黑色窗口。
不要把本机口令、Tailscale 账号或真实 IP 写进这个目录。
