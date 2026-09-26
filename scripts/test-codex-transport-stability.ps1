[CmdletBinding()]
param(
    [ValidateRange(30, 900)]
    [int]$WindowSeconds = 120
)

$ErrorActionPreference = 'Stop'
$since = (Get-Date).AddSeconds(-$WindowSeconds)
$networkLog = 'Microsoft-Windows-NetworkProfile/Operational'

function Get-EventQueryErrorCategory {
    param(
        [Parameter(Mandatory = $true)]
        [Management.Automation.ErrorRecord]$Record,
        [Parameter(Mandatory = $true)]
        [ValidateSet('log_metadata', 'events')]
        [string]$Stage
    )

    if ($Record.Exception -is [UnauthorizedAccessException] -or
        $Record.Exception -is [Security.SecurityException] -or
        [string]$Record.FullyQualifiedErrorId -match '(?i:unauthorized|accessdenied|security)') {
        return 'access_denied'
    }
    if ($Stage -eq 'log_metadata' -and
        [string]$Record.FullyQualifiedErrorId -match '(?i:nomatchinglogs|lognotfound|objectnotfound)') {
        return 'log_missing'
    }
    if ($Stage -eq 'log_metadata') {
        return 'log_metadata_query_failed'
    }
    return 'event_query_failed'
}

function Get-ProcessQueryErrorCategory {
    param(
        [Parameter(Mandatory = $true)]
        [Management.Automation.ErrorRecord]$Record
    )

    if ($Record.Exception -is [UnauthorizedAccessException] -or
        $Record.Exception -is [Security.SecurityException] -or
        [string]$Record.FullyQualifiedErrorId -match '(?i:unauthorized|accessdenied|permission|security)') {
        return 'access_denied'
    }
    return 'process_query_failed'
}

$networkEvents = @()
$eventQuery = [ordered]@{
    source       = 'windows_event_log'
    logName      = $networkLog
    succeeded    = $false
    stage        = 'log_metadata'
    category     = 'not_started'
    eventCount   = 0
}

try {
    $logMetadata = Get-WinEvent -ListLog $networkLog -ErrorAction Stop
    if ($null -eq $logMetadata) {
        $eventQuery.category = 'log_missing'
    } elseif (-not ($logMetadata.PSObject.Properties.Name -contains 'IsEnabled')) {
        $eventQuery.category = 'log_metadata_invalid'
    } elseif (-not [bool]$logMetadata.IsEnabled) {
        $eventQuery.category = 'log_disabled'
    } else {
        $eventQuery.stage = 'events'
        try {
            $networkEvents = @(
                Get-WinEvent -FilterHashtable @{
                    LogName   = $networkLog
                    Id        = 10000, 10001
                    StartTime = $since
                } -ErrorAction Stop
            )
            $eventQuery.category = if ($networkEvents.Count -eq 0) { 'ok_no_events' } else { 'ok' }
            $eventQuery.succeeded = $true
        } catch {
            if ([string]$_.FullyQualifiedErrorId -match '^(?i:NoMatchingEventsFound)') {
                $networkEvents = @()
                $eventQuery.category = 'ok_no_events'
                $eventQuery.succeeded = $true
            } else {
                $eventQuery.category = Get-EventQueryErrorCategory -Record $_ -Stage 'events'
            }
        }
    }
} catch {
    $eventQuery.category = Get-EventQueryErrorCategory -Record $_ -Stage 'log_metadata'
}
$eventQuery.eventCount = $networkEvents.Count
$disconnects = @($networkEvents | Where-Object Id -eq 10001)
$connections = @($networkEvents | Where-Object Id -eq 10000)

$transportProcessNames = @(
    'clash-verge',
    'verge-mihomo',
    'mihomo-tag'
)
$freshTransportProcesses = @()
$processQuery = [ordered]@{
    source       = 'windows_process_table'
    succeeded    = $false
    stage        = 'helper_enumeration'
    category     = 'not_started'
    processCount = 0
}
try {
    $allProcesses = @(Get-Process -ErrorAction Stop)
    $processQuery.processCount = $allProcesses.Count
    $freshTransportProcesses = @(
        $allProcesses |
        Where-Object {
            $_.ProcessName -in $transportProcessNames -and
            $_.StartTime -ge $since
        } |
        Select-Object ProcessName, Id, StartTime
    )
    $processQuery.succeeded = $true
    $processQuery.category = if ($allProcesses.Count -eq 0) { 'ok_no_processes' } else { 'ok' }
} catch {
    $processQuery.category = Get-ProcessQueryErrorCategory -Record $_
}

# Codex/TCP fields are diagnostics only.  Their best-effort probes cannot
# upgrade or downgrade the independently fail-closed helper-process query.
$codexProcessIds = @(
    Get-Process -Name codex -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Id
)
$established443Count = 0
if ($codexProcessIds.Count -gt 0) {
    $established443Count = @(
        Get-NetTCPConnection -State Established -RemotePort 443 -ErrorAction SilentlyContinue |
            Where-Object OwningProcess -in $codexProcessIds
    ).Count
}

$stable = [bool]$eventQuery.succeeded -and
    [bool]$processQuery.succeeded -and
    $disconnects.Count -eq 0 -and
    $freshTransportProcesses.Count -eq 0
$result = [ordered]@{
    stable                       = $stable
    sampledAtUtc                 = (Get-Date).ToUniversalTime().ToString('o')
    windowSeconds                = $WindowSeconds
    eventQuery                   = $eventQuery
    processQuery                 = $processQuery
    networkDisconnectCount       = $disconnects.Count
    lastDisconnectUtc            = if ($disconnects.Count) {
        ($disconnects | Sort-Object TimeCreated | Select-Object -Last 1).TimeCreated.ToUniversalTime().ToString('o')
    } else { $null }
    networkConnectCount          = $connections.Count
    freshTransportProcessCount   = $freshTransportProcesses.Count
    freshTransportProcesses      = @(
        $freshTransportProcesses | ForEach-Object {
            [ordered]@{
                processName = $_.ProcessName
                processId   = $_.Id
                startTimeUtc = $_.StartTime.ToUniversalTime().ToString('o')
            }
        }
    )
    codexProcessCount            = $codexProcessIds.Count
    codexEstablished443Count     = $established443Count
    recommendation               = if ($stable) {
        'Transport window is stable enough to dispatch a Codex turn.'
    } elseif (-not $eventQuery.succeeded) {
        'Do not dispatch. The NetworkProfile event query did not complete successfully.'
    } elseif (-not $processQuery.succeeded) {
        'Do not dispatch. The helper-process query did not complete successfully.'
    } else {
        'Do not dispatch. Wait for a clean window, then run this check again.'
    }
}

$result | ConvertTo-Json -Depth 5
if (-not $stable) { exit 2 }
