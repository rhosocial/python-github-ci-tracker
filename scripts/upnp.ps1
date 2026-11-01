<#
.SYNOPSIS
    Create or remove an UPnP port mapping on the gateway (TP-LINK TL-R5406PE-AC).

.DESCRIPTION
    Manages the WAN -> LAN port mapping through the router's IGD service.

    The script resolves everything it can on its own:
      * gateway IP   -- from the active IPv4 default gateway
      * local IP     -- the address of the interface that owns that gateway
      * duplicates   -- the existing mapping table is listed before any change

    Note on this router: the SOAP query actions (GetGenericPortMappingEntry,
    GetSpecificPortMappingEntry, GetNumPortMappings) return empty values or
    fail outright through the COM interop, so the duplicate check is
    advisory. Adds and deletes, however, do take effect -- confirm the result
    in the router's own UPnP / port-forwarding page.

.PARAMETER ExternalPort
    Port opened on the WAN side. Default from CITRACKER_WEBHOOK_PORT in .env.

.PARAMETER InternalPort
    Port on this machine. Default 18080.

.PARAMETER Protocol
    TCP (default) or UDP.

.PARAMETER Action
    Add (default) or Remove.

.PARAMETER Description
    Label shown in the router's mapping table.

.EXAMPLE
    pwsh scripts/upnp.ps1
    pwsh scripts/upnp.ps1 -ExternalPort 35532 -InternalPort 18080
    pwsh scripts/upnp.ps1 -Action Remove
    pwsh scripts/upnp.ps1 -List
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [int]$ExternalPort = 0,
    [int]$InternalPort = 18080,
    [ValidateSet('TCP', 'UDP')]
    [string]$Protocol = 'TCP',
    [ValidateSet('Add', 'Remove')]
    [string]$Action = 'Add',
    [string]$Description = 'citracker',
    [switch]$List
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

function Get-ConfiguredPort {
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

function Get-GatewayIP {
    <# The IPv4 default gateway of the interface carrying the default route. #>
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric |
        Select-Object -First 1
    if (-not $route) { throw 'No IPv4 default route found' }
    return $route.NextHop
}

function Get-LocalIPFor([string]$GatewayIP) {
    <# Local address on the same subnet as the gateway. #>
    $candidates = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' }
    foreach ($addr in $candidates) {
        $route = Get-NetRoute -DestinationPrefix $GatewayIP -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($route -and $route.IPAddress -eq $addr.IPAddress) { return $addr.IPAddress }
    }
    # Fall back to any 192.168.x address on a connected adapter.
    $fallback = $candidates |
        Where-Object { $_.IPAddress -like '192.168.*' } |
        Sort-Object InterfaceMetric |
        Select-Object -First 1
    if ($fallback) { return $fallback.IPAddress }
    throw 'Could not determine the local LAN IP address'
}

function Get-WanService {
    <#
      Locate the IGD's WANIPConnection service. FindByType on WANConnectionDevice
      is the only path that reliably reaches it; walking down from the IGD root
      does not expose grandchildren through this COM API.
    #>
    $finder = New-Object -ComObject UPnP.UPnPDeviceFinder
    $devices = @($finder.FindByType('urn:schemas-upnp-org:device:WANConnectionDevice:1', 0))
    foreach ($d in $devices) {
        foreach ($s in $d.Services) {
            if ($s.ServiceTypeIdentifier -eq 'urn:schemas-upnp-org:service:WANIPConnection:1') {
                return $s
            }
        }
    }
    throw 'WANIPConnection service not found (is the gateway UPnP enabled?)'
}

function Get-PortMappings($Service) {
    <#
      Best-effort listing of the mapping table. Fields frequently come back
      empty through COM, so entries are reported as-is and never trusted for
      correctness -- only for human comparison.
    #>
    $rows = [System.Collections.Generic.List[object]]::new()
    for ($i = 0; $i -lt 64; $i++) {
        $res = @()
        try {
            $null = $Service.InvokeAction('GetGenericPortMappingEntry', @($i), [ref]$res)
        }
        catch { break }
        if ($res.Count -lt 7) { break }
        $rows.Add([pscustomobject]@{
            Index    = $i
            Protocol = "$($res[2])"
            External = "$($res[1])"
            Internal = "$($res[3])"
            Host     = "$($res[4])"
            Enabled  = "$($res[5])"
            Desc     = "$($res[6])"
        })
    }
    return @($rows)
}

function Find-Duplicate($Service, [int]$Port, [string]$Proto) {
    <#
      Reports whether a mapping for this external port already exists. Returns
      the matching row, or $null. Because the read-back fields are unreliable on
      this router, "not found" here does not prove the mapping is absent.
    #>
    foreach ($row in (@(Get-PortMappings $Service))) {
        if ("$($row.External)" -eq "$Port" -and "$($row.Protocol)" -eq $Proto) {
            return $row
        }
    }
    return $null
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

if ($ExternalPort -le 0) { $ExternalPort = Get-ConfiguredPort }
if ($InternalPort -le 0) { $InternalPort = Get-ConfiguredPort }

$Gateway = Get-GatewayIP
$LocalIP = Get-LocalIPFor $Gateway
$Svc = Get-WanService

Write-Host "Gateway      : $Gateway"
Write-Host "Local IP     : $LocalIP"
Write-Host "Mapping      : $Protocol $ExternalPort -> $LocalIP`:$InternalPort ('$Description')"
Write-Host ""

if ($List) {
    Write-Host "Current mapping table (empty fields are a limitation of this router):"
    $mappings = @(Get-PortMappings $Svc)
    if ($mappings.Count -eq 0) {
        Write-Host "  (none readable)"
    }
    else {
        $mappings | Format-Table Index, Protocol, External, Internal, Host, Enabled, Desc -AutoSize
    }
    return
}

Write-Host "Existing mapping table:"
$mappings = @(Get-PortMappings $Svc)
if ($mappings.Count -eq 0) {
    Write-Host "  (none readable through UPnP on this router)"
}
else {
    $mappings | Format-Table Index, Protocol, External, Internal, Host, Enabled, Desc -AutoSize
}

$dup = Find-Duplicate $Svc $ExternalPort $Protocol
if ($dup) {
    Write-Host "Duplicate found: index $($dup.Index) -> $($dup.Internal)"
    if ($Action -eq 'Remove') {
        if ($PSCmdlet.ShouldProcess("$Protocol/$ExternalPort", 'Delete port mapping')) {
            $res = @()
            $null = $Svc.InvokeAction('DeletePortMapping', @('', $ExternalPort, $Protocol), [ref]$res)
            Write-Host "Deleted $Protocol/$ExternalPort."
        }
    }
    else {
        Write-Host "Replacing it (delete + add) ..."
        if ($PSCmdlet.ShouldProcess("$Protocol/$ExternalPort", 'Replace port mapping')) {
            $res = @()
            $null = $Svc.InvokeAction('DeletePortMapping', @('', $ExternalPort, $Protocol), [ref]$res)
            $null = $Svc.InvokeAction(
                'AddPortMapping',
                @('', $ExternalPort, $Protocol, $InternalPort, $LocalIP, $true, $Description, 0),
                [ref]$res
            )
            Write-Host "Replaced $Protocol/$ExternalPort -> $LocalIP`:$InternalPort."
        }
    }
}
else {
    if ($Action -eq 'Remove') {
        Write-Host "No readable duplicate; issuing delete anyway."
        if ($PSCmdlet.ShouldProcess("$Protocol/$ExternalPort", 'Delete port mapping')) {
            $res = @()
            $null = $Svc.InvokeAction('DeletePortMapping', @('', $ExternalPort, $Protocol), [ref]$res)
            Write-Host "Delete issued for $Protocol/$ExternalPort."
        }
    }
    else {
        if ($PSCmdlet.ShouldProcess("$Protocol/$ExternalPort", 'Add port mapping')) {
            $res = @()
            $null = $Svc.InvokeAction(
                'AddPortMapping',
                @('', $ExternalPort, $Protocol, $InternalPort, $LocalIP, $true, $Description, 0),
                [ref]$res
            )
            Write-Host "Added $Protocol/$ExternalPort -> $LocalIP`:$InternalPort."
        }
    }
}

Write-Host ""
Write-Host "Mapping table after the change:"
$mappings = @(Get-PortMappings $Svc)
if ($mappings.Count -eq 0) {
    Write-Host "  (none readable through UPnP on this router)"
}
else {
    $mappings | Format-Table Index, Protocol, External, Internal, Host, Enabled, Desc -AutoSize
}

Write-Host ""
Write-Host "Reminder: confirm in the router's own UPnP / port-forwarding page."
Write-Host "          Verify from an external network (a LAN hairpin test proves nothing):"
Write-Host "            Invoke-WebRequest http://<gateway-WAN-ip-or-domain>:$ExternalPort/healthz"

