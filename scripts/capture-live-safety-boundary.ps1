[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    [string]$RunId,
    [Parameter(Mandatory = $true)]
    [ValidateSet('pre', 'post', 'checkpoint')]
    [string]$Phase,
    [string]$OniUserRoot = "$env:USERPROFILE\Documents\Klei\OxygenNotIncluded",
    [string]$ReportRoot = '',
    [string]$TestRoot = '',
    [string]$ProcessStateFixture = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

if (-not ('OniBoundaryReceipt.NativeMethods' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace OniBoundaryReceipt {
    [StructLayout(LayoutKind.Sequential)]
    public struct ByHandleFileInformation {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    public static class NativeMethods {
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetFileInformationByHandle(
            SafeFileHandle handle,
            out ByHandleFileInformation information
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern SafeFileHandle CreateFileW(
            string fileName,
            uint desiredAccess,
            uint shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile
        );
    }
}
'@
}

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

    $current = $root
    if (-not (Test-Path -LiteralPath $current)) {
        throw 'The safety boundary root does not exist.'
    }
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

function Get-BytesSha256([byte[]]$Bytes) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return 'sha256:' + (Convert-BytesToLowerHex $hasher.ComputeHash($Bytes))
    } finally {
        $hasher.Dispose()
    }
}

function Get-HandleIdentity([Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle) {
    $information = [OniBoundaryReceipt.ByHandleFileInformation]::new()
    if (-not [OniBoundaryReceipt.NativeMethods]::GetFileInformationByHandle(
        $Handle,
        [ref]$information
    )) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "Could not read native file identity (Win32 $errorCode)."
    }
    $fileIndex = ([uint64]$information.FileIndexHigh -shl 32) -bor [uint64]$information.FileIndexLow
    $fileLength = ([uint64]$information.FileSizeHigh -shl 32) -bor [uint64]$information.FileSizeLow
    return [ordered]@{
        volume_serial_number = [uint64]$information.VolumeSerialNumber
        file_index = [uint64]$fileIndex
        number_of_links = [uint32]$information.NumberOfLinks
        file_length = [uint64]$fileLength
        file_attributes = [uint32]$information.FileAttributes
    }
}

function Get-DirectoryIdentity([string]$LiteralPath) {
    $handle = [OniBoundaryReceipt.NativeMethods]::CreateFileW(
        (Get-NormalizedFullPath $LiteralPath),
        [uint32]0,
        [uint32]7,
        [IntPtr]::Zero,
        [uint32]3,
        [uint32]0x02000000,
        [IntPtr]::Zero
    )
    if ($handle.IsInvalid) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        $handle.Dispose()
        throw "Could not open report parent identity (Win32 $errorCode)."
    }
    try {
        $identity = Get-HandleIdentity $handle
        if (-not ([uint32]$identity.file_attributes -band [uint32][IO.FileAttributes]::Directory)) {
            throw 'The report parent identity is not a directory.'
        }
        if ([uint32]$identity.file_attributes -band [uint32][IO.FileAttributes]::ReparsePoint) {
            throw 'The report parent identity is a reparse point.'
        }
        return [ordered]@{
            volume_serial_number = [uint64]$identity.volume_serial_number
            file_index = [uint64]$identity.file_index
        }
    } finally {
        $handle.Dispose()
    }
}

function Test-SameNativeIdentity([object]$Left, [object]$Right) {
    return [uint64]$Left.volume_serial_number -eq [uint64]$Right.volume_serial_number -and
        [uint64]$Left.file_index -eq [uint64]$Right.file_index
}

function Get-VerifiedReportPublication([string]$LiteralPath, [byte[]]$ExpectedBytes) {
    $expectedHash = Get-BytesSha256 $ExpectedBytes
    $stream = [IO.File]::Open(
        $LiteralPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $before = Get-HandleIdentity $stream.SafeFileHandle
        if (
            ([uint32]$before.file_attributes -band [uint32][IO.FileAttributes]::Directory) -or
            ([uint32]$before.file_attributes -band [uint32][IO.FileAttributes]::ReparsePoint) -or
            [uint32]$before.number_of_links -ne 1 -or
            [uint64]$before.file_length -ne [uint64]$ExpectedBytes.Length
        ) {
            throw 'The published boundary report is not a regular single-link writer file.'
        }
        $actualHash = 'sha256:' + (Convert-BytesToLowerHex $hasher.ComputeHash($stream))
        $after = Get-HandleIdentity $stream.SafeFileHandle
        if (
            -not (Test-SameNativeIdentity $before $after) -or
            [uint32]$after.number_of_links -ne 1 -or
            [uint64]$after.file_length -ne [uint64]$ExpectedBytes.Length -or
            $actualHash -cne $expectedHash
        ) {
            throw 'The published boundary report differs from the exact writer bytes.'
        }
        return [ordered]@{
            sha256 = $actualHash
            byte_length = [uint64]$ExpectedBytes.Length
            identity = [ordered]@{
                volume_serial_number = [uint64]$after.volume_serial_number
                file_index = [uint64]$after.file_index
                number_of_links = [uint32]$after.number_of_links
            }
        }
    } finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
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

function Get-FileEvidence([IO.FileInfo]$Item, [string]$Scope, [switch]$IncludeStem) {
    if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Evidence files cannot be reparse points.'
    }
    $Item.Refresh()
    $beforeLength = [long]$Item.Length
    $beforeWrite = $Item.LastWriteTimeUtc
    $sha256 = Get-Sha256 $Item.FullName
    $Item.Refresh()
    if ([long]$Item.Length -ne $beforeLength -or $Item.LastWriteTimeUtc -ne $beforeWrite) {
        throw 'A file changed while its safety-boundary evidence was being captured.'
    }

    $entry = [ordered]@{
        scope = $Scope
        path_id = Get-PathId $Item.FullName
        parent_path_id = Get-PathId $Item.Directory.FullName
        file_name_id = Get-TextId $Item.Name
        extension = $Item.Extension.ToLowerInvariant()
        length = $beforeLength
        last_write_utc = $beforeWrite.ToString('o')
        sha256 = $sha256
    }
    if ($IncludeStem) {
        $entry['stem_id'] = Get-TextId ([IO.Path]::GetFileNameWithoutExtension($Item.Name))
    }
    return $entry
}

function Assert-UniquePathIds([object[]]$Entries, [string]$Section) {
    $seen = @{}
    foreach ($entry in $Entries) {
        $pathId = [string]$entry.path_id
        if ($seen.ContainsKey($pathId)) {
            throw "$Section produced a duplicate path identifier."
        }
        $seen[$pathId] = $true
    }
}

function Convert-ToNonNegativeInt([object]$Value, [string]$Label) {
    $integerTypes = @(
        [byte], [sbyte], [int16], [uint16], [int32], [uint32], [int64], [uint64]
    )
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
        throw 'Live capture is restricted to the default ONI user-data root; use -TestRoot for fixtures.'
    }
    Assert-NoReparsePath $resolvedOniRoot $resolvedOniRoot
}

if ([string]::IsNullOrWhiteSpace($ReportRoot)) {
    $ReportRoot = if ($testMode) {
        Join-Path $resolvedTestRoot 'reports\live-boundaries'
    } else {
        Join-Path $projectRoot 'reports\live-boundaries'
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
$target = Join-Path $runReportRoot ("{0}-boundary.json" -f $Phase)
if (Test-Path -LiteralPath $target) {
    throw 'The requested boundary report already exists; evidence is never overwritten.'
}

$saveEvidence = @()
foreach ($scope in @('save_files', 'cloud_save_files')) {
    $saveRoot = Join-Path $resolvedOniRoot $scope
    if (-not (Test-Path -LiteralPath $saveRoot -PathType Container)) {
        continue
    }
    Assert-NoReparsePath $saveRoot $resolvedOniRoot
    foreach ($item in Get-ChildItem -LiteralPath $saveRoot -Recurse -Force -ErrorAction Stop) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Reparse points are not allowed below an ONI save root.'
        }
        if ($item -isnot [IO.FileInfo] -or $item.Extension -notin @('.sav', '.png')) {
            continue
        }
        $saveEvidence += Get-FileEvidence $item $scope -IncludeStem
    }
}
$saveEvidence = @($saveEvidence | Sort-Object scope, path_id)
Assert-UniquePathIds $saveEvidence 'saves'

$configEvidence = @()
$modsRoot = Join-Path $resolvedOniRoot 'mods'
if (Test-Path -LiteralPath $modsRoot -PathType Container) {
    Assert-NoReparsePath $modsRoot $resolvedOniRoot
}
foreach ($candidateSpec in @(
    @('kplayerprefs.yaml', 'player_prefs'),
    @('mods\mods.json', 'mod_config'),
    @('mods\mods.json.bak', 'mod_config'),
    @('mods\config.json', 'mod_config')
)) {
    $candidate = Join-Path $resolvedOniRoot $candidateSpec[0]
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        Assert-NoReparsePath $candidate $resolvedOniRoot
        $configEvidence += Get-FileEvidence (Get-Item -LiteralPath $candidate -Force) $candidateSpec[1]
    }
}
$configEvidence = @($configEvidence | Sort-Object path_id)
Assert-UniquePathIds $configEvidence 'mod_configs'

$probeEvidence = @()
$probeRoot = Join-Path $modsRoot 'Dev\oni-ai-colony-agent'
if (Test-Path -LiteralPath $probeRoot -PathType Container) {
    Assert-NoReparsePath $probeRoot $resolvedOniRoot
    foreach ($item in Get-ChildItem -LiteralPath $probeRoot -File -Force -ErrorAction Stop) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Installed probe evidence cannot contain reparse points.'
        }
        $probeEvidence += Get-FileEvidence $item 'installed_probe'
    }
}
$probeEvidence = @($probeEvidence | Sort-Object path_id)
Assert-UniquePathIds $probeEvidence 'installed_probe'

$bridgeRoot = Join-Path $resolvedOniRoot 'ai-bridge'
if (Test-Path -LiteralPath $bridgeRoot -PathType Container) {
    Assert-NoReparsePath $bridgeRoot $resolvedOniRoot
}
$policyEvidence = @()
foreach ($relative in @('action-policy.json', 'validation-control-policy.json')) {
    $candidate = Join-Path $bridgeRoot $relative
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        Assert-NoReparsePath $candidate $resolvedOniRoot
        $policyEvidence += Get-FileEvidence (Get-Item -LiteralPath $candidate -Force) 'bridge_policy'
    }
}
$policyEvidence = @($policyEvidence | Sort-Object path_id)
Assert-UniquePathIds $policyEvidence 'bridge_policies'

$queueCounts = [ordered]@{}
$temporaryCount = 0
foreach ($stage in @('pending', 'approving', 'approved', 'executing', 'confirming', 'done', 'failed', 'rejected', 'quarantine')) {
    $stageRoot = Join-Path $bridgeRoot (Join-Path 'commands' $stage)
    if (Test-Path -LiteralPath $stageRoot -PathType Container) {
        Assert-NoReparsePath $stageRoot $resolvedOniRoot
        $stageItems = @(Get-ChildItem -LiteralPath $stageRoot -File -Force -ErrorAction Stop)
        foreach ($item in $stageItems) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Command queues cannot contain reparse-point files.'
            }
        }
        $queueCounts[$stage] = @($stageItems | Where-Object { $_.Extension -ieq '.json' }).Count
        $temporaryCount += @($stageItems | Where-Object { $_.Extension -ieq '.tmp' }).Count
    } else {
        $queueCounts[$stage] = 0
    }
}
$queueCounts['temporary_files'] = $temporaryCount
$processCounts = Get-ProcessCounts $ProcessStateFixture $testMode $resolvedTestRoot

$payload = [ordered]@{
    schema = 'oni.live-safety-boundary.v1'
    run_id = $RunId
    phase = $Phase
    captured_at = [DateTimeOffset]::Now.ToString('o')
    oni_root_id = Get-PathId $resolvedOniRoot
    path_disclosure = 'identifiers_only'
    saves = [ordered]@{
        count = $saveEvidence.Count
        entries = $saveEvidence
    }
    mod_configs = [ordered]@{
        count = $configEvidence.Count
        entries = $configEvidence
    }
    installed_probe = [ordered]@{
        count = $probeEvidence.Count
        entries = $probeEvidence
    }
    bridge_policies = [ordered]@{
        count = $policyEvidence.Count
        entries = $policyEvidence
    }
    queues = $queueCounts
    processes = $processCounts
}

New-Item -ItemType Directory -Path $runReportRoot -Force | Out-Null
Assert-NoReparsePath $runReportRoot $reportBoundary
$reportParentIdentityBefore = Get-DirectoryIdentity $runReportRoot
if (Test-Path -LiteralPath $target) {
    throw 'The requested boundary report appeared concurrently; refusing to overwrite it.'
}
$temporary = Join-Path $runReportRoot ('.{0}-{1}.tmp' -f $Phase, [Guid]::NewGuid().ToString('N'))
$jsonBytes = [Text.Encoding]::UTF8.GetBytes((($payload | ConvertTo-Json -Depth 12) + [Environment]::NewLine))
try {
    $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $stream.Write($jsonBytes, 0, $jsonBytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
    [IO.File]::Move($temporary, $target)
} finally {
    if (Test-Path -LiteralPath $temporary -PathType Leaf) {
        Remove-Item -LiteralPath $temporary -Force
    }
}
Assert-NoReparsePath $target $reportBoundary
$verifiedPublication = Get-VerifiedReportPublication $target $jsonBytes
$reportParentIdentityAfter = Get-DirectoryIdentity $runReportRoot
if (-not (Test-SameNativeIdentity $reportParentIdentityBefore $reportParentIdentityAfter)) {
    throw 'The boundary report parent directory changed during publication.'
}

[ordered]@{
    schema = 'oni.live-safety-boundary-capture-receipt.v2'
    status = 'captured'
    report_path_id = Get-PathId $target
    report_relative_path = Join-Path $RunId ([IO.Path]::GetFileName($target))
    report_sha256 = [string]$verifiedPublication.sha256
    report_byte_length = [uint64]$verifiedPublication.byte_length
    report_identity = $verifiedPublication.identity
    report_parent_identity = $reportParentIdentityAfter
    save_count = $saveEvidence.Count
    mod_config_count = $configEvidence.Count
    probe_file_count = $probeEvidence.Count
    bridge_policy_count = $policyEvidence.Count
    queue_nonterminal_count = [int]$queueCounts.pending + [int]$queueCounts.approving + [int]$queueCounts.approved + [int]$queueCounts.executing + [int]$queueCounts.confirming
    oxygen_not_included_count = $processCounts.oxygen_not_included_count
    bridge_count = $processCounts.bridge_count
} | ConvertTo-Json -Depth 5
