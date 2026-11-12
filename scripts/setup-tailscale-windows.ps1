# 尝试用 winget 安装 Tailscale。登录请在官方客户端里完成，脚本不代登、不保存账号。
$ErrorActionPreference = 'Continue'
$bin = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (Test-Path $bin) { Write-Host 'Tailscale 已经装着。'; exit 0 }
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Tailscale.Tailscale -e --accept-package-agreements --accept-source-agreements
} else {
  Write-Host '没有 winget。请到 https://tailscale.com/download 安装。'
  exit 1
}
Write-Host '请打开 Tailscale 并登录。然后执行: & "C:\Program Files\Tailscale\tailscale.exe" ip -4'
Write-Host '把得到的 100.x.x.x 填到其它电脑 config.json 的 peers.host，不要把账号或密钥写进仓库。'
