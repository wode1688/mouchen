param(
    [string]$BaseUrl = "https://api.openai.com/v1",
    [string]$Model = "gpt-5.6-sol"
)

$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Join-Path $repositoryRoot "backend\data"
$encryptedKeyPath = Join-Path $runtimeRoot "openai-api-key.dpapi"
$configurationPath = Join-Path $runtimeRoot "openai-api-config.json"

function Get-NormalizedBaseUrl([string]$Value) {
    $candidate = $Value.Trim()
    try {
        $uri = [Uri]$candidate
    }
    catch {
        throw "The API base URL is invalid."
    }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne "https") {
        throw "The API base URL must be an absolute HTTPS URL."
    }
    if ($uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw "The API base URL cannot contain credentials, a query, or a fragment."
    }

    $normalized = $candidate.TrimEnd("/")
    if ($uri.AbsolutePath -eq "/") {
        $normalized += "/v1"
    }
    return $normalized
}

function Set-CurrentUserOnlyAcl([string]$Path) {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$normalizedBaseUrl = Get-NormalizedBaseUrl $BaseUrl
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

Write-Host "My AI Twin model connection setup"
Write-Host "Endpoint: $normalizedBaseUrl"
Write-Host "Model: $Model"
$secureKey = Read-Host "Paste the API key (input is hidden)" -AsSecureString
if ($secureKey.Length -lt 20) {
    throw "The API key is empty or unexpectedly short."
}

$bstr = [IntPtr]::Zero
$plainKey = $null
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $headers = @{
        Authorization = "Bearer $plainKey"
        "Content-Type" = "application/json"
    }
    $payload = @{
        model = $Model
        reasoning = @{ effort = "medium" }
        input = @(
            @{
                role = "user"
                content = @(
                    @{ type = "input_text"; text = "Reply with exactly MOUCHEN_OK." }
                )
            }
        )
    } | ConvertTo-Json -Depth 8 -Compress

    $null = Invoke-RestMethod `
        -Method Post `
        -Uri "$normalizedBaseUrl/responses" `
        -Headers $headers `
        -Body $payload `
        -TimeoutSec 120
}
catch {
    $status = $null
    if ($_.Exception.Response) {
        try { $status = [int]$_.Exception.Response.StatusCode } catch { $status = $null }
    }
    if ($status) {
        throw "The GPT smoke test failed with HTTP $status. The key was not saved."
    }
    throw "The GPT smoke test failed. The key was not saved: $($_.Exception.Message)"
}
finally {
    $plainKey = $null
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$encryptedKey = ConvertFrom-SecureString $secureKey
Set-Content -LiteralPath $encryptedKeyPath -Value $encryptedKey -Encoding ASCII
$configuration = [ordered]@{
    base_url = $normalizedBaseUrl
    api_style = "responses"
    routine_model = $Model
    complex_model = $Model
}
$configuration | ConvertTo-Json | Set-Content -LiteralPath $configurationPath -Encoding UTF8
Set-CurrentUserOnlyAcl $encryptedKeyPath
Set-CurrentUserOnlyAcl $configurationPath

Write-Host "GPT connection verified. The key is stored with Windows user encryption."
Write-Host "Restart the My AI Twin backend to activate it."
