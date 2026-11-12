# 尝试安装 SyncClipboard。账号请在应用里填，脚本不写密钥。
$ErrorActionPreference = 'Continue'
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Jeric-X.SyncClipboard -e --accept-package-agreements --accept-source-agreements
  if ($LASTEXITCODE -ne 0) {
    Write-Host 'winget 没有直接装上。请到 https://github.com/Jeric-X/SyncClipboard/releases 下载 Windows 版。'
  }
} else {
  Write-Host '请到 https://github.com/Jeric-X/SyncClipboard/releases 下载 Windows 版。'
}
Write-Host '打开 SyncClipboard 后，在设置里填你自己的同步服务。不要把密码写进本仓库。'
