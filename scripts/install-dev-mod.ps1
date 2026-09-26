param(
    [string]$Configuration = "Debug",
    [string]$DevModsDir = "$HOME\Documents\Klei\OxygenNotIncluded\mods\Dev",
    [string]$ModFolderName = "oni-ai-colony-agent",
    [string]$ONIManagedDir = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ModDir = Join-Path $ProjectRoot "mod"
$TargetDir = Join-Path $DevModsDir $ModFolderName

$BuildScript = Join-Path $PSScriptRoot "build-mod.ps1"
& $BuildScript -Configuration $Configuration -ONIManagedDir $ONIManagedDir

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

$Files = @(
    "OniAiProbeMod.dll",
    "OniAiProbeMod.pdb",
    "mod.yaml",
    "mod_info.yaml"
)

foreach ($File in $Files) {
    $Source = Join-Path $ModDir $File
    if (Test-Path -LiteralPath $Source) {
        Copy-Item -LiteralPath $Source -Destination $TargetDir -Force
    }
}

$BuiltDll = Join-Path $ModDir "OniAiProbeMod.dll"
$InstalledDll = Join-Path $TargetDir "OniAiProbeMod.dll"
if (-not (Test-Path -LiteralPath $BuiltDll -PathType Leaf) -or -not (Test-Path -LiteralPath $InstalledDll -PathType Leaf)) {
    throw "Built or installed mod DLL is missing."
}

$BuiltHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BuiltDll).Hash.ToLowerInvariant()
$InstalledHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstalledDll).Hash.ToLowerInvariant()
if ($BuiltHash -cne $InstalledHash) {
    throw "Installed DLL hash does not match the build artifact."
}

Write-Host "Installed ONI AI Probe dev mod to $TargetDir"
Write-Host "DLL SHA256: $InstalledHash"
