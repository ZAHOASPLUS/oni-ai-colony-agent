[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TokenPath
)

$ErrorActionPreference = 'Stop'

if ($env:CODEX_FLEET_CREDENTIAL_REQUEST -ne '1') {
    throw 'Credential helper can only be called by Codex Fleet.'
}

if (-not (Test-Path -LiteralPath $TokenPath -PathType Leaf)) {
    throw 'Credential source file does not exist.'
}

$token = (Get-Content -LiteralPath $TokenPath -Raw -Encoding UTF8).Trim()
if ([string]::IsNullOrWhiteSpace($token)) {
    throw 'Credential value is empty.'
}

if ($token -match '\s') {
    throw 'Credential source must contain exactly one token.'
}

[Console]::Out.Write($token)
