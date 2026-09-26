param(
    [string]$SourceSave = "",
    [string]$BridgeDir = "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge",
    [switch]$AllowExternalSource
)

$ErrorActionPreference = "Stop"

# Retired together with restore-test-save.ps1 (code review 2026-09-26,
# finding S4). It picks the most recently written .sav by default, which can be
# a test clone's autosave, and stores the baseline inside the Bridge directory.
throw "prepare-test-save.ps1 is retired. Use create-named-test-clone.ps1 to make a verified named test clone."

$saveRoots = @(
    (Join-Path $HOME "Documents\Klei\OxygenNotIncluded\save_files"),
    (Join-Path $HOME "Documents\Klei\OxygenNotIncluded\cloud_save_files")
) | ForEach-Object {
    if (Test-Path -LiteralPath $_) {
        (Resolve-Path -LiteralPath $_).Path
    }
}

if (-not $SourceSave) {
    $candidate = $saveRoots |
        ForEach-Object { Get-ChildItem -LiteralPath $_ -Recurse -File -ErrorAction SilentlyContinue } |
        Where-Object { $_.Extension -eq ".sav" } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $candidate) {
        throw "No save file was found under the local ONI save roots."
    }
    $SourceSave = $candidate.FullName
}

$resolvedSource = (Resolve-Path -LiteralPath $SourceSave).Path
$sourceAllowed = $false
foreach ($root in $saveRoots) {
    if ($resolvedSource.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        $sourceAllowed = $true
        break
    }
}
if (-not $sourceAllowed -and -not $AllowExternalSource) {
    throw "Source save is outside the ONI save roots. Use -AllowExternalSource only for an intentional local test file."
}

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedSource
$item = Get-Item -LiteralPath $resolvedSource
$baselineRoot = Join-Path $BridgeDir "test-save-baseline"
$baselineDir = Join-Path $baselineRoot $hash.Hash
$baselineFile = Join-Path $baselineDir ("save" + $item.Extension)
$manifestPath = Join-Path $baselineDir "manifest.json"

New-Item -ItemType Directory -Force -Path $baselineDir | Out-Null
if (-not (Test-Path -LiteralPath $baselineFile)) {
    Copy-Item -LiteralPath $resolvedSource -Destination $baselineFile
}

$manifest = [ordered]@{
    schema = "oni.test-save-baseline.v1"
    created_at = (Get-Date).ToString("o")
    save_id_hash = "sha256:" + $hash.Hash.ToLowerInvariant()
    file_name = Split-Path -Leaf $baselineFile
    length = $item.Length
    source_last_write_time = $item.LastWriteTime.ToString("o")
    source_scope = if ($sourceAllowed) { "oni_save_root" } else { "external_local_test" }
}
$tempPath = "$manifestPath.tmp"
$manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $tempPath
Move-Item -LiteralPath $tempPath -Destination $manifestPath -Force

Write-Host "Prepared test-save baseline: $manifestPath"
Write-Host "Save hash: $($manifest.save_id_hash)"
