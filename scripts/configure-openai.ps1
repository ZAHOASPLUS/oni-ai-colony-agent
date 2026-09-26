param(
    [string]$Model = "gpt-4.1-mini",
    [string]$BaseUrl = "https://api.openai.com/v1",
    [ValidateSet("responses", "chat_completions")]
    [string]$WireApi = "responses",
    [switch]$Clear
)

$ErrorActionPreference = "Stop"

if ($Clear) {
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $null, "User")
    Write-Host "OPENAI_API_KEY cleared from the current user's environment."
    exit 0
}

$secure = Read-Host "OpenAI API key (hidden input; leave this window private)" -AsSecureString
$pointer = [IntPtr]::Zero
$value = $null
try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($value) -or $value.Length -lt 20) {
        throw "The key is empty or unexpectedly short."
    }
    # Persist only to the per-user environment. It is never written to the
    # repository, passed on a command line, or printed by this script.
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $value, "User")
    [Environment]::SetEnvironmentVariable("OPENAI_BASE_URL", $BaseUrl.TrimEnd('/'), "User")
    [Environment]::SetEnvironmentVariable("OPENAI_MODEL", $Model, "User")
    [Environment]::SetEnvironmentVariable("OPENAI_WIRE_API", $WireApi, "User")
}
finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    if ($secure) {
        $secure.Dispose()
    }
    $value = $null
}

Write-Host "OpenAI configuration saved for the current Windows user."
Write-Host ("Model: {0}; wire API: {1}; key present: {2}" -f $Model, $WireApi, [bool]([Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")))
Write-Host "Restart the Bridge process so it receives the updated environment."
