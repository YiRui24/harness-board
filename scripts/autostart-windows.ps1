# 写入计划任务，登录后用 hidden-run.vbs 隐藏启动总控板（不弹黑色窗口）。
param([string]$Prefix = $(if ($env:HARNESS_PREFIX) { $env:HARNESS_PREFIX } else { Join-Path $env:USERPROFILE 'Harness' }))
$ErrorActionPreference = 'Stop'
$taskPath = '\Harness\'
$taskName = 'Board'
$vbs = Join-Path $Prefix 'hidden-run.vbs'
$srcVbs = Join-Path $Prefix 'board\hidden-run.vbs'
if (-not (Test-Path $vbs) -and (Test-Path $srcVbs)) { Copy-Item $srcVbs $vbs }
$starter = Join-Path $Prefix 'start-board.ps1'
Copy-Item -Force (Join-Path $Prefix 'board\start-board.ps1') $starter
$arg = "//B `"$vbs`" `"$starter`""
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $arg -WorkingDirectory $Prefix
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
try { Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction SilentlyContinue } catch {}
Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskPath $taskPath -TaskName $taskName
Write-Host "已写入计划任务 ${taskPath}${taskName} 并启动。停用：Unregister-ScheduledTask -TaskPath '$taskPath' -TaskName $taskName"
