param([string]$Folder = $(Join-Path $env:USERPROFILE '交接盘'))
$ErrorActionPreference = 'Continue'
if (-not (Get-Command syncthing -ErrorAction SilentlyContinue)) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id Syncthing.Syncthing -e --accept-package-agreements --accept-source-agreements
  } else { Write-Host '请到 https://syncthing.net/downloads 安装 Syncthing。'; exit 1 }
}
New-Item -ItemType Directory -Force -Path (Join-Path $Folder '提示词模板') | Out-Null
Write-Host "交接盘目录：$Folder"
Write-Host '请打开 Syncthing（默认 http://127.0.0.1:8384/），把这个文件夹加进去并与其它电脑共享。'
Write-Host '不要把设备 ID、API 密钥写进仓库。'
