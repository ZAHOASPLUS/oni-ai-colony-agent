param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,
    [string]$BridgeDir = "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge",
    [switch]$EnableActions,
    [ValidateSet(
        "noop",
        "focus_note",
        "set_paused",
        "set_research",
        "set_duplicant_chore_priority",
        "mark_dig_cells",
        "cancel_dig_cells",
        "place_building_blueprint",
        "cancel_building_blueprint",
        "place_utility_blueprint_path",
        "cancel_utility_blueprint_path"
    )]
    [string[]]$AllowedActions = @()
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ResolvedBridge = (Resolve-Path -LiteralPath $BridgeDir).Path
$ResolvedManifest = (Resolve-Path -LiteralPath $Manifest).Path
$Lineage = Get-Content -LiteralPath $ResolvedManifest -Encoding UTF8 -Raw | ConvertFrom-Json

if ($Lineage.schema -cne "oni.test-save-lineage.v1" -or $Lineage.status -cne "active") {
    throw "The save-lineage manifest is not active."
}
if (-not $Lineage.latest -or $Lineage.latest.save_id_hash -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "The save-lineage manifest has no valid current save hash."
}

$LatestStatePath = Join-Path $ResolvedBridge "latest_state.json"
$State = Get-Content -LiteralPath $LatestStatePath -Encoding UTF8 -Raw | ConvertFrom-Json
if ($State.session_id -cne $Lineage.latest.session_id -or
    $State.save_id_hash -cne $Lineage.latest.save_id_hash -or
    [int]$State.world_id -ne [int]$Lineage.latest.world_id) {
    throw "Bridge state does not match the active save-lineage tip."
}
if ($State.save_dirty -ne $false -or ($State.bridge -and $State.bridge.stale -eq $true)) {
    throw "Bridge state is dirty or stale."
}

$ActiveQueueCounts = @{}
foreach ($QueueName in @("pending", "approving", "approved", "executing", "confirming")) {
    $ActiveQueueCounts[$QueueName] = @(
        Get-ChildItem -LiteralPath (Join-Path $ResolvedBridge ("commands\{0}" -f $QueueName)) `
            -Filter "*.json" -File -ErrorAction SilentlyContinue
    ).Count
}
if (@($ActiveQueueCounts.Values | Where-Object { $_ -ne 0 }).Count -ne 0) {
    throw "Pending, approving, approved, executing, and confirming commands must be empty before changing the validation policy."
}

$Writer = Join-Path $PSScriptRoot "write-action-policy.ps1"
if ($EnableActions) {
    if ($AllowedActions.Count -eq 0) {
        throw "-EnableActions requires an explicit -AllowedActions list."
    }
    & $Writer `
        -BridgeDir $ResolvedBridge `
        -EnableActions `
        -DisableSuggestions `
        -AllowedSaveHashes @([string]$Lineage.latest.save_id_hash) `
        -AllowedActions $AllowedActions
} else {
    & $Writer -BridgeDir $ResolvedBridge -DisableSuggestions -AllowedActions @("noop", "focus_note")
}

$Policy = Get-Content -LiteralPath (Join-Path $ResolvedBridge "action-policy.json") -Encoding UTF8 -Raw | ConvertFrom-Json
if ($EnableActions) {
    if ($Policy.actions_enabled -ne $true -or
        $Policy.suggestions_enabled -ne $false -or
        @($Policy.allowed_save_hashes).Count -ne 1 -or
        $Policy.allowed_save_hashes[0] -cne $Lineage.latest.save_id_hash) {
        throw "Enabled policy is not bound to exactly the current checkpoint hash."
    }
} elseif ($Policy.actions_enabled -ne $false -or
    $Policy.suggestions_enabled -ne $false -or
    @($Policy.allowed_save_hashes).Count -ne 0) {
    throw "Disabled policy retained a save hash."
}

Write-Host ("V2.7 validation policy: enabled={0}, session={1}, save={2}" -f `
    [bool]$Policy.actions_enabled,
    $State.session_id,
    $State.save_id_hash)
