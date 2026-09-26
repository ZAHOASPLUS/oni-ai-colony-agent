param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [string]$ONIManagedDir = "",
    [string]$WarningBaseline = "",
    [switch]$SkipWarningGate
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProjectFile = Join-Path $ProjectRoot "mod\OniAiProbeMod.csproj"
if (-not $WarningBaseline) {
    $WarningBaseline = Join-Path $ProjectRoot "docs\build-warning-baseline.json"
}

function Get-StableBuildArtifact {
    param([Parameter(Mandatory = $true)][string]$Path)

    $Before = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($Before.PSIsContainer -or
        ($Before.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Build artifact is not a regular non-reparse file: $Path"
    }
    $Length = [int64]$Before.Length
    $WriteUtc = $Before.LastWriteTimeUtc
    $Stream = [IO.File]::Open(
        $Before.FullName,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $Digest = ([BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $Hasher.Dispose()
        $Stream.Dispose()
    }
    $After = Get-Item -LiteralPath $Before.FullName -ErrorAction Stop
    if ($After.PSIsContainer -or
        ($After.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        [int64]$After.Length -ne $Length -or
        $After.LastWriteTimeUtc -ne $WriteUtc) {
        throw "Build artifact changed while it was verified: $Path"
    }
    return [pscustomobject]@{
        Length = $Length
        Sha256 = "sha256:$Digest"
    }
}

$WorkspaceIdBytes = [Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant())
$WorkspaceHasher = [Security.Cryptography.SHA256]::Create()
try {
    $WorkspaceId = ([BitConverter]::ToString(
        $WorkspaceHasher.ComputeHash($WorkspaceIdBytes)
    )).Replace('-', '').ToLowerInvariant()
} finally {
    $WorkspaceHasher.Dispose()
}
$BuildMutex = [Threading.Mutex]::new($false, "Local\oni-build-mod-$WorkspaceId")
$BuildMutexOwned = $false
try {
    try {
        $BuildMutexOwned = $BuildMutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $BuildMutexOwned = $true
    }
    if (-not $BuildMutexOwned) {
        throw "Another build-mod invocation already owns this workspace build lock."
    }

    $BuildArgs = @(
        "build",
        $ProjectFile,
        "-c",
        $Configuration,
        "--no-incremental",
        "--nologo",
        "/p:UseSharedCompilation=false"
    )
    if ($ONIManagedDir) {
        $BuildArgs += "/p:ONIManagedDir=$ONIManagedDir"
    }

    $Output = @(& dotnet @BuildArgs 2>&1)
    $ExitCode = $LASTEXITCODE
    $Output | ForEach-Object { Write-Host $_ }
    if ($ExitCode -ne 0) {
        throw "dotnet build failed with exit code $ExitCode"
    }

    if (-not $SkipWarningGate) {
        if (-not (Test-Path -LiteralPath $WarningBaseline)) {
            throw "Build warning baseline not found: $WarningBaseline"
        }

        $Baseline = Get-Content -Raw -Encoding UTF8 -LiteralPath $WarningBaseline | ConvertFrom-Json
        $Allowed = @($Baseline.allowed_warning_codes)
        $Observed = @(
            foreach ($Line in $Output) {
                foreach ($Match in [regex]::Matches([string]$Line, 'warning\s+([A-Za-z]+\d+):')) {
                    $Match.Groups[1].Value.ToUpperInvariant()
                }
            }
        ) | Sort-Object -Unique

        $Unexpected = @($Observed | Where-Object { $_ -notin $Allowed })
        if ($Unexpected.Count -gt 0) {
            throw "Unexpected build warning codes: $($Unexpected -join ', ')"
        }
    }

    $OutputDirectory = Join-Path $ProjectRoot "mod\bin\$Configuration"
    foreach ($ArtifactName in @("OniAiProbeMod.dll", "OniAiProbeMod.pdb")) {
        $RootArtifact = Get-StableBuildArtifact -Path (Join-Path $ProjectRoot "mod\$ArtifactName")
        $OutputArtifact = Get-StableBuildArtifact -Path (Join-Path $OutputDirectory $ArtifactName)
        if ($RootArtifact.Length -ne $OutputArtifact.Length -or
            $RootArtifact.Sha256 -cne $OutputArtifact.Sha256) {
            throw "Root/bin build artifact mismatch: $ArtifactName"
        }
    }
} finally {
    if ($BuildMutexOwned) {
        $BuildMutex.ReleaseMutex()
    }
    $BuildMutex.Dispose()
}
