param(
    [string]$Configuration = "Debug",
    [string]$ONIManagedDir = "C:\Program Files (x86)\Steam\steamapps\common\OxygenNotIncluded\OxygenNotIncluded_Data\Managed",
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputPath) {
    $OutputPath = Join-Path $ProjectRoot "docs\build-manifest.local.json"
}

function Get-ArtifactInfo {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [ordered]@{
            label = $Label
            present = $false
        }
    }

    $item = Get-Item -LiteralPath $Path
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $Path
    return [ordered]@{
        label = $Label
        present = $true
        file_name = $item.Name
        length = $item.Length
        last_write_time = $item.LastWriteTime.ToString("o")
        file_version = $item.VersionInfo.FileVersion
        sha256 = $hash.Hash
    }
}

$modSource = Get-Content -Raw -Encoding UTF8 (Join-Path $ProjectRoot "mod\Source\Mod.cs")
$versionMatch = [regex]::Match($modSource, 'ModVersion\s*=\s*"([^"]+)"')
$modVersion = if ($versionMatch.Success) { $versionMatch.Groups[1].Value } else { "unknown" }
$gameRoot = Split-Path -Parent (Split-Path -Parent $ONIManagedDir)
$gameExe = Join-Path $gameRoot "OxygenNotIncluded.exe"
$devArtifact = Join-Path $HOME "Documents\Klei\OxygenNotIncluded\mods\Dev\oni-ai-colony-agent\OniAiProbeMod.dll"
$gameBuild = "unknown"
$gameBranch = "unknown"
$ilspy = Get-Command ilspycmd -ErrorAction SilentlyContinue
if ($ilspy) {
    $kleiVersion = & $ilspy.Source -t KleiVersion (Join-Path $ONIManagedDir "Assembly-CSharp.dll") 2>$null | Out-String
    $changeListMatch = [regex]::Match($kleiVersion, 'ChangeList\s*=\s*(\d+)')
    $branchMatch = [regex]::Match($kleiVersion, 'BuildBranch\s*=\s*"([^"]+)"')
    if ($changeListMatch.Success) {
        $gameBuild = "cl-" + $changeListMatch.Groups[1].Value
    }
    if ($branchMatch.Success) {
        $gameBranch = $branchMatch.Groups[1].Value
    }
}

$references = @(
    "0Harmony.dll",
    "Assembly-CSharp.dll",
    "Assembly-CSharp-firstpass.dll",
    "UnityEngine.CoreModule.dll",
    "Newtonsoft.Json.dll"
)

$manifest = [ordered]@{
    schema = "oni.build-manifest.v1"
    generated_at = (Get-Date).ToString("o")
    configuration = $Configuration
    mod_version = $modVersion
    game_build = $gameBuild
    game_branch = $gameBranch
    game = Get-ArtifactInfo -Label "game_executable" -Path $gameExe
    references = @(
        foreach ($name in $references) {
            Get-ArtifactInfo -Label $name -Path (Join-Path $ONIManagedDir $name)
        }
    )
    artifacts = @(
        Get-ArtifactInfo -Label "build_output" -Path (Join-Path $ProjectRoot "mod\bin\$Configuration\OniAiProbeMod.dll")
        Get-ArtifactInfo -Label "project_root_copy" -Path (Join-Path $ProjectRoot "mod\OniAiProbeMod.dll")
        Get-ArtifactInfo -Label "dev_install" -Path $devArtifact
    )
    warning_baseline = "docs/build-warning-baseline.json"
}

$outputDirectory = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
$tempPath = "$OutputPath.tmp"
$manifest | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -LiteralPath $tempPath
Move-Item -LiteralPath $tempPath -Destination $OutputPath -Force
Write-Host "Wrote build manifest to $OutputPath"
