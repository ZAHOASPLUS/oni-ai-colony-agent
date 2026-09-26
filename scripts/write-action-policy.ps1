param(
    [string]$BridgeDir = "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge",
    [switch]$EnableActions,
    [switch]$DisableSuggestions,
    [string[]]$AllowedSaveHashes = @(),
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
    [string[]]$AllowedActions = @("noop", "focus_note"),
    [ValidateRange(1, 300)]
    [int]$CommandTtlSeconds = 30,
    [ValidateRange(1, 100)]
    [int]$MaxPendingCommands = 20
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $BridgeDir | Out-Null
$Root = (Resolve-Path -LiteralPath $BridgeDir).Path
$Hashes = @($AllowedSaveHashes | Where-Object { $_ } | Sort-Object -Unique)
$Actions = @($AllowedActions | Where-Object { $_ } | Sort-Object -Unique)

foreach ($Hash in $Hashes) {
    if ($Hash -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Invalid save hash. Expected sha256 followed by 64 lowercase hex characters."
    }
}

if ($EnableActions -and $Hashes.Count -eq 0) {
    throw "Enabling actions requires at least one explicit test-save hash."
}

if ($Actions.Count -eq 0) {
    throw "At least one allowed action must be listed."
}

$Policy = [ordered]@{
    schema = "oni.action-policy.v1"
    actions_enabled = [bool]$EnableActions
    suggestions_enabled = -not [bool]$DisableSuggestions
    allowed_save_hashes = $Hashes
    allowed_actions = $Actions
    command_ttl_seconds = $CommandTtlSeconds
    max_pending_commands = $MaxPendingCommands
    generated_at = [DateTimeOffset]::Now.ToString("o")
}

$Target = Join-Path $Root "action-policy.json"
$Temp = Join-Path $Root (".action-policy.{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
$Json = ($Policy | ConvertTo-Json -Depth 10) + [Environment]::NewLine
[IO.File]::WriteAllText($Temp, $Json, [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $Temp -Destination $Target -Force

Write-Host ("Action policy written: enabled={0}, suggestions={1}, saves={2}, actions={3}" -f `
    [bool]$EnableActions,
    (-not [bool]$DisableSuggestions),
    $Hashes.Count,
    ($Actions -join ","))
