[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceSave,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$')]
    [string]$TargetStem,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    [string]$RunId,
    [string]$OniUserRoot = "$env:USERPROFILE\Documents\Klei\OxygenNotIncluded",
    [string]$ReportRoot = '',
    [string]$TestRoot = '',
    [string]$ProcessStateFixture = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

function Get-NormalizedFullPath([string]$LiteralPath) {
    if ([string]::IsNullOrWhiteSpace($LiteralPath)) {
        throw 'A required path was empty.'
    }
    $full = [IO.Path]::GetFullPath($LiteralPath)
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    }
    return $full
}

function Test-SamePath([string]$Left, [string]$Right) {
    return [string]::Equals(
        (Get-NormalizedFullPath $Left),
        (Get-NormalizedFullPath $Right),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Test-StrictDescendant([string]$Candidate, [string]$BoundaryRoot) {
    $candidatePath = Get-NormalizedFullPath $Candidate
    $rootPath = Get-NormalizedFullPath $BoundaryRoot
    $boundary = $rootPath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    return $candidatePath.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)
}

function Test-PathsOverlap([string]$Left, [string]$Right) {
    return (Test-SamePath $Left $Right) -or
        (Test-StrictDescendant $Left $Right) -or
        (Test-StrictDescendant $Right $Left)
}

function Assert-NoReparsePath([string]$LiteralPath, [string]$BoundaryRoot) {
    $path = Get-NormalizedFullPath $LiteralPath
    $root = Get-NormalizedFullPath $BoundaryRoot
    if (-not (Test-SamePath $path $root) -and -not (Test-StrictDescendant $path $root)) {
        throw 'Path escaped its declared safety boundary.'
    }
    if (-not (Test-Path -LiteralPath $root)) {
        throw 'The safety boundary root does not exist.'
    }
    $current = $root
    if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'The safety boundary root cannot be a reparse point.'
    }
    if (Test-SamePath $path $root) {
        return
    }
    $relative = $path.Substring(($root.TrimEnd('\', '/') + '\').Length)
    foreach ($part in $relative -split '[\\/]') {
        if ([string]::IsNullOrEmpty($part)) {
            continue
        }
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Reparse points are not allowed inside a safety-bounded path.'
            }
        }
    }
}

function Assert-SafeWindowsSegment([string]$Value, [string]$Label) {
    if ($Value.EndsWith('.', [StringComparison]::Ordinal)) {
        throw "$Label cannot end with a period."
    }
    $baseName = $Value.Split('.')[0]
    if ($baseName -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
        throw "$Label uses a reserved Windows device name."
    }
}

function Convert-BytesToLowerHex([byte[]]$Bytes) {
    return -join @($Bytes | ForEach-Object { $_.ToString('x2') })
}

function Get-TextId([string]$Value) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value.ToLowerInvariant())
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return 'sha256:' + (Convert-BytesToLowerHex $hasher.ComputeHash($bytes))
    } finally {
        $hasher.Dispose()
    }
}

function Get-PathId([string]$LiteralPath) {
    return Get-TextId (Get-NormalizedFullPath $LiteralPath)
}

function Get-Sha256([string]$LiteralPath) {
    $stream = [IO.File]::Open(
        $LiteralPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return 'sha256:' + (Convert-BytesToLowerHex $hasher.ComputeHash($stream))
    } finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
}

function Get-StableFileState([string]$LiteralPath) {
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if ($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Clone source files must be regular non-reparse files.'
    }
    $item.Refresh()
    $beforeLength = [long]$item.Length
    $beforeWrite = $item.LastWriteTimeUtc
    $hash = Get-Sha256 $item.FullName
    $item.Refresh()
    if ([long]$item.Length -ne $beforeLength -or $item.LastWriteTimeUtc -ne $beforeWrite) {
        throw 'A clone source file changed while it was being verified.'
    }
    return [ordered]@{
        path_id = Get-PathId $item.FullName
        file_name_id = Get-TextId $item.Name
        length = $beforeLength
        last_write_utc = $beforeWrite.ToString('o')
        sha256 = $hash
    }
}

function Test-FileStateMatches([string]$LiteralPath, [object]$Expected) {
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        return $false
    }
    $actual = Get-StableFileState $LiteralPath
    return $actual.path_id -ceq $Expected.path_id -and
        $actual.file_name_id -ceq $Expected.file_name_id -and
        [long]$actual.length -eq [long]$Expected.length -and
        $actual.last_write_utc -ceq $Expected.last_write_utc -and
        $actual.sha256 -ceq $Expected.sha256
}

function Convert-ToNonNegativeInt([object]$Value, [string]$Label) {
    $integerTypes = @([byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64])
    $isInteger = $false
    foreach ($type in $integerTypes) {
        if ($Value -is $type) {
            $isInteger = $true
            break
        }
    }
    if (-not $isInteger) {
        throw "$Label must be a non-negative integer."
    }
    $converted = [int64]$Value
    if ($converted -lt 0 -or $converted -gt [int]::MaxValue) {
        throw "$Label is outside the supported range."
    }
    return [int]$converted
}

function Get-ProcessCounts([string]$FixturePath, [bool]$IsTestMode, [string]$ResolvedTestRoot) {
    if (-not [string]::IsNullOrWhiteSpace($FixturePath)) {
        if (-not $IsTestMode) {
            throw 'Process-state fixtures are allowed only with -TestRoot.'
        }
        $resolvedFixture = (Resolve-Path -LiteralPath $FixturePath).Path
        if (-not (Test-StrictDescendant $resolvedFixture $ResolvedTestRoot)) {
            throw 'Process-state fixture escaped the test root.'
        }
        Assert-NoReparsePath $resolvedFixture $ResolvedTestRoot
        $fixtureItem = Get-Item -LiteralPath $resolvedFixture -Force
        if ($fixtureItem.Length -gt 4096) {
            throw 'Process-state fixture is unexpectedly large.'
        }
        $fixture = Get-Content -Raw -Encoding UTF8 -LiteralPath $resolvedFixture | ConvertFrom-Json
        if ($fixture.schema -cne 'oni.process-state-fixture.v1') {
            throw 'Unsupported process-state fixture schema.'
        }
        return [ordered]@{
            oxygen_not_included_count = Convert-ToNonNegativeInt $fixture.oxygen_not_included_count 'oxygen_not_included_count'
            bridge_count = Convert-ToNonNegativeInt $fixture.bridge_count 'bridge_count'
        }
    }
    $gameProcesses = @(Get-Process -Name 'OxygenNotIncluded' -ErrorAction SilentlyContinue)
    $bridgeProcesses = @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -match '^(?i:python|pythonw|py)(?:\.exe)?$' -and
                [string]$_.CommandLine -match '(?i)(^|[\s"''\\/])oni_bridge\.py(?=$|[\s"''])'
            }
    )
    return [ordered]@{
        oxygen_not_included_count = $gameProcesses.Count
        bridge_count = $bridgeProcesses.Count
    }
}

Assert-SafeWindowsSegment $RunId 'RunId'
Assert-SafeWindowsSegment $TargetStem 'TargetStem'
if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    throw 'USERPROFILE is required to establish the live ONI root.'
}
$defaultOniRoot = Get-NormalizedFullPath (Join-Path $env:USERPROFILE 'Documents\Klei\OxygenNotIncluded')
$testMode = -not [string]::IsNullOrWhiteSpace($TestRoot)
$resolvedTestRoot = ''
if ($testMode) {
    if (-not $PSBoundParameters.ContainsKey('OniUserRoot')) {
        throw '-OniUserRoot must be explicit when -TestRoot is used.'
    }
    $resolvedTestRoot = (Resolve-Path -LiteralPath $TestRoot).Path
    if (-not (Test-Path -LiteralPath $resolvedTestRoot -PathType Container)) {
        throw 'Test root must be an existing directory.'
    }
    if (Test-SamePath $resolvedTestRoot ([IO.Path]::GetPathRoot($resolvedTestRoot))) {
        throw 'Test root cannot be a filesystem root.'
    }
    if ($resolvedTestRoot.StartsWith('\\', [StringComparison]::Ordinal)) {
        throw 'Test root must be on a local filesystem.'
    }
    Assert-NoReparsePath $resolvedTestRoot $resolvedTestRoot
    if (Test-PathsOverlap $resolvedTestRoot $defaultOniRoot) {
        throw 'Test root cannot overlap the live ONI user-data root.'
    }
    if (Test-PathsOverlap $resolvedTestRoot $projectRoot) {
        throw 'Test root cannot overlap the ONI workspace.'
    }
}

$resolvedOniRoot = (Resolve-Path -LiteralPath $OniUserRoot).Path
if (-not (Test-Path -LiteralPath $resolvedOniRoot -PathType Container)) {
    throw 'ONI user root must be an existing directory.'
}
if ($testMode) {
    if (-not (Test-StrictDescendant $resolvedOniRoot $resolvedTestRoot)) {
        throw 'Test ONI root must be a strict descendant of -TestRoot.'
    }
    Assert-NoReparsePath $resolvedOniRoot $resolvedTestRoot
} else {
    if (-not (Test-SamePath $resolvedOniRoot $defaultOniRoot)) {
        throw 'Live clone creation is restricted to the default ONI root; use -TestRoot for fixtures.'
    }
    Assert-NoReparsePath $resolvedOniRoot $resolvedOniRoot
}

$resolvedSource = (Resolve-Path -LiteralPath $SourceSave).Path
if (-not (Test-StrictDescendant $resolvedSource $resolvedOniRoot)) {
    throw 'Source save must stay inside the ONI user-data root.'
}
if ([IO.Path]::GetExtension($resolvedSource) -cne '.sav') {
    throw 'Source save must use the exact .sav extension.'
}
Assert-NoReparsePath $resolvedSource $resolvedOniRoot
$sourceItem = Get-Item -LiteralPath $resolvedSource -Force
$sourceDirectory = $sourceItem.Directory.FullName
$allowedRoot = $null
foreach ($relativeRoot in @('save_files', 'cloud_save_files')) {
    $candidateRoot = Join-Path $resolvedOniRoot $relativeRoot
    if (-not (Test-Path -LiteralPath $candidateRoot -PathType Container)) {
        continue
    }
    Assert-NoReparsePath $candidateRoot $resolvedOniRoot
    if (Test-StrictDescendant $resolvedSource $candidateRoot) {
        $allowedRoot = $candidateRoot
        break
    }
}
if ($null -eq $allowedRoot) {
    throw 'Source save is outside the allowed ONI save roots.'
}
Assert-NoReparsePath $sourceDirectory $allowedRoot

$sourcePreview = [IO.Path]::ChangeExtension($resolvedSource, '.png')
if (-not (Test-Path -LiteralPath $sourcePreview -PathType Leaf)) {
    throw 'Source save does not have a paired .png preview.'
}
Assert-NoReparsePath $sourcePreview $allowedRoot
$targetSave = Get-NormalizedFullPath (Join-Path $sourceDirectory ($TargetStem + '.sav'))
$targetPreview = Get-NormalizedFullPath (Join-Path $sourceDirectory ($TargetStem + '.png'))
foreach ($target in @($targetSave, $targetPreview)) {
    if (-not (Test-StrictDescendant $target $sourceDirectory)) {
        throw 'Resolved test-clone target escaped the source save directory.'
    }
    if (Test-Path -LiteralPath $target) {
        throw 'A requested test-clone target already exists.'
    }
}

if ([string]::IsNullOrWhiteSpace($ReportRoot)) {
    $ReportRoot = if ($testMode) {
        Join-Path $resolvedTestRoot 'reports\runtime'
    } else {
        Join-Path $projectRoot 'reports\runtime'
    }
}
$resolvedReportRoot = Get-NormalizedFullPath $ReportRoot
$reportBoundary = if ($testMode) { $resolvedTestRoot } else { $projectRoot }
if (-not (Test-StrictDescendant $resolvedReportRoot $reportBoundary)) {
    throw 'Report root must be a strict descendant of its safety boundary.'
}
if (Test-PathsOverlap $resolvedReportRoot $resolvedOniRoot) {
    throw 'Report root cannot overlap the ONI user-data root.'
}
Assert-NoReparsePath $resolvedReportRoot $reportBoundary
$runReportRoot = Join-Path $resolvedReportRoot $RunId
if (Test-Path -LiteralPath $runReportRoot) {
    throw 'Run evidence directory already exists; clone evidence is never overwritten.'
}

$sourceSaveState = Get-StableFileState $resolvedSource
$sourcePreviewState = Get-StableFileState $sourcePreview
$processCounts = Get-ProcessCounts $ProcessStateFixture $testMode $resolvedTestRoot
if ($processCounts.oxygen_not_included_count -ne 0) {
    throw 'Oxygen Not Included must be stopped before creating a named test clone.'
}
if ($processCounts.bridge_count -ne 0) {
    throw 'oni_bridge.py must be stopped before creating a named test clone.'
}

$created = $false
$manifestPath = Join-Path $runReportRoot 'test-clone-manifest.json'
$manifest = [ordered]@{
    schema = 'oni.named-test-clone.v1'
    run_id = $RunId
    created_at = $null
    created = $false
    oni_root_id = Get-PathId $resolvedOniRoot
    report_root_id = Get-PathId $resolvedReportRoot
    path_disclosure = 'identifiers_only'
    target_stem_id = Get-TextId $TargetStem
    source = [ordered]@{
        save = $sourceSaveState
        preview = $sourcePreviewState
    }
    target = [ordered]@{
        directory_path_id = Get-PathId $sourceDirectory
        save_path_id = Get-PathId $targetSave
        preview_path_id = Get-PathId $targetPreview
        save_sha256 = $null
        preview_sha256 = $null
    }
}

if ($PSCmdlet.ShouldProcess($targetSave, 'Create an atomic named ONI test-save clone, paired preview, and immutable manifest')) {
    $token = [Guid]::NewGuid().ToString('N')
    $temporarySave = Join-Path $sourceDirectory ('.clone-save-{0}.tmp' -f $token)
    $temporaryPreview = Join-Path $sourceDirectory ('.clone-preview-{0}.tmp' -f $token)
    $temporaryManifest = ''
    $saveCommitted = $false
    $previewCommitted = $false
    try {
        [IO.File]::Copy($resolvedSource, $temporarySave, $false)
        [IO.File]::Copy($sourcePreview, $temporaryPreview, $false)
        if ((Get-Sha256 $temporarySave) -cne $sourceSaveState.sha256 -or
            (Get-Sha256 $temporaryPreview) -cne $sourcePreviewState.sha256) {
            throw 'Staged clone copies failed hash verification.'
        }
        if (-not (Test-FileStateMatches $resolvedSource $sourceSaveState) -or
            -not (Test-FileStateMatches $sourcePreview $sourcePreviewState)) {
            throw 'Immutable clone source changed before commit.'
        }
        if ((Test-Path -LiteralPath $targetSave) -or (Test-Path -LiteralPath $targetPreview)) {
            throw 'A clone target appeared concurrently; refusing to overwrite it.'
        }
        if (Test-Path -LiteralPath $runReportRoot) {
            throw 'Run evidence appeared concurrently; refusing to overwrite it.'
        }

        [IO.File]::Move($temporaryPreview, $targetPreview)
        $previewCommitted = $true
        [IO.File]::Move($temporarySave, $targetSave)
        $saveCommitted = $true
        if ((Get-Sha256 $targetSave) -cne $sourceSaveState.sha256 -or
            (Get-Sha256 $targetPreview) -cne $sourcePreviewState.sha256) {
            throw 'Committed clone pair failed hash verification.'
        }

        $manifest.created_at = [DateTimeOffset]::Now.ToString('o')
        $manifest.created = $true
        $manifest.target.save_sha256 = Get-Sha256 $targetSave
        $manifest.target.preview_sha256 = Get-Sha256 $targetPreview
        New-Item -ItemType Directory -Path $runReportRoot -Force | Out-Null
        Assert-NoReparsePath $runReportRoot $reportBoundary
        $temporaryManifest = Join-Path $runReportRoot ('.test-clone-{0}.tmp' -f $token)
        $manifestBytes = [Text.Encoding]::UTF8.GetBytes((($manifest | ConvertTo-Json -Depth 10) + [Environment]::NewLine))
        $stream = [IO.File]::Open($temporaryManifest, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $stream.Write($manifestBytes, 0, $manifestBytes.Length)
            $stream.Flush($true)
        } finally {
            $stream.Dispose()
        }
        [IO.File]::Move($temporaryManifest, $manifestPath)
        $created = $true
    } catch {
        $originalError = $_
        $unsafeCleanup = $false
        foreach ($committed in @(
            @($targetSave, $sourceSaveState.sha256, $saveCommitted),
            @($targetPreview, $sourcePreviewState.sha256, $previewCommitted)
        )) {
            if (-not [bool]$committed[2] -or -not (Test-Path -LiteralPath $committed[0] -PathType Leaf)) {
                continue
            }
            if ((Get-Sha256 $committed[0]) -ceq [string]$committed[1]) {
                Remove-Item -LiteralPath $committed[0] -Force
            } else {
                $unsafeCleanup = $true
            }
        }
        if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
            $unsafeCleanup = $true
        }
        if (Test-Path -LiteralPath $runReportRoot -PathType Container) {
            if (@(Get-ChildItem -LiteralPath $runReportRoot -Force).Count -eq 0) {
                Remove-Item -LiteralPath $runReportRoot -Force
            }
        }
        if ($unsafeCleanup) {
            throw 'Clone creation failed and a concurrently changed artifact requires manual review.'
        }
        throw $originalError
    } finally {
        foreach ($temporary in @($temporarySave, $temporaryPreview, $temporaryManifest)) {
            if (-not [string]::IsNullOrWhiteSpace($temporary) -and (Test-Path -LiteralPath $temporary -PathType Leaf)) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
    }
}

[ordered]@{
    schema = 'oni.named-test-clone-result.v1'
    status = if ($created) { 'created' } else { 'would_create' }
    created = $created
    run_id = $RunId
    target_stem_id = $manifest.target_stem_id
    manifest_relative_path = Join-Path $RunId 'test-clone-manifest.json'
    manifest_path_id = Get-PathId $manifestPath
    manifest_sha256 = if ($created) { Get-Sha256 $manifestPath } else { $null }
    source_save_sha256 = $sourceSaveState.sha256
    source_preview_sha256 = $sourcePreviewState.sha256
} | ConvertTo-Json -Depth 8
