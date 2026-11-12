# Harness 总控板自启。已在听 8765 则退出（升级请先停旧进程再跑本脚本）。
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $Root "board\server.py"))) {
  # script may live in the Harness root or board folder
  $Root = $PSScriptRoot
}
# If this script is placed at <Harness>/start-board.ps1
if (Test-Path (Join-Path $Root "board\server.py")) { }
elseif (Test-Path (Join-Path (Split-Path $Root -Parent) "board\server.py")) { $Root = Split-Path $Root -Parent }

$env:HARNESS_BOARD_TOKEN_FILE = Join-Path $Root "board-token.txt"
$peers = @((Join-Path $Root "config.json"), (Join-Path $Root "peers.json")) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($peers) {
  try {
    $cfg = Get-Content -Raw -Encoding UTF8 $peers | ConvertFrom-Json
    if ($cfg.self) { $env:HARNESS_DEVICE_ID = [string]$cfg.self }
    if ($cfg.label) { $env:HARNESS_DEVICE_LABEL = [string]$cfg.label }
  } catch {}
}
if (-not $env:HARNESS_DEVICE_LABEL) { $env:HARNESS_DEVICE_LABEL = "Windows" }

$listen = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
  Write-Host "8765 already listening, exit"
  exit 0
}

$cands = @(
  "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
  "C:\Program Files\Python313\pythonw.exe"
)
$py = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) {
  $cmd = Get-Command py -ErrorAction SilentlyContinue
  if ($cmd) { $py = "py"; $pyArgs = @("-3") } else { Write-Error "no python"; exit 1 }
} else { $pyArgs = @() }

# Wait up to ~2 min for Tailscale IPv4; server will also retry bind.
$ts = $null
foreach ($bin in @("$env:ProgramFiles\Tailscale\tailscale.exe", "tailscale")) {
  if (Get-Command $bin -ErrorAction SilentlyContinue) {
    for ($i=0; $i -lt 24; $i++) {
      try { $ts = (& $bin ip -4 2>$null | Select-Object -First 1) } catch {}
      if ($ts -like "100.*") { break }
      Start-Sleep -Seconds 5
    }
    break
  }
}

$log = Join-Path $Root "board\server.log"
$server = Join-Path $Root "board\server.py"
if ($py -eq "py") {
  Start-Process -FilePath "py" -ArgumentList (@("-3", $server, "--port", "8765")) -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $log
} else {
  # prefer pythonw for autostart (no window). Log via python.exe if pythonw chosen we still start pythonw.
  $exe = $py
  Start-Process -FilePath $exe -ArgumentList @($server, "--port", "8765") -WorkingDirectory (Join-Path $Root "board") -WindowStyle Hidden
}
Write-Host "board start requested"
