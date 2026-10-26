<#
.SYNOPSIS
Start the autonomous controller in the background and return at once.

.DESCRIPTION
Launches "scripts/oni_autonomous.py start --config <Config>" as its own
process with stdout/stderr redirected to log files, writes a start record
next to the logs, prints it, and exits. It never waits for or watches the
controller: a command that also polls can outlive its timeout, become a
background job, and take the controller with it when that job is cancelled
(AGENTS.md, "Process lifetime"; Phase 2 rounds 19, 27 and 45). Watch with
"python scripts/watch_controller.py" in a separate command.

The controller itself refuses a second instance through its own lock; this
script adds no ownership of its own and does not launch ONI.

.EXAMPLE
powershell -File scripts/start-controller.ps1 -Config C:\path\autonomous-controller-config.v1.json -LogDir C:\path\logs
#>
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$LogDir,
    [string]$Label = "controller"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ConfigPath = (Resolve-Path -LiteralPath $Config).Path
if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}
$LogRoot = (Resolve-Path -LiteralPath $LogDir).Path
if ($Label -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
    throw "Label must be 1-64 characters of letters, digits, dot, underscore or dash."
}
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Stdout = Join-Path $LogRoot "$Label-$Stamp.log"
$Stderr = Join-Path $LogRoot "$Label-$Stamp.log.err"
$Script = Join-Path $ProjectRoot "scripts\oni_autonomous.py"

if (Get-Command py -ErrorAction SilentlyContinue) {
    $Exe = (Get-Command py).Source
    $Interpreter = "`"$Exe`" -3 -B"
} else {
    $Exe = (Get-Command python).Source
    $Interpreter = "`"$Exe`" -B"
}
foreach ($Value in @($Script, $ConfigPath, $Stdout, $Stderr)) {
    if ($Value -match '["%^&|<>]') {
        throw "Path contains a character cmd.exe would interpret: $Value"
    }
}

# Start-Process without -Redirect* uses ShellExecute, so the child inherits no
# handle from this shell; with -RedirectStandardOutput it would inherit the
# caller's output pipe, and a caller capturing this script's output would wait
# until the controller exited (verified 2026-10-27). cmd.exe does the
# redirection instead. The environment (model key variables) is inherited.
$CommandLine = "/d /c `"$Interpreter `"$Script`" start --config `"$ConfigPath`" 1>`"$Stdout`" 2>`"$Stderr`"`""
$Launcher = Start-Process -FilePath "cmd.exe" -ArgumentList $CommandLine -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden -PassThru

$Record = [ordered]@{
    schema     = "oni.controller-start-record.v1"
    started_at = (Get-Date).ToString("o")
    launcher_pid = $Launcher.Id
    note       = "launcher_pid is the cmd.exe wrapper; the controller records its own pid in autonomous-controller.json"
    config     = $ConfigPath
    stdout     = $Stdout
    stderr     = $Stderr
    watch      = "python scripts/watch_controller.py"
}
$RecordPath = Join-Path $LogRoot "$Label-$Stamp.start.json"
$Record | ConvertTo-Json | Out-File -FilePath $RecordPath -Encoding utf8
$Record | ConvertTo-Json
exit 0
