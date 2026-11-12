# Allow 8765 only from Tailscale CGNAT. Does not print secrets.
$ErrorActionPreference = "Continue"
$name = "Harness Board Tailscale"
$existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
if ($existing) {
  Set-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Enabled True -Profile Any -ErrorAction SilentlyContinue | Out-Null
  Get-NetFirewallPortFilter -AssociatedNetFirewallRule $existing | Out-Null
} else {
  New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -RemoteAddress 100.64.0.0/10 -Profile Any -ErrorAction SilentlyContinue | Out-Null
}
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Select-Object DisplayName, Enabled, Direction, Action | Format-List
