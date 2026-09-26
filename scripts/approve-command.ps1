param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^cmd-[A-Za-z0-9_.-]+$')]
    [string]$CommandId,
    [string]$BridgeDir = "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge"
)

$ErrorActionPreference = "Stop"

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $Stream = [IO.File]::OpenRead($LiteralPath)
    try {
        $Hasher = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace("-", "")
        }
        finally {
            $Hasher.Dispose()
        }
    }
    finally {
        $Stream.Dispose()
    }
}

function Write-CreateOnlyUtf8 {
    param([Parameter(Mandatory = $true)][string]$LiteralPath, [Parameter(Mandatory = $true)][string]$Content)
    $Stream = [IO.File]::Open($LiteralPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $Bytes = [Text.UTF8Encoding]::new($false).GetBytes($Content)
        $Stream.Write($Bytes, 0, $Bytes.Length)
        $Stream.Flush($true)
    }
    finally {
        $Stream.Dispose()
    }
}

$Root = (Resolve-Path -LiteralPath $BridgeDir).Path
$PendingDir = Join-Path $Root "commands\pending"
$ApprovedDir = Join-Path $Root "commands\approved"
$Source = Join-Path $PendingDir "$CommandId.json"
$Target = Join-Path $ApprovedDir "$CommandId.json"
$PolicyPath = Join-Path $Root "action-policy.json"
$DecisionFramePath = $null
$ApprovalHelper = Join-Path (Split-Path -Parent $PSScriptRoot) "agent\approval_receipt.py"
$ApprovalCommitHelper = Join-Path $PSScriptRoot "commit_command_approval.py"

if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
    throw "Pending command not found."
}

if (-not (Test-Path -LiteralPath $PolicyPath -PathType Leaf)) {
    throw "Action policy is missing. Actions default to disabled."
}

$Command = Get-Content -Raw -Encoding UTF8 -LiteralPath $Source | ConvertFrom-Json
$Policy = Get-Content -Raw -Encoding UTF8 -LiteralPath $PolicyPath | ConvertFrom-Json

$IsLegacyNonMutating = (
    $Command.schema -ceq "oni.command.v2" -and
    @("noop", "focus_note") -ccontains [string]$Command.action
)
$IsMutatingV3 = (
    $Command.schema -ceq "oni.command.v3" -and
    @("noop", "focus_note") -cnotcontains [string]$Command.action
)
if ($Command.schema -cne "oni.command.v3" -and -not $IsLegacyNonMutating) {
    throw "Only oni.command.v3, or legacy oni.command.v2 noop/focus_note, can be approved."
}
if ($Command.id -cne $CommandId) {
    throw "Command id does not match its file name."
}
if ($Command.status -cne "pending_human_approval") {
    throw "Only pending_human_approval commands can be approved."
}
if ($Command.requires_approval -cne $true) {
    throw "requires_approval must be true."
}
if ([string]::IsNullOrWhiteSpace([string]$Command.session_id)) {
    throw "session_id is required."
}
if ([string]$Command.source_snapshot_id -notlike ("{0}:*" -f $Command.session_id)) {
    throw "source_snapshot_id must belong to command session_id."
}
if ([string]$Command.save_id_hash -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "save_id_hash is invalid."
}
if ([string]$Command.state_hash -notmatch '^sha256:[0-9a-f]{64}$') {
    throw "state_hash is invalid."
}
if ($IsMutatingV3) {
    $FrameIdMatch = [regex]::Match(
        [string]$Command.source_frame_id,
        '^sha256:(?<digest>[0-9a-f]{64})$',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $FrameIdMatch.Success) {
        throw "Mutating oni.command.v3 commands require a valid source_frame_id."
    }
    if ($null -eq $Command.source -or [string]$Command.source.policy_hash -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Mutating oni.command.v3 commands require a valid source.policy_hash binding."
    }
    # The path is derived solely from the validated lower-case digest.  Never
    # approve against the moving root decision-frame.json or an arbitrary path.
    $ArchiveDir = Join-Path $Root "decision-frames"
    $DecisionFramePath = Join-Path $ArchiveDir (
        "sha256-{0}.json" -f $FrameIdMatch.Groups["digest"].Value
    )
}
if ($null -eq $Command.world_id) {
    throw "world_id is required."
}
if ($Command.PSObject.Properties.Name -notcontains "preconditions" -or
    $Command.PSObject.Properties.Name -notcontains "expected_effects") {
    throw "preconditions and expected_effects are required."
}

$ExpiresAt = [DateTimeOffset]::Parse([string]$Command.expires_at, [Globalization.CultureInfo]::InvariantCulture)
if ($ExpiresAt -le [DateTimeOffset]::Now) {
    throw "Command expired before approval."
}

if ($Policy.schema -cne "oni.action-policy.v1" -or $Policy.actions_enabled -cne $true) {
    throw "Action policy is missing, invalid, or disabled."
}
if (@($Policy.allowed_actions) -cnotcontains [string]$Command.action) {
    throw "Command action is not allowed by local policy."
}
if (@($Policy.allowed_save_hashes) -cnotcontains [string]$Command.save_id_hash) {
    throw "Command save hash is not whitelisted by local policy."
}

foreach ($DirectoryName in @("confirming", "done", "failed", "rejected", "quarantine")) {
    $Finalized = Join-Path $Root "commands\$DirectoryName\$CommandId.json"
    if (Test-Path -LiteralPath $Finalized) {
        throw "Command id was already finalized."
    }
}

if (Test-Path -LiteralPath $Target) {
    throw "An approved command with this id already exists."
}

$ApprovedAt = [DateTimeOffset]::UtcNow.ToString("o")
$PythonCommand = Get-Command python -ErrorAction Stop
if (-not (Test-Path -LiteralPath $ApprovalCommitHelper -PathType Leaf)) {
    throw "Atomic approval commit helper is missing."
}
if ($IsMutatingV3 -and -not (Test-Path -LiteralPath $DecisionFramePath -PathType Leaf)) {
    throw "The exact archived decision frame is required for mutating command approval."
}
if ($IsMutatingV3 -and -not (Test-Path -LiteralPath $ApprovalHelper -PathType Leaf)) {
    throw "Approval receipt validator is missing."
}
$BindingPaths = @($Source, $PolicyPath)
if ($IsMutatingV3) {
    $BindingPaths += $DecisionFramePath
}
$BeforeHashes = @{}
foreach ($BindingPath in $BindingPaths) {
    $BeforeHashes[$BindingPath] = Get-Sha256Hex -LiteralPath $BindingPath
}

if ($IsMutatingV3) {
    $ApprovedCommandJson = (& $PythonCommand.Source $ApprovalHelper approve `
        --command $Source `
        --policy $PolicyPath `
        --frame $DecisionFramePath `
        --approved-by "local_human" `
        --approved-at $ApprovedAt | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Decision-frame approval binding was rejected."
    }
    if ([string]::IsNullOrWhiteSpace($ApprovedCommandJson)) {
        throw "Approval receipt validator returned no approved command."
    }
    foreach ($BindingPath in $BindingPaths) {
        $AfterHash = Get-Sha256Hex -LiteralPath $BindingPath
        if ($AfterHash -cne $BeforeHashes[$BindingPath]) {
            throw "Approval input changed while the receipt was being issued. Retry from a fresh frame."
        }
    }
}

if (-not $IsMutatingV3) {
    $Command.status = "approved"
    $Command | Add-Member -NotePropertyName "approved_at" -NotePropertyValue $ApprovedAt -Force
    $Command | Add-Member -NotePropertyName "approved_by" -NotePropertyValue "local_human" -Force
}

$TempDir = $null
$Temp = $null
$TempMarker = $null
$TempToken = [Guid]::NewGuid().ToString("N")
try {
    $TempDir = ([IO.Directory]::CreateTempSubdirectory("oni-approval-")).FullName
    $TempDirInfo = Get-Item -LiteralPath $TempDir -Force -ErrorAction Stop
    if (-not $TempDirInfo.PSIsContainer -or
        (($TempDirInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "Approval temporary directory is a reparse point or not a directory."
    }
    $TempMarker = Join-Path $TempDir (".owner-{0}" -f $TempToken)
    Write-CreateOnlyUtf8 -LiteralPath $TempMarker -Content $TempToken
    $Temp = Join-Path $TempDir ("{0}.json" -f $CommandId)
    if ($IsMutatingV3) {
        $Json = $ApprovedCommandJson + [Environment]::NewLine
    }
    else {
        $Json = ($Command | ConvertTo-Json -Depth 50) + [Environment]::NewLine
    }
    Write-CreateOnlyUtf8 -LiteralPath $Temp -Content $Json
    if ($IsMutatingV3) {
        & $PythonCommand.Source $ApprovalHelper validate `
            --command $Temp `
            --policy $PolicyPath `
            --frame $DecisionFramePath | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Generated approved command failed receipt validation."
        }
        foreach ($BindingPath in $BindingPaths) {
            $FinalHash = Get-Sha256Hex -LiteralPath $BindingPath
            if ($FinalHash -cne $BeforeHashes[$BindingPath]) {
                throw "Approval input changed before commit. Retry from a fresh frame."
            }
        }
    }
    $CommitArguments = @(
        $ApprovalCommitHelper,
        "--command", $Source,
        "--target", $Target,
        "--approved-payload", $Temp,
        "--policy", $PolicyPath,
        "--expected-command-sha256", $BeforeHashes[$Source],
        "--expected-policy-sha256", $BeforeHashes[$PolicyPath]
    )
    if ($IsMutatingV3) {
        $CommitArguments += @(
            "--frame", $DecisionFramePath,
            "--expected-frame-sha256", $BeforeHashes[$DecisionFramePath]
        )
    }
    & $PythonCommand.Source @CommitArguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw (
            "Atomic pending-to-approved transition failed. Treat the outcome as unknown until " +
            "commands\pending, approving, approved, executing, confirming, done, failed, rejected, " +
            "quarantine, and approval-recovery have all been inspected; never replay an absent " +
            "pending command."
        )
    }
}
finally {
    if ($null -ne $TempDir) {
        Write-Warning "Approval temporary evidence was retained at $TempDir; cleanup is intentionally disabled."
    }
}

Write-Host "Approved command $CommandId"
