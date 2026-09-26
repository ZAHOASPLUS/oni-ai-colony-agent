param(
    [Parameter(Mandatory = $true)]
    [string]$BaselineManifest,
    [Parameter(Mandatory = $true)]
    [string]$DestinationSave,
    [string]$BridgeDir = "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge",
    [switch]$ConfirmRestore
)

$ErrorActionPreference = "Stop"

# Retired (code review 2026-09-26, finding S4): the destination check only
# requires a parent under save_files/cloud_save_files, so a baseline could
# overwrite any original save, and the backup lives in a Bridge directory that
# other tools move or delete. Use scripts/restore-named-test-clone.ps1.
throw "restore-test-save.ps1 is retired: it can overwrite original saves. Use restore-named-test-clone.ps1 with a verified named-clone manifest."

if (-not $ConfirmRestore) {
    throw "Restore is disabled by default. Re-run with -ConfirmRestore after checking the destination test save."
}

$resolvedBridge = (Resolve-Path -LiteralPath $BridgeDir).Path
$baselineRoot = Join-Path $resolvedBridge "test-save-baseline"
$resolvedManifest = (Resolve-Path -LiteralPath $BaselineManifest).Path
if (-not $resolvedManifest.StartsWith($baselineRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Baseline manifest is outside the expected bridge baseline directory."
}

$destinationParent = (Resolve-Path -LiteralPath (Split-Path -Parent $DestinationSave)).Path
$allowedRoots = @(
    (Join-Path $HOME "Documents\Klei\OxygenNotIncluded\save_files"),
    (Join-Path $HOME "Documents\Klei\OxygenNotIncluded\cloud_save_files")
) | ForEach-Object {
    if (Test-Path -LiteralPath $_) {
        (Resolve-Path -LiteralPath $_).Path
    }
}
$destinationAllowed = $false
foreach ($root in $allowedRoots) {
    if ($destinationParent.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $destinationParent.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        $destinationAllowed = $true
        break
    }
}
if (-not $destinationAllowed) {
    throw "Destination save is outside the ONI save roots."
}

$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $resolvedManifest | ConvertFrom-Json
$baselineFile = Join-Path (Split-Path -Parent $resolvedManifest) $manifest.file_name
$baselineHash = Get-FileHash -Algorithm SHA256 -LiteralPath $baselineFile
$expectedHash = $manifest.save_id_hash -replace '^sha256:', ''
if (-not $baselineHash.Hash.Equals($expectedHash, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Baseline save hash does not match its manifest."
}

if (Test-Path -LiteralPath $DestinationSave) {
    $backupDir = Join-Path $resolvedBridge ("restore-backups\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    Copy-Item -LiteralPath $DestinationSave -Destination (Join-Path $backupDir (Split-Path -Leaf $DestinationSave))
}

Copy-Item -LiteralPath $baselineFile -Destination $DestinationSave -Force
$restoredHash = Get-FileHash -Algorithm SHA256 -LiteralPath $DestinationSave
if (-not $restoredHash.Hash.Equals($expectedHash, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Restored save hash verification failed."
}

Write-Host "Restored and verified test save: $DestinationSave"
