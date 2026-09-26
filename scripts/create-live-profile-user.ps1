<#
.SYNOPSIS
    Create the dedicated local Windows account used for an ONI live profile.

.DESCRIPTION
    This is an explicit administrator-only bootstrap.  It validates an
    unused account name and creates a local account without accepting or
    storing a password.  It never derives or creates C:\Users\<name> and it
    cannot launch ONI.  Windows creates the profile directory on first login;
    the launcher must be run from that signed-in session so USERPROFILE and
    the known Documents/LocalLow roots are real values.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$UserName,

    [string]$Description = 'ONI live validation profile; sign in interactively before launching ONI.'
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($UserName)) {
    throw 'UserName is required.'
}
$UserName = $UserName.Trim()
if ($UserName.Length -gt 20) {
    throw 'UserName must be at most 20 characters.'
}
if ($UserName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$') {
    throw 'UserName may contain only ASCII letters, digits, dot, underscore, and hyphen, and must start with a letter or digit.'
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this bootstrap from an elevated PowerShell session.'
}

$getLocalUser = Get-Command Get-LocalUser -ErrorAction SilentlyContinue
$newLocalUser = Get-Command New-LocalUser -ErrorAction SilentlyContinue
if ($null -eq $getLocalUser -or $null -eq $newLocalUser) {
    throw 'The Microsoft.PowerShell.LocalAccounts module is required.'
}

$existing = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw 'The requested local account already exists; no changes were made.'
}

$result = [ordered]@{
    schema = 'oni.live-profile-user-bootstrap.v1'
    status = 'planned'
    user_name = $UserName
    password_mode = 'none'
    profile_path = $null
    launcher = 'scripts/start-live-profile.ps1'
}

if ($PSCmdlet.ShouldProcess("local account '$UserName'", 'Create without a password')) {
    New-LocalUser -Name $UserName -NoPassword -Description $Description -AccountNeverExpires -UserMayNotChangePassword:$false | Out-Null
    $result.status = 'created'
}

$result | ConvertTo-Json -Depth 5 -Compress
Write-Host ''
Write-Host 'First-login steps:'
Write-Host "1. Sign out of the administrator session and sign in to '$UserName'."
Write-Host '2. If Windows asks for a password, leave it blank; set one using Windows account settings if policy requires it.'
Write-Host '3. In that signed-in session, inspect $env:USERPROFILE and allow Windows to finish creating Documents and AppData\LocalLow.'
Write-Host '4. Derive $BridgeDir from [Environment]::GetFolderPath(''MyDocuments'') and create only that directory, then resolve the ONI executable and freeze its SHA-256.'
Write-Host "5. Run start-live-profile.ps1 -Action Inventory -GameExe <actual-exe> -BridgeDir <derived-bridge-dir> -ExpectedUserName $UserName."
Write-Host '6. Do not replace -ProfileRoot with a guessed C:\Users\<name> path; the launcher discovers the actual USERPROFILE.'
