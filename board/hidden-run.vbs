' 隐藏启动 Harness 的 ps1，不弹任何窗口：wscript.exe //B hidden-run.vbs <脚本全路径>
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count > 0 Then
  sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & WScript.Arguments(0) & """", 0, False
End If
