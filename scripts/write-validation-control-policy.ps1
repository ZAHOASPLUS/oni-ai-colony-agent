param(
    [string]$BridgeDir = "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge",
    [switch]$EnableControls,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    [string]$TestRunId,
    [ValidateSet("save_checkpoint", "reload_checkpoint")]
    [string[]]$AllowedOperations = @(),
    [string[]]$AllowedSaveHashes = @(),
    [string[]]$AllowedSavePathIds = @(),
    [string[]]$AllowedTargetSavePathIds = @(),
    [ValidateRange(1, 120)]
    [int]$TtlSeconds = 60
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $BridgeDir | Out-Null
$Root = (Resolve-Path -LiteralPath $BridgeDir).Path
$Operations = @($AllowedOperations | Where-Object { $_ } | Sort-Object -Unique)
$SaveHashes = @($AllowedSaveHashes | Where-Object { $_ } | Sort-Object -Unique)
$SavePathIds = @($AllowedSavePathIds | Where-Object { $_ } | Sort-Object -Unique)
$TargetPathIds = @($AllowedTargetSavePathIds | Where-Object { $_ } | Sort-Object -Unique)

foreach ($Value in @($SaveHashes + $SavePathIds + $TargetPathIds)) {
    if ($Value -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Invalid SHA-256 identity: $Value"
    }
}

if ($EnableControls -and
    ([string]::IsNullOrWhiteSpace($TestRunId) -or
     $Operations.Count -ne 1 -or
     $SaveHashes.Count -ne 1 -or
     $SavePathIds.Count -ne 1)) {
    throw "Enabling validation controls requires one run id, operation, save hash, and save path id."
}

$Now = [DateTimeOffset]::Now
[object[]]$PolicyOperations = @()
[object[]]$PolicySaveHashes = @()
[object[]]$PolicySavePathIds = @()
[object[]]$PolicyTargetPathIds = @()
if ($EnableControls) {
    $PolicyOperations = [object[]]$Operations
    $PolicySaveHashes = [object[]]$SaveHashes
    $PolicySavePathIds = [object[]]$SavePathIds
    $PolicyTargetPathIds = [object[]]$TargetPathIds
}
$Policy = [ordered]@{
    schema = "oni.validation-control-policy.v1"
    controls_enabled = [bool]$EnableControls
    test_run_id = if ($EnableControls) { $TestRunId } else { $null }
    allowed_operations = $PolicyOperations
    allowed_save_hashes = $PolicySaveHashes
    allowed_save_path_ids = $PolicySavePathIds
    allowed_target_save_path_ids = $PolicyTargetPathIds
    generated_at = $Now.ToString("o")
    expires_at = if ($EnableControls) { $Now.AddSeconds($TtlSeconds).ToString("o") } else { $null }
}

$Target = Join-Path $Root "validation-control-policy.json"
$Temp = Join-Path $Root (".validation-control-policy.{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
$Json = ($Policy | ConvertTo-Json -Depth 10) + [Environment]::NewLine
[IO.File]::WriteAllText($Temp, $Json, [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $Temp -Destination $Target -Force

Write-Host ("Validation control policy written: enabled={0}, operations={1}" -f [bool]$EnableControls, ($Operations -join ","))
