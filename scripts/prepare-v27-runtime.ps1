param(
    [ValidateSet('Install', 'Abort')]
    [string]$Operation,

    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    [string]$RunId,

    [string]$OniRoot,
    [string]$EvidenceRoot,
    [string]$EnvironmentManifest,
    [ValidatePattern('^sha256:[0-9a-f]{64}$')]
    [string]$ExpectedEnvironmentManifestSha256,
    [string]$ReleaseDll,
    [ValidatePattern('^sha256:[0-9a-f]{64}$')]
    [string]$ExpectedReleaseDllSha256,
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$ExpectedModVersion,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    [string]$ConfirmLiveRunId,

    [switch]$Stage,
    [switch]$RecoverStage,
    [switch]$Apply,
    [switch]$WhatIf,
    [ValidatePattern('^[0-9a-f]{32}$')] [string]$StageNonce,
    [ValidatePattern('^sha256:[0-9a-f]{64}$')] [string]$ExpectedStageSha256,
    [string]$ExpectedTransactionId,
    [string]$ExpectedAbortId,

    [ValidatePattern('^sha256:[0-9a-f]{64}$')]
    [string]$ExpectedInstallResultSha256,
    [string]$PreBoundary,
    [string]$CloneManifest,
    [string]$SourceSave,
    [string]$LoaderSave,
    [string]$LoaderStem,

    # Workspace-only phase one. It builds, prints a four-artifact hash receipt,
    # and exits without invoking the transaction manager. The caller freezes
    # the returned DLL hash before a separate runtime WhatIf/Stage/Apply phase.
    [switch]$BuildRelease,
    [ValidateSet('Debug', 'Release')] [string]$BuildConfiguration = 'Release',
    [string]$ONIManagedDir,

    # TestRoot-only passthroughs. They never default to a live path.
    [string]$TestRoot,
    [string]$ProcessStateFixture,
    [string]$TestFailAt,
    [string]$TestBuildScriptFixture
)

$ErrorActionPreference = 'Stop'

if ($BuildRelease) {
    if ($Stage -or $RecoverStage -or $Apply -or $WhatIf) {
        throw 'BuildRelease is a standalone workspace-only phase.'
    }
    if ($Operation -and $Operation -cne 'Install') {
        throw 'BuildRelease cannot be combined with the Abort operation.'
    }
    if (-not $ReleaseDll) {
        throw 'BuildRelease requires the expected ReleaseDll output path.'
    }
    if ($ExpectedReleaseDllSha256) {
        throw 'BuildRelease computes the DLL hash; freeze it externally for the later WhatIf.'
    }
    $BuildScript = Join-Path $PSScriptRoot 'build-mod.ps1'
    if ($TestBuildScriptFixture) {
        if (-not $TestRoot) {
            throw 'TestBuildScriptFixture requires TestRoot.'
        }
        $ResolvedTestRoot = [IO.Path]::GetFullPath($TestRoot).TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        )
        $ResolvedBuildFixture = [IO.Path]::GetFullPath($TestBuildScriptFixture)
        $ResolvedReleaseDll = [IO.Path]::GetFullPath($ReleaseDll)
        $TestPrefix = $ResolvedTestRoot + [IO.Path]::DirectorySeparatorChar
        if (-not $ResolvedBuildFixture.StartsWith($TestPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not $ResolvedReleaseDll.StartsWith($TestPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'TestRoot build fixture and release output must stay inside TestRoot.'
        }
        $BuildScript = $ResolvedBuildFixture
    }
    if (-not (Test-Path -LiteralPath $BuildScript -PathType Leaf)) {
        throw 'Release build script is missing.'
    }
    # Named hashtable splatting is required here. Array splatting passes the
    # literal switch names positionally to a PowerShell script and can bind
    # Configuration='-Configuration', ONIManagedDir='Release'.
    $BuildArguments = @{
        Configuration = $BuildConfiguration
    }
    if ($ONIManagedDir) {
        $BuildArguments['ONIManagedDir'] = $ONIManagedDir
    }
    $BuildOutput = [System.Collections.Generic.List[string]]::new()
    try {
        # Capture every non-error log stream so successful stdout contains the
        # single compact receipt below and nothing else.
        & $BuildScript @BuildArguments 3>&1 4>&1 5>&1 6>&1 |
            ForEach-Object { [void]$BuildOutput.Add([string]$_) }
        if ($LASTEXITCODE -ne 0) {
            throw "Release build failed with exit code $LASTEXITCODE."
        }
    } catch {
        foreach ($BuildLine in $BuildOutput) {
            [Console]::Error.WriteLine([string]$BuildLine)
        }
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }

    $ReleaseDllItem = Get-Item -LiteralPath $ReleaseDll -ErrorAction Stop
    if ($ReleaseDllItem.Name -cne 'OniAiProbeMod.dll') {
        throw 'ReleaseDll must name OniAiProbeMod.dll.'
    }
    $ArtifactNames = @('OniAiProbeMod.dll', 'OniAiProbeMod.pdb', 'mod.yaml', 'mod_info.yaml')
    $Artifacts = [ordered]@{}
    foreach ($ArtifactName in $ArtifactNames) {
        $ArtifactPath = Join-Path $ReleaseDllItem.DirectoryName $ArtifactName
        $Artifact = Get-Item -LiteralPath $ArtifactPath -ErrorAction Stop
        if ($Artifact.PSIsContainer -or
            ($Artifact.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Release artifact is not a file: $ArtifactName"
        }
        $ArtifactLength = [int64]$Artifact.Length
        $ArtifactWriteUtc = $Artifact.LastWriteTimeUtc
        $ArtifactStream = [IO.File]::Open(
            $Artifact.FullName,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        $Hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $DigestBytes = $Hasher.ComputeHash($ArtifactStream)
        } finally {
            $Hasher.Dispose()
            $ArtifactStream.Dispose()
        }
        $ArtifactAfter = Get-Item -LiteralPath $Artifact.FullName -ErrorAction Stop
        if ($ArtifactAfter.PSIsContainer -or
            ($ArtifactAfter.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            [int64]$ArtifactAfter.Length -ne $ArtifactLength -or
            $ArtifactAfter.LastWriteTimeUtc -ne $ArtifactWriteUtc) {
            throw "Release artifact changed during receipt hashing: $ArtifactName"
        }
        $Digest = ([BitConverter]::ToString($DigestBytes)).Replace('-', '').ToLowerInvariant()
        $Artifacts[$ArtifactName] = [ordered]@{
            length = $ArtifactLength
            sha256 = "sha256:$Digest"
        }
    }
    [ordered]@{
        schema = 'oni.live-runtime-bootstrap-release-build.v1'
        status = 'release_built_for_external_freeze'
        classification = 'static_probe'
        build_configuration = $BuildConfiguration
        release_dll = $ReleaseDllItem.FullName
        expected_release_dll_sha256 = $Artifacts['OniAiProbeMod.dll'].sha256
        artifacts = $Artifacts
        manager_invoked = $false
        runtime_write_count = 0
        game_launched = $false
    } | ConvertTo-Json -Compress -Depth 6
    exit 0
}

$ModeCount = [int][bool]$Stage + [int][bool]$RecoverStage + [int][bool]$Apply + [int][bool]$WhatIf
if ($ModeCount -ne 1) {
    throw 'Select exactly one runtime phase: -Stage, -RecoverStage, -Apply, or -WhatIf.'
}
$RequiredRuntimeArguments = [ordered]@{
    Operation = $Operation
    RunId = $RunId
    OniRoot = $OniRoot
    EvidenceRoot = $EvidenceRoot
    EnvironmentManifest = $EnvironmentManifest
    ExpectedEnvironmentManifestSha256 = $ExpectedEnvironmentManifestSha256
    ReleaseDll = $ReleaseDll
    ExpectedReleaseDllSha256 = $ExpectedReleaseDllSha256
    ExpectedModVersion = $ExpectedModVersion
    ConfirmLiveRunId = $ConfirmLiveRunId
}
foreach ($RequiredArgument in $RequiredRuntimeArguments.GetEnumerator()) {
    if (-not $RequiredArgument.Value) {
        throw "Runtime phase requires $($RequiredArgument.Key)."
    }
}
if ($ConfirmLiveRunId -cne $RunId) {
    throw 'ConfirmLiveRunId must exactly equal RunId.'
}
if (-not $StageNonce) {
    throw 'Every runtime phase requires the caller-frozen StageNonce.'
}

$IsPublishOrApply = [bool]$Stage -or [bool]$RecoverStage -or [bool]$Apply
if ($IsPublishOrApply -and -not $ExpectedStageSha256) {
    throw 'Stage, RecoverStage, and Apply require ExpectedStageSha256 from WhatIf.'
}
if ($Operation -ceq 'Install') {
    if ($ExpectedAbortId -or $ExpectedInstallResultSha256 -or $PreBoundary -or
        $CloneManifest -or $SourceSave -or $LoaderSave -or $LoaderStem) {
        throw 'Abort-only arguments cannot be used for Install.'
    }
    if ($IsPublishOrApply -and -not $ExpectedTransactionId) {
        throw 'Install Stage, RecoverStage, and Apply require ExpectedTransactionId.'
    }
} else {
    if (-not $ExpectedInstallResultSha256 -or -not $PreBoundary -or
        -not $CloneManifest -or -not $SourceSave -or -not $LoaderSave -or
        -not $LoaderStem) {
        throw 'Abort requires the install-result hash and all explicit clone/source bindings.'
    }
    if ($IsPublishOrApply -and -not $ExpectedAbortId) {
        throw 'Abort Stage, RecoverStage, and Apply require ExpectedAbortId.'
    }
    if ($ExpectedTransactionId) {
        throw 'ExpectedTransactionId is Install-only.'
    }
}

$Manager = Join-Path $PSScriptRoot 'manage_live_runtime_bootstrap.py'
if (-not (Test-Path -LiteralPath $Manager -PathType Leaf)) {
    throw 'Runtime bootstrap transaction manager is missing.'
}

$ModeArgument = if ($Stage) {
    '--stage'
} elseif ($RecoverStage) {
    '--recover-stage'
} elseif ($Apply) {
    '--apply'
} else {
    '--what-if'
}
$Arguments = @(
    $Manager,
    '--operation', $Operation.ToLowerInvariant(),
    '--run-id', $RunId,
    '--oni-root', $OniRoot,
    '--evidence-root', $EvidenceRoot,
    '--environment-manifest', $EnvironmentManifest,
    '--expected-environment-manifest-sha256', $ExpectedEnvironmentManifestSha256,
    '--release-dll', $ReleaseDll,
    '--expected-release-dll-sha256', $ExpectedReleaseDllSha256,
    '--expected-mod-version', $ExpectedModVersion,
    '--confirm-live-run-id', $ConfirmLiveRunId,
    $ModeArgument,
    '--stage-nonce', $StageNonce
)
if ($ExpectedStageSha256) {
    $Arguments += @('--expected-stage-sha256', $ExpectedStageSha256)
}
if ($ExpectedTransactionId) {
    $Arguments += @('--expected-transaction-id', $ExpectedTransactionId)
}
if ($ExpectedAbortId) {
    $Arguments += @('--expected-abort-id', $ExpectedAbortId)
}
if ($Operation -ceq 'Abort') {
    $Arguments += @(
        '--expected-install-result-sha256', $ExpectedInstallResultSha256,
        '--pre-boundary', $PreBoundary,
        '--clone-manifest', $CloneManifest,
        '--source-save', $SourceSave,
        '--loader-save', $LoaderSave,
        '--loader-stem', $LoaderStem
    )
}
if ($TestRoot) { $Arguments += @('--test-root', $TestRoot) }
if ($ProcessStateFixture) { $Arguments += @('--process-state-fixture', $ProcessStateFixture) }
if ($TestFailAt) { $Arguments += @('--test-fail-at', $TestFailAt) }

# This sole manager invocation is lexically after the optional release build
# gate. The manager itself neither builds nor launches ONI.
$PythonLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if ($PythonLauncher) {
    & $PythonLauncher.Source -3 @Arguments
} else {
    $PythonLauncher = Get-Command 'python.exe' -ErrorAction Stop
    & $PythonLauncher.Source @Arguments
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
