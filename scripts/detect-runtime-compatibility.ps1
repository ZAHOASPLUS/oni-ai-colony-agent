param(
    [string]$OniRoot = "",
    [string]$ManagedDir = "",
    [string]$ProjectRoot = "",
    [string]$Project = "",
    [string]$ExpectedBuild = "",
    [string]$ExpectedAssemblySha256 = "",
    [string]$Output = "",
    [switch]$Strict,
    [switch]$NoExternalTool,
    [switch]$Compact
)

$ErrorActionPreference = "Stop"

$ProjectScript = Join-Path $PSScriptRoot "detect_runtime_compatibility.py"
$Python = Get-Command py -ErrorAction SilentlyContinue
$UsePyLauncher = $true
if (-not $Python) {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    $UsePyLauncher = $false
}
if (-not $Python) {
    throw "Python 3 was not found (install py/python and retry)."
}

$Args = @($ProjectScript)
if ($OniRoot) { $Args += @("--oni-root", $OniRoot) }
if ($ManagedDir) { $Args += @("--managed-dir", $ManagedDir) }
if ($ProjectRoot) { $Args += @("--project-root", $ProjectRoot) }
if ($Project) { $Args += @("--project", $Project) }
if ($ExpectedBuild) { $Args += @("--expected-build", $ExpectedBuild) }
if ($ExpectedAssemblySha256) { $Args += @("--expected-assembly-sha256", $ExpectedAssemblySha256) }
if ($Output) { $Args += @("--output", $Output) }
if ($Strict) { $Args += "--strict" }
if ($NoExternalTool) { $Args += "--no-external-tool" }
if ($Compact) { $Args += "--compact" }

if ($UsePyLauncher) {
    & $Python.Source -3 @Args
} else {
    & $Python.Source @Args
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
