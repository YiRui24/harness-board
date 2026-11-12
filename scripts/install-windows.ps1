# 一键安装 Harness 总控板（Windows）：拷文件、生成配置和口令、写入计划任务（隐藏窗口启动）。
# 用法（在仓库根目录，以当前用户运行即可，不必管理员，除非要写防火墙规则）：
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Prefix $env:USERPROFILE\Harness -Id my-pc -Label "我的电脑"
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -SetupEnv
param(
  [string]$Prefix = $(if ($env:HARNESS_PREFIX) { $env:HARNESS_PREFIX } else { Join-Path $env:USERPROFILE 'Harness' }),
  [string]$Id = '',
  [string]$Label = '',
  [switch]$SetupEnv
)
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Write-Host "安装到 $Prefix"
New-Item -ItemType Directory -Force -Path (Join-Path $Prefix 'board\data'), (Join-Path $Prefix 'board\icons') | Out-Null
Get-ChildItem -Force (Join-Path $Here 'board') | ForEach-Object {
  if ($_.Name -in @('data','__pycache__')) { return }
  $dest = Join-Path $Prefix "board\$($_.Name)"
  if ($_.PSIsContainer) { Copy-Item -Recurse -Force $_.FullName $dest }
  else { Copy-Item -Force $_.FullName $dest }
}
$cfg = Join-Path $Prefix 'config.json'
$ex = Join-Path $Here 'config.example.json'
if (-not (Test-Path $cfg) -and (Test-Path $ex)) {
  Copy-Item $ex $cfg
  if (-not $Id) { $Id = $env:COMPUTERNAME }
  if (-not $Label) { $Label = $Id }
  $d = Get-Content -Raw -Encoding UTF8 $cfg | ConvertFrom-Json
  $d.self = $Id; $d.label = $Label
  if ($d.peers) { $d.peers = @($d.peers | Where-Object { -not ([string]$_.host).StartsWith('<') }) }
  ($d | ConvertTo-Json -Depth 8) | Set-Content -Encoding UTF8 $cfg
  Write-Host "已生成 $cfg（请把同伴的 Tailscale IP 填进 peers）"
}
$token = Join-Path $Prefix 'board-token.txt'
if (Get-Command python -ErrorAction SilentlyContinue) {
  & python (Join-Path $Prefix 'board\ensure-token.py') $token
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  & py -3 (Join-Path $Prefix 'board\ensure-token.py') $token
} else { Write-Error '需要先安装 Python 3'; exit 1 }
Write-Host "本机口令已写在 $token（不要提交、不要发给外人）"

if ($SetupEnv) {
  $ts = Join-Path $Here 'scripts\setup-tailscale-windows.ps1'
  $st = Join-Path $Here 'scripts\setup-syncthing-windows.ps1'
  $sc = Join-Path $Here 'scripts\setup-syncclipboard-windows.ps1'
  if (Test-Path $ts) { & $ts }
  if (Test-Path $st) { & $st -Folder (Join-Path $env:USERPROFILE '交接盘') }
  if (Test-Path $sc) { & $sc }
}

& (Join-Path $Here 'scripts\autostart-windows.ps1') -Prefix $Prefix
$fw = Join-Path $Prefix 'board\firewall-board.ps1'
if (Test-Path $fw) { try { & $fw } catch { Write-Host '防火墙规则没写上（可能需要管理员），可稍后手动运行 board\firewall-board.ps1' } }
Write-Host '装好了。本机打开：http://127.0.0.1:8765/'
Write-Host '从其它电脑用 Tailscale 打开时，要用 board-token.txt 里的口令登录。'
