param(
    [ValidateSet("observer", "batch-a", "batch-b", "batch-c", "final")]
    [string]$Phase = "final",
    [ValidateRange(0, 3600)]
    [double]$MinimumDurationSeconds = 600,
    [string]$BridgeDir = "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge",
    [string]$PlayerLog = "$HOME\AppData\LocalLow\Klei\Oxygen Not Included\Player.log",
    [string]$SaveLineage = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ($Phase -eq "final" -and [string]::IsNullOrWhiteSpace($SaveLineage)) {
    throw "Final runtime verification requires -SaveLineage."
}
$arguments = @(
    "-3",
    (Join-Path $PSScriptRoot "verify_runtime.py"),
    "--bridge-dir", $BridgeDir,
    "collect",
    "--phase", $Phase,
    "--minimum-duration-seconds", $MinimumDurationSeconds,
    "--player-log", $PlayerLog
)
if (-not [string]::IsNullOrWhiteSpace($SaveLineage)) {
    $arguments += @("--save-lineage", $SaveLineage)
}
& py @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Runtime verification did not pass. See reports/runtime-acceptance.json."
}
