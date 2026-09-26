[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [string]$CloneManifest,
    [Parameter(Mandatory = $true)]
    [string]$SourceSave,
    [Parameter(Mandatory = $true)]
    [string]$TargetSave,
    [switch]$ConfirmRestore,
    [string]$OniUserRoot = "$env:USERPROFILE\Documents\Klei\OxygenNotIncluded",
    [string]$ReportRoot = '',
    [string]$TestRoot = '',
    [string]$ProcessStateFixture = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

function Expand-ShortPathSegments([string]$FullPath) {
    # An 8.3 alias (DOCUME~1) names the same directory as its long name, so
    # string comparisons and path ids must use the long spelling (review
    # section 6).  Only the existing prefix can be expanded; a missing tail
    # is kept verbatim.  Aliases always contain "~".
    if ($FullPath.IndexOf('~') -lt 0) {
        return $FullPath
    }
    $root = [IO.Path]::GetPathRoot($FullPath)
    $parts = $FullPath.Substring($root.Length).Split([char[]]@('\', '/'), [StringSplitOptions]::RemoveEmptyEntries)
    $current = $root
    for ($index = 0; $index -lt $parts.Count; $index++) {
        $found = @()
        if ([IO.Directory]::Exists($current)) {
            # FindFirstFile matches a literal segment against long and short
            # names and returns the long name.
            $found = @([IO.Directory]::GetFileSystemEntries($current, $parts[$index]))
        }
        if ($found.Count -eq 0) {
            return [IO.Path]::Combine($current, [string]::Join('\', $parts[$index..($parts.Count - 1)]))
        }
        $exact = @($found | Where-Object { [IO.Path]::GetFileName($_) -ieq $parts[$index] })
        if ($exact.Count -eq 1) {
            $current = $exact[0]
        } elseif ($found.Count -eq 1) {
            $current = $found[0]
        } else {
            throw 'A path segment is ambiguous.'
        }
    }
    return $current
}

function Get-NormalizedFullPath([string]$LiteralPath) {
    if ([string]::IsNullOrWhiteSpace($LiteralPath)) {
        throw 'A required path was empty.'
    }
    $full = Expand-ShortPathSegments ([IO.Path]::GetFullPath($LiteralPath))
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
    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$' -or
        $Value.EndsWith('.', [StringComparison]::Ordinal)) {
        throw "$Label is not a safe Windows path segment."
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

function Move-ReplaceFile([string]$Source, [string]$Destination) {
    # .NET Core (PowerShell 7) has File.Move(src, dst, overwrite).  Windows
    # PowerShell 5.1 runs on .NET Framework, which lacks it; there File.Replace
    # (Win32 ReplaceFile) swaps an existing destination atomically on the same
    # volume.  Both callers stage the source next to an existing destination.
    $overwriteMove = [IO.File].GetMethods() | Where-Object {
        $_.Name -ceq 'Move' -and
        $_.GetParameters().Count -eq 3 -and
        $_.GetParameters()[2].ParameterType -eq [bool]
    } | Select-Object -First 1
    if ($null -ne $overwriteMove) {
        [void]$overwriteMove.Invoke($null, [object[]]@($Source, $Destination, $true))
        return
    }
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        throw 'Atomic replacement requires an existing destination file.'
    }
    # [NullString]::Value: a PowerShell $null would bind as "" and make
    # ReplaceFile reject an empty backup path.
    [IO.File]::Replace($Source, $Destination, [NullString]::Value, $true)
}

function Get-StableFileState([string]$LiteralPath) {
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if ($item -isnot [IO.FileInfo] -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Restore files must be regular non-reparse files.'
    }
    $item.Refresh()
    $beforeLength = [long]$item.Length
    $beforeWrite = $item.LastWriteTimeUtc
    $hash = Get-Sha256 $item.FullName
    $item.Refresh()
    if ([long]$item.Length -ne $beforeLength -or $item.LastWriteTimeUtc -ne $beforeWrite) {
        throw 'A restore file changed while it was being verified.'
    }
    return [ordered]@{
        path_id = Get-PathId $item.FullName
        file_name_id = Get-TextId $item.Name
        length = $beforeLength
        last_write_utc = $beforeWrite.ToString('o')
        sha256 = $hash
    }
}

function Test-ExactState([string]$LiteralPath, [object]$Expected) {
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        return $false
    }
    $actual = Get-StableFileState $LiteralPath
    return $actual.path_id -ceq [string]$Expected.path_id -and
        $actual.file_name_id -ceq [string]$Expected.file_name_id -and
        [long]$actual.length -eq [long]$Expected.length -and
        $actual.last_write_utc -ceq [string]$Expected.last_write_utc -and
        $actual.sha256 -ceq [string]$Expected.sha256
}

function Test-ExactContentState([string]$LiteralPath, [object]$Expected) {
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        return $false
    }
    $actual = Get-StableFileState $LiteralPath
    return [long]$actual.length -eq [long]$Expected.length -and
        $actual.last_write_utc -ceq [string]$Expected.last_write_utc -and
        $actual.sha256 -ceq [string]$Expected.sha256
}

function Assert-CanonicalHash([object]$Value, [string]$Label) {
    if ([string]$Value -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "$Label is not a canonical SHA-256 value."
    }
}

function Assert-ManifestFileState([object]$State, [string]$Label) {
    if ($null -eq $State) {
        throw "$Label is missing."
    }
    foreach ($name in @('path_id', 'file_name_id', 'sha256')) {
        Assert-CanonicalHash $State.$name "$Label.$name"
    }
    if ($State.length -is [bool] -or [long]$State.length -lt 0) {
        throw "$Label.length is invalid."
    }
    try {
        [DateTimeOffset]::Parse(
            [string]$State.last_write_utc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ) | Out-Null
    } catch {
        throw "$Label.last_write_utc is invalid."
    }
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

if (-not $ConfirmRestore) {
    throw 'Restore is disabled by default. Re-run with -ConfirmRestore after verifying the named test clone.'
}
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
        throw 'Live clone restore is restricted to the default ONI root; use -TestRoot for fixtures.'
    }
    Assert-NoReparsePath $resolvedOniRoot $resolvedOniRoot
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

$resolvedManifest = (Resolve-Path -LiteralPath $CloneManifest).Path
if (-not (Test-StrictDescendant $resolvedManifest $resolvedReportRoot)) {
    throw 'Clone manifest must stay inside the selected report root.'
}
Assert-NoReparsePath $resolvedManifest $reportBoundary
$manifestItem = Get-Item -LiteralPath $resolvedManifest -Force
if ($manifestItem -isnot [IO.FileInfo] -or $manifestItem.Length -gt 65536) {
    throw 'Clone manifest must be a small regular file.'
}
$manifestText = Get-Content -Raw -Encoding UTF8 -LiteralPath $resolvedManifest
if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
    $manifest = $manifestText | ConvertFrom-Json -DateKind String
} else {
    $manifest = $manifestText | ConvertFrom-Json
}
if ($manifest.schema -cne 'oni.named-test-clone.v1' -or $manifest.created -ne $true -or
    $manifest.path_disclosure -cne 'identifiers_only') {
    throw 'Clone manifest is not an activated oni.named-test-clone.v1 record.'
}
$runId = [string]$manifest.run_id
Assert-SafeWindowsSegment $runId 'Clone manifest run_id'
$runReportRoot = Join-Path $resolvedReportRoot $runId
$expectedManifest = Join-Path $runReportRoot 'test-clone-manifest.json'
if (-not (Test-SamePath $resolvedManifest $expectedManifest)) {
    throw 'Clone manifest is not at its unique run evidence path.'
}
if ([string]$manifest.oni_root_id -cne (Get-PathId $resolvedOniRoot) -or
    [string]$manifest.report_root_id -cne (Get-PathId $resolvedReportRoot)) {
    throw 'Clone manifest root identities do not match this restore boundary.'
}
Assert-CanonicalHash $manifest.target_stem_id 'target_stem_id'
Assert-ManifestFileState $manifest.source.save 'source.save'
Assert-ManifestFileState $manifest.source.preview 'source.preview'
foreach ($name in @('save_base_path_id', 'directory_path_id', 'save_path_id', 'preview_path_id', 'save_sha256', 'preview_sha256')) {
    Assert-CanonicalHash $manifest.target.$name "target.$name"
}
foreach ($name in @('colony_directory_path_id', 'redirect_colony_directory_path_id')) {
    Assert-CanonicalHash $manifest.source.$name "source.$name"
}
# Review P0-1: only clones created in their own colony folder may be
# restored (and so reused); a manifest from the old same-folder layout is
# refused.
if ([string]$manifest.target.isolation -cne 'own_colony_folder') {
    throw 'Clone manifest does not describe a clone in its own colony folder.'
}
if ([string]$manifest.target.save_sha256 -cne [string]$manifest.source.save.sha256 -or
    [string]$manifest.target.preview_sha256 -cne [string]$manifest.source.preview.sha256) {
    throw 'Clone manifest target hashes do not describe an exact source clone.'
}

$resolvedSource = (Resolve-Path -LiteralPath $SourceSave).Path
$resolvedTarget = (Resolve-Path -LiteralPath $TargetSave).Path
foreach ($path in @($resolvedSource, $resolvedTarget)) {
    if (-not (Test-StrictDescendant $path $resolvedOniRoot)) {
        throw 'Source and target saves must stay inside the ONI user-data root.'
    }
    if ([IO.Path]::GetExtension($path) -cne '.sav') {
        throw 'Source and target must use the exact .sav extension.'
    }
    Assert-NoReparsePath $path $resolvedOniRoot
}
$sourceDirectory = [IO.Path]::GetDirectoryName($resolvedSource)
$targetDirectory = [IO.Path]::GetDirectoryName($resolvedTarget)
$saveBase = [IO.Path]::GetDirectoryName($sourceDirectory)
if ((Test-PathsOverlap $sourceDirectory $targetDirectory) -or
    -not (Test-SamePath ([IO.Path]::GetDirectoryName($targetDirectory)) $saveBase)) {
    throw 'The target must sit in its own colony folder beside the source colony folder.'
}
$allowedRoot = $null
foreach ($relativeRoot in @('save_files', 'cloud_save_files')) {
    $candidateRoot = Join-Path $resolvedOniRoot $relativeRoot
    if (-not (Test-Path -LiteralPath $candidateRoot -PathType Container)) {
        continue
    }
    Assert-NoReparsePath $candidateRoot $resolvedOniRoot
    if ((Test-StrictDescendant $resolvedSource $candidateRoot) -and
        (Test-StrictDescendant $resolvedTarget $candidateRoot)) {
        $allowedRoot = $candidateRoot
        break
    }
}
if ($null -eq $allowedRoot) {
    throw 'Source and target are outside the same allowed ONI save root.'
}
Assert-NoReparsePath $sourceDirectory $allowedRoot
Assert-NoReparsePath $targetDirectory $allowedRoot

$sourcePreview = [IO.Path]::ChangeExtension($resolvedSource, '.png')
$targetPreview = [IO.Path]::ChangeExtension($resolvedTarget, '.png')
foreach ($path in @($sourcePreview, $targetPreview)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'A paired preview is missing.'
    }
    Assert-NoReparsePath $path $allowedRoot
}
$targetStem = [IO.Path]::GetFileNameWithoutExtension($resolvedTarget)
if ((Get-TextId $targetStem) -cne [string]$manifest.target_stem_id -or
    (Get-PathId $targetDirectory) -cne [string]$manifest.target.directory_path_id -or
    (Get-PathId $saveBase) -cne [string]$manifest.target.save_base_path_id -or
    (Get-PathId $sourceDirectory) -cne [string]$manifest.source.colony_directory_path_id -or
    (Get-PathId $targetDirectory) -ceq [string]$manifest.source.redirect_colony_directory_path_id -or
    [IO.Path]::GetFileName($targetDirectory) -cne $targetStem -or
    (Get-PathId $resolvedSource) -cne [string]$manifest.source.save.path_id -or
    (Get-PathId $sourcePreview) -cne [string]$manifest.source.preview.path_id -or
    (Get-PathId $resolvedTarget) -cne [string]$manifest.target.save_path_id -or
    (Get-PathId $targetPreview) -cne [string]$manifest.target.preview_path_id) {
    throw 'Clone manifest identity does not match the requested source and named target pair.'
}
if (-not (Test-ExactState $resolvedSource $manifest.source.save) -or
    -not (Test-ExactState $sourcePreview $manifest.source.preview)) {
    throw 'Immutable clone source no longer matches its manifest exactly.'
}

$processCounts = Get-ProcessCounts $ProcessStateFixture $testMode $resolvedTestRoot
if ($processCounts.oxygen_not_included_count -ne 0) {
    throw 'Oxygen Not Included must be stopped before restoring the named test clone.'
}
if ($processCounts.bridge_count -ne 0) {
    throw 'oni_bridge.py must be stopped before restoring the named test clone.'
}

$backupRoot = Join-Path $runReportRoot 'post-action-clone'
if (Test-Path -LiteralPath $backupRoot) {
    throw 'Post-action clone backup already exists; refusing to overwrite evidence.'
}
$beforeSaveState = Get-StableFileState $resolvedTarget
$beforePreviewState = Get-StableFileState $targetPreview
$manifestHash = Get-Sha256 $resolvedManifest
$restored = $false
$backupSave = Join-Path $backupRoot 'target-before-restore.sav'
$backupPreview = Join-Path $backupRoot 'target-before-restore.png'

if ($PSCmdlet.ShouldProcess($resolvedTarget, 'Back up and atomically restore the named ONI test-clone pair')) {
    $token = [Guid]::NewGuid().ToString('N')
    $stagingBackupRoot = Join-Path $runReportRoot ('.post-action-clone-{0}.tmp' -f $token)
    $restoreSaveTemp = Join-Path $targetDirectory ('.restore-save-{0}.tmp' -f $token)
    $restorePreviewTemp = Join-Path $targetDirectory ('.restore-preview-{0}.tmp' -f $token)
    $restoreResultTemp = ''
    try {
        [IO.File]::Copy($resolvedSource, $restoreSaveTemp, $false)
        [IO.File]::Copy($sourcePreview, $restorePreviewTemp, $false)
        if ((Get-Sha256 $restoreSaveTemp) -cne [string]$manifest.source.save.sha256 -or
            (Get-Sha256 $restorePreviewTemp) -cne [string]$manifest.source.preview.sha256 -or
            -not (Test-ExactState $resolvedSource $manifest.source.save) -or
            -not (Test-ExactState $sourcePreview $manifest.source.preview)) {
            throw 'Restore staging or immutable source verification failed.'
        }

        New-Item -ItemType Directory -Path $stagingBackupRoot | Out-Null
        Assert-NoReparsePath $stagingBackupRoot $reportBoundary
        $stagingBackupSave = Join-Path $stagingBackupRoot 'target-before-restore.sav'
        $stagingBackupPreview = Join-Path $stagingBackupRoot 'target-before-restore.png'
        [IO.File]::Copy($resolvedTarget, $stagingBackupSave, $false)
        [IO.File]::Copy($targetPreview, $stagingBackupPreview, $false)
        if (-not (Test-ExactContentState $stagingBackupSave $beforeSaveState) -or
            -not (Test-ExactContentState $stagingBackupPreview $beforePreviewState)) {
            throw 'Post-action clone evidence backup verification failed.'
        }
        $backupPayload = [ordered]@{
            schema = 'oni.named-test-clone-backup.v1'
            run_id = $runId
            created_at = [DateTimeOffset]::Now.ToString('o')
            manifest_sha256 = $manifestHash
            target_save_path_id = Get-PathId $resolvedTarget
            target_preview_path_id = Get-PathId $targetPreview
            before_save_sha256 = $beforeSaveState.sha256
            before_preview_sha256 = $beforePreviewState.sha256
        }
        $backupManifestPath = Join-Path $stagingBackupRoot 'backup-manifest.json'
        $backupBytes = [Text.Encoding]::UTF8.GetBytes((($backupPayload | ConvertTo-Json -Depth 6) + [Environment]::NewLine))
        $stream = [IO.File]::Open($backupManifestPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $stream.Write($backupBytes, 0, $backupBytes.Length)
            $stream.Flush($true)
        } finally {
            $stream.Dispose()
        }
        if (Test-Path -LiteralPath $backupRoot) {
            throw 'Post-action backup path appeared concurrently.'
        }
        [IO.Directory]::Move($stagingBackupRoot, $backupRoot)

        $restoreStage = 'replace_preview'
        try {
            Move-ReplaceFile $restorePreviewTemp $targetPreview
            $restoreStage = 'replace_save'
            Move-ReplaceFile $restoreSaveTemp $resolvedTarget
            $restoreStage = 'verify_restored_pair'
            if ((Get-Sha256 $resolvedTarget) -cne [string]$manifest.source.save.sha256 -or
                (Get-Sha256 $targetPreview) -cne [string]$manifest.source.preview.sha256) {
                throw 'Named test-clone restore hash verification failed.'
            }

            $restorePayload = [ordered]@{
                schema = 'oni.named-test-clone-restore.v1'
                status = 'restored'
                run_id = $runId
                restored_at = [DateTimeOffset]::Now.ToString('o')
                manifest_sha256 = $manifestHash
                target_save_path_id = Get-PathId $resolvedTarget
                target_preview_path_id = Get-PathId $targetPreview
                before_save_sha256 = $beforeSaveState.sha256
                after_save_sha256 = Get-Sha256 $resolvedTarget
                before_preview_sha256 = $beforePreviewState.sha256
                after_preview_sha256 = Get-Sha256 $targetPreview
                backup_save_sha256 = Get-Sha256 $backupSave
                backup_preview_sha256 = Get-Sha256 $backupPreview
            }
            $restoreStage = 'commit_restore_evidence'
            $restoreResultPath = Join-Path $backupRoot 'restore-result.json'
            $restoreResultTemp = Join-Path $backupRoot ('.restore-result-{0}.tmp' -f $token)
            $resultBytes = [Text.Encoding]::UTF8.GetBytes((($restorePayload | ConvertTo-Json -Depth 6) + [Environment]::NewLine))
            $stream = [IO.File]::Open($restoreResultTemp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $stream.Write($resultBytes, 0, $resultBytes.Length)
                $stream.Flush($true)
            } finally {
                $stream.Dispose()
            }
            [IO.File]::Move($restoreResultTemp, $restoreResultPath)
            $restored = $true
        } catch {
            $failedRestoreStage = $restoreStage
            $rollbackSaveTemp = Join-Path $targetDirectory ('.rollback-save-{0}.tmp' -f $token)
            $rollbackPreviewTemp = Join-Path $targetDirectory ('.rollback-preview-{0}.tmp' -f $token)
            $rollbackStage = 'stage_rollback_pair'
            try {
                [IO.File]::Copy($backupSave, $rollbackSaveTemp, $false)
                [IO.File]::Copy($backupPreview, $rollbackPreviewTemp, $false)
                $rollbackStage = 'replace_rollback_preview'
                Move-ReplaceFile $rollbackPreviewTemp $targetPreview
                $rollbackStage = 'replace_rollback_save'
                Move-ReplaceFile $rollbackSaveTemp $resolvedTarget
                $rollbackStage = 'verify_rollback_pair'
                if (-not (Test-ExactContentState $resolvedTarget $beforeSaveState) -or
                    -not (Test-ExactContentState $targetPreview $beforePreviewState)) {
                    throw 'Rollback content verification failed.'
                }
            } catch {
                throw "Restore failed at $failedRestoreStage and automatic rollback failed at $rollbackStage; manual recovery from post-action-clone is required."
            } finally {
                foreach ($rollbackTemporary in @($rollbackSaveTemp, $rollbackPreviewTemp)) {
                    if (Test-Path -LiteralPath $rollbackTemporary -PathType Leaf) {
                        Remove-Item -LiteralPath $rollbackTemporary -Force
                    }
                }
            }
            throw "Restore failed at $failedRestoreStage; automatic rollback restored and verified the original target pair."
        }
    } finally {
        foreach ($temporary in @($restoreSaveTemp, $restorePreviewTemp, $restoreResultTemp)) {
            if (-not [string]::IsNullOrWhiteSpace($temporary) -and (Test-Path -LiteralPath $temporary -PathType Leaf)) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
        if (Test-Path -LiteralPath $stagingBackupRoot -PathType Container) {
            $stagingChildren = @(Get-ChildItem -LiteralPath $stagingBackupRoot -Force)
            foreach ($child in $stagingChildren) {
                if ($child -is [IO.FileInfo] -and -not ($child.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                    Remove-Item -LiteralPath $child.FullName -Force
                }
            }
            if (@(Get-ChildItem -LiteralPath $stagingBackupRoot -Force).Count -eq 0) {
                Remove-Item -LiteralPath $stagingBackupRoot -Force
            }
        }
    }
}

[ordered]@{
    schema = 'oni.named-test-clone-restore-result.v1'
    status = if ($restored) { 'restored' } else { 'would_restore' }
    restored = $restored
    run_id = $runId
    manifest_sha256 = $manifestHash
    target_save_path_id = Get-PathId $resolvedTarget
    target_preview_path_id = Get-PathId $targetPreview
    before_save_sha256 = $beforeSaveState.sha256
    after_save_sha256 = if ($restored) { Get-Sha256 $resolvedTarget } else { $null }
    before_preview_sha256 = $beforePreviewState.sha256
    after_preview_sha256 = if ($restored) { Get-Sha256 $targetPreview } else { $null }
    backup_relative_path = Join-Path $runId 'post-action-clone'
    backup_save_sha256 = if ($restored) { Get-Sha256 $backupSave } else { $null }
    backup_preview_sha256 = if ($restored) { Get-Sha256 $backupPreview } else { $null }
} | ConvertTo-Json -Depth 6
