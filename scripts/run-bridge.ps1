<#
.SYNOPSIS
Starts the ONI AI Bridge. ONI-BRIDGE-HELP-SIDE-EFFECT-FREE

.DESCRIPTION
The help path is handled by PowerShell before the script body and never starts
Python or reads/writes a Bridge directory. Unknown parameters fail during
advanced-script parameter binding before the launcher body can execute.
#>
[CmdletBinding()]
param(
    [string]$BridgeDir = "$HOME\Documents\Klei\OxygenNotIncluded\ai-bridge",
    [switch]$Once,
    [switch]$EnableLlm,
    [switch]$ModelLoop,
    [string]$AutonomousConfig = "",
    [string]$SecretsDir = "",
    [string]$Goal = "Keep the colony alive with conservative, reversible actions.",
    [string]$ExpectedValidationRunId = "",
    [int]$ExpectedValidationProcessId = 0,
    [string]$ExpectedStartupBindingSha256 = "",
    [string]$ExpectedStartupIntentHash = "",
    [string]$ExpectedRuntimeConfigEvidenceSha256 = "",
    [string]$ExpectedLoaderPathIdUpper = "",
    [string]$ExpectedLoaderSaveSha256 = "",
    [string]$ExpectedGameBuild = "",
    [string]$ExpectedModVersion = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$BridgeScript = Join-Path $ProjectRoot "bridge\oni_bridge.py"
if ($PSBoundParameters.ContainsKey("ExpectedValidationProcessId") -and $ExpectedValidationProcessId -le 0) {
    throw "ExpectedValidationProcessId must be a positive integer when supplied."
}
$ExpectedValidationProcessIdValue = if ($ExpectedValidationProcessId -gt 0) {
    [string]$ExpectedValidationProcessId
} else {
    ""
}
$StartupExpectationValues = @(
    $ExpectedValidationRunId,
    $ExpectedValidationProcessIdValue,
    $ExpectedStartupBindingSha256,
    $ExpectedStartupIntentHash,
    $ExpectedRuntimeConfigEvidenceSha256,
    $ExpectedLoaderPathIdUpper,
    $ExpectedLoaderSaveSha256,
    $ExpectedGameBuild,
    $ExpectedModVersion
)
$StartupExpectationCount = @($StartupExpectationValues | Where-Object { -not [string]::IsNullOrEmpty($_) }).Count
if ($StartupExpectationCount -ne 0 -and $StartupExpectationCount -ne $StartupExpectationValues.Count) {
    throw "Startup expectation parameters must be supplied as one exact set."
}
if ($AutonomousConfig) {
    if ($Once -or $EnableLlm -or $ModelLoop -or $SecretsDir -or $StartupExpectationCount -ne 0 -or
        $PSBoundParameters.ContainsKey("BridgeDir") -or $PSBoundParameters.ContainsKey("Goal")) {
        throw "AutonomousConfig supplies the sole clone/run configuration and cannot be combined with execution or identity overrides."
    }
    $BridgeScript = Join-Path $ProjectRoot "scripts\oni_autonomous.py"
    $ControllerArgs = @($BridgeScript, "start", "--config", $AutonomousConfig)
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 @ControllerArgs
    } else {
        & python @ControllerArgs
    }
    exit $LASTEXITCODE
}

$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) {
    $PythonArgs = @("-3", $BridgeScript, "--bridge-dir", $BridgeDir)
    if ($Once) {
        $PythonArgs += "--once"
    }
    if ($EnableLlm) {
        $PythonArgs += "--enable-llm"
    }
    if ($ModelLoop) {
        $PythonArgs += "--model-loop"
    }
    if ($SecretsDir) {
        $PythonArgs += @("--secrets-dir", $SecretsDir)
    }
    if ($Goal) {
        $PythonArgs += @("--goal", $Goal)
    }
    if ($StartupExpectationCount -eq $StartupExpectationValues.Count) {
        $PythonArgs += @(
            "--expected-validation-run-id", $ExpectedValidationRunId,
            "--expected-validation-process-id", ([string]$ExpectedValidationProcessId),
            "--expected-startup-binding-sha256", $ExpectedStartupBindingSha256,
            "--expected-startup-intent-hash", $ExpectedStartupIntentHash,
            "--expected-runtime-config-evidence-sha256", $ExpectedRuntimeConfigEvidenceSha256,
            "--expected-loader-path-id-upper", $ExpectedLoaderPathIdUpper,
            "--expected-loader-save-sha256", $ExpectedLoaderSaveSha256,
            "--expected-game-build", $ExpectedGameBuild,
            "--expected-mod-version", $ExpectedModVersion
        )
    }
    & py @PythonArgs
    exit $LASTEXITCODE
} else {
    $PythonArgs = @($BridgeScript, "--bridge-dir", $BridgeDir)
    if ($Once) {
        $PythonArgs += "--once"
    }
    if ($EnableLlm) {
        $PythonArgs += "--enable-llm"
    }
    if ($ModelLoop) {
        $PythonArgs += "--model-loop"
    }
    if ($SecretsDir) {
        $PythonArgs += @("--secrets-dir", $SecretsDir)
    }
    if ($Goal) {
        $PythonArgs += @("--goal", $Goal)
    }
    if ($StartupExpectationCount -eq $StartupExpectationValues.Count) {
        $PythonArgs += @(
            "--expected-validation-run-id", $ExpectedValidationRunId,
            "--expected-validation-process-id", ([string]$ExpectedValidationProcessId),
            "--expected-startup-binding-sha256", $ExpectedStartupBindingSha256,
            "--expected-startup-intent-hash", $ExpectedStartupIntentHash,
            "--expected-runtime-config-evidence-sha256", $ExpectedRuntimeConfigEvidenceSha256,
            "--expected-loader-path-id-upper", $ExpectedLoaderPathIdUpper,
            "--expected-loader-save-sha256", $ExpectedLoaderSaveSha256,
            "--expected-game-build", $ExpectedGameBuild,
            "--expected-mod-version", $ExpectedModVersion
        )
    }
    & python @PythonArgs
    exit $LASTEXITCODE
}
