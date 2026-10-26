# Configure the ONI model endpoint for the current Windows user.
#
# Presets (default: cline-pass / DeepSeek through the Cline-pass gateway):
#   cline-pass  https://api.cline.bot/api/v1   cline-pass/deepseek-v4.1-flash
#   openai      https://api.openai.com/v1      gpt-4.1-mini        (Responses API)
#   deepseek    https://api.deepseek.com/v1    deepseek-chat
#
# The key is read from the user environment when it is already present (for
# cline-pass that is CLINE_API_KEY, the same admitted credential the interactive
# session uses), so no secret is ever retyped, echoed, or written to the repo.

[CmdletBinding()]
param(
    [ValidateSet("cline-pass", "openai", "deepseek")]
    [string]$Provider = "cline-pass",
    [string]$Model,
    [string]$BaseUrl,
    [ValidateSet("auto", "responses", "chat_completions")]
    [string]$WireApi = "auto",
    [string]$InputCostPer1M,
    [string]$OutputCostPer1M,
    [switch]$Clear,
    [switch]$Show
)

$ErrorActionPreference = "Stop"

$presets = @{
    "cline-pass" = @{
        BaseUrl = "https://api.cline.bot/api/v1"
        Model   = "cline-pass/deepseek-v4.1-flash"
        WireApi = "chat_completions"
        KeyEnv  = @("CLINE_API_KEY", "ONI_AI_API_KEY", "OPENAI_API_KEY")
    }
    "openai"     = @{
        BaseUrl = "https://api.openai.com/v1"
        Model   = "gpt-4.1-mini"
        WireApi = "responses"
        KeyEnv  = @("OPENAI_API_KEY", "ONI_AI_API_KEY")
    }
    "deepseek"   = @{
        BaseUrl = "https://api.deepseek.com/v1"
        Model   = "deepseek-chat"
        WireApi = "chat_completions"
        KeyEnv  = @("DEEPSEEK_API_KEY", "ONI_AI_API_KEY", "OPENAI_API_KEY")
    }
}

$preset = $presets[$Provider]
$resolvedBaseUrl = if ($BaseUrl) { $BaseUrl.TrimEnd('/') } else { $preset.BaseUrl }
$resolvedModel = if ($Model) { $Model } else { $preset.Model }
$resolvedWire = if ($WireApi -eq "auto") { $preset.WireApi } else { $WireApi }

if ($Show) {
    $keyPresent = $false
    foreach ($name in $preset.KeyEnv) {
        if ([Environment]::GetEnvironmentVariable($name, "User")) { $keyPresent = $true }
    }
    Write-Host "Provider : $Provider"
    Write-Host "Base URL : $([Environment]::GetEnvironmentVariable('OPENAI_BASE_URL', 'User'))"
    Write-Host "Model    : $([Environment]::GetEnvironmentVariable('OPENAI_MODEL', 'User'))"
    Write-Host "Wire API : $([Environment]::GetEnvironmentVariable('OPENAI_WIRE_API', 'User'))"
    Write-Host "Key found in one of $($preset.KeyEnv -join ', ') : $keyPresent"
    exit 0
}

if ($Clear) {
    foreach ($name in @("OPENAI_API_KEY", "ONI_AI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_WIRE_API")) {
        [Environment]::SetEnvironmentVariable($name, $null, "User")
    }
    Write-Host "ONI model configuration cleared from the current user's environment."
    exit 0
}

# Prefer a key that is already provisioned for this provider.
$key = $null
foreach ($name in $preset.KeyEnv) {
    $candidate = [Environment]::GetEnvironmentVariable($name, "User")
    if ($candidate) { $key = $candidate; break }
}

if (-not $key) {
    $secure = Read-Host "$Provider API key (hidden input; leave this window private)" -AsSecureString
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if ($secure) { $secure.Dispose() }
    }
    if ([string]::IsNullOrWhiteSpace($key) -or $key.Length -lt 20) {
        throw "The key is empty or unexpectedly short."
    }
    # Persist the provider's own variable name; it is never written to the repo,
    # passed on a command line, or printed by this script.
    $primaryKeyEnv = $preset.KeyEnv[0]
    [Environment]::SetEnvironmentVariable($primaryKeyEnv, $key, "User")
}

# ONI reads OPENAI_* as its wire configuration; these values are not secret.
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $key, "User")
[Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", $resolvedBaseUrl, "User")
[Environment]::SetEnvironmentVariable("OPENAI_MODEL", $resolvedModel, "User")
[Environment]::SetEnvironmentVariable("OPENAI_WIRE_API", $resolvedWire, "User")
[Environment]::SetEnvironmentVariable("ONI_AI_PROVIDER", $Provider, "User")
if ($InputCostPer1M) { [Environment]::SetEnvironmentVariable("ONI_AI_INPUT_COST_PER_1M", $InputCostPer1M, "User") }
if ($OutputCostPer1M) { [Environment]::SetEnvironmentVariable("ONI_AI_OUTPUT_COST_PER_1M", $OutputCostPer1M, "User") }

Write-Host "ONI model configuration saved for the current Windows user."
Write-Host ("Provider: {0}; model: {1}; wire API: {2}" -f $Provider, $resolvedModel, $resolvedWire)
Write-Host ("Endpoint host: {0}; key present: {1}" -f ([Uri]$resolvedBaseUrl).Host, [bool]$key)
Write-Host "Restart the Bridge/controller process so it receives the updated environment."
