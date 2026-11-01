<#
.SYNOPSIS
    Enable or disable a Windows Firewall inbound rule for a TCP port.

.DESCRIPTION
    Manages the inbound firewall rule that lets GitHub reach the webhook
    receiver. Run as Administrator. The rule is idempotent: enabling twice
    updates the existing rule instead of creating a second one.

.PARAMETER Port
    TCP port to open. Default comes from CITRACKER_WEBHOOK_PORT in the
    project's .env file (falling back to 18080).

.PARAMETER Action
    Enable (default) or Disable.

.PARAMETER Remove
    Delete the rule entirely instead of disabling it.

.EXAMPLE
    pwsh scripts/firewall.ps1
    pwsh scripts/firewall.ps1 -Action Disable
    pwsh scripts/firewall.ps1 -Port 35532 -Action Enable
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [int]$Port = 0,
    [ValidateSet('Enable', 'Disable')]
    [string]$Action = 'Enable',
    [switch]$Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RuleName = "CI Tracker Webhook (TCP {0})"

function Get-ConfiguredPort {
    <#
      Reads CITRACKER_WEBHOOK_PORT from the project .env so the script and the
      service agree on the port without a hard-coded constant.
    #>
    $root = Split-Path -Parent $PSScriptRoot
    foreach ($name in @('.env', '.env.example')) {
        $path = Join-Path $root $name
        if (Test-Path $path) {
            foreach ($line in Get-Content $path) {
                if ($line -match '^\s*CITRACKER_WEBHOOK_PORT\s*=\s*(\d+)') {
                    return [int]$Matches[1]
                }
            }
        }
    }
    return 18080
}

if ($Port -le 0) { $Port = Get-ConfiguredPort }
$DisplayName = $RuleName -f $Port

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must run as Administrator."
    exit 1
}

$existing = Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue

if ($Remove) {
    if ($existing) {
        if ($PSCmdlet.ShouldProcess($DisplayName, 'Remove firewall rule')) {
            $existing | Remove-NetFirewallRule
            Write-Host "Removed rule: $DisplayName"
        }
    }
    else {
        Write-Host "No rule named '$DisplayName' - nothing to remove."
    }
    return
}

if ($existing) {
    $pf = $existing | Get-NetFirewallPortFilter
    if ("$($pf.LocalPort)" -ne "$Port") {
        Write-Warning "Rule exists but its port filter is '$($pf.LocalPort)' (expected $Port). Recreating."
        $existing | Remove-NetFirewallRule
        $existing = $null
    }
}

if (-not $existing) {
    if ($PSCmdlet.ShouldProcess($Port, 'Create firewall rule')) {
        New-NetFirewallRule -DisplayName $DisplayName `
            -Description "Allow GitHub webhook deliveries to the CI Tracker receiver." `
            -Direction Inbound -Protocol TCP -LocalPort $Port `
            -Action Allow -Profile Any | Out-Null
        Write-Host "Created rule: $DisplayName"
        $existing = Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue
    }
}

if ($existing) {
    $enable = ($Action -eq 'Enable')
    if ($enable) {
        $existing | Enable-NetFirewallRule
    }
    else {
        $existing | Disable-NetFirewallRule
    }
    Write-Host ("Rule '{0}' is now {1}." -f $DisplayName, $(if ($enable) { 'ENABLED' } else { 'DISABLED' }))
}

Write-Host ""
Write-Host "Current state:"
Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue |
    Select-Object DisplayName, Enabled, Direction, Action, Profile |
    Format-List
