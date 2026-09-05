param(
    [ValidateSet("codex_cli", "openai", "auto")]
    [string]$ModelProvider = "codex_cli"
)

$ErrorActionPreference = "Stop"

$mutexName = "Local\MouchenDesktopPrivateAlpha"
$existingInstance = $null
try {
    $existingInstance = [System.Threading.Mutex]::OpenExisting($mutexName)
}
catch [System.Threading.WaitHandleCannotBeOpenedException] {
    # No desktop instance currently owns the named mutex.
}
if ($null -ne $existingInstance) {
    try {
        Write-Host "My AI Twin is already running."
    }
    finally {
        $existingInstance.Dispose()
    }
    exit 0
}

$desktopRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $desktopRoot
$backendRoot = Join-Path $repositoryRoot "backend"
$launcher = Join-Path $desktopRoot "MouchenDesktop.pyw"
$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = $python
}

function Get-MouchenBackendHealth([string]$Url, [int]$TimeoutSec = 1) {
    try {
        $health = Invoke-RestMethod -Uri "$Url/health" -TimeoutSec $TimeoutSec
        if ($health.status -eq "ok") {
            return $health
        }
        return $null
    }
    catch {
        return $null
    }
}

function Test-MouchenLoopbackHost([string]$HostName) {
    return $HostName -in @("127.0.0.1", "localhost", "::1")
}

# A configured HTTPS server is authoritative. Its bearer token remains inside
# the desktop client's DPAPI-protected settings; this launcher neither reads nor
# exports it. Local model/provider setup is relevant only for a local backend.
$settingsPath = Join-Path $env:LOCALAPPDATA "Mouchen\Desktop\settings.json"
$configuredBackendUrl = $null
if (Test-Path -LiteralPath $settingsPath) {
    try {
        $savedSettings = Get-Content -Raw -LiteralPath $settingsPath | ConvertFrom-Json
        if ($savedSettings.backend_url) {
            $configuredBackendUrl = ([string]$savedSettings.backend_url).TrimEnd("/")
        }
    }
    catch {
        throw "My AI Twin desktop settings are invalid: $($_.Exception.Message)"
    }
}

$remoteBackendUri = $null
if ($configuredBackendUrl) {
    try {
        $candidateUri = [System.Uri]$configuredBackendUrl
        if ($candidateUri.Scheme -eq "https" -and -not (Test-MouchenLoopbackHost $candidateUri.Host)) {
            $remoteBackendUri = $candidateUri
        }
    }
    catch {
        throw "My AI Twin backend_url is invalid. Open My AI Twin settings and correct it."
    }
}

if ($null -ne $remoteBackendUri) {
    $remoteHealth = Get-MouchenBackendHealth $configuredBackendUrl 5
    if (-not $remoteHealth) {
        throw "Configured My AI Twin server is unavailable: $configuredBackendUrl"
    }
    Start-Process -FilePath $pythonw -ArgumentList @($launcher) -WorkingDirectory $desktopRoot
    exit 0
}

# Keep the provider decision explicit. Otherwise a stale process-level API key
# silently wins over the owner's authenticated Codex session.
$env:MOUCHEN_MODEL_PROVIDER = $ModelProvider
$env:MOUCHEN_CODEX_CLI_ENABLED = if ($ModelProvider -eq "openai") { "false" } else { "true" }
$expectedRuntimeContract = "proactive-analysis-v2"
$expectedModelProvider = $ModelProvider
if ($ModelProvider -eq "auto") {
    $expectedModelProvider = if ($env:OPENAI_API_KEY -or $env:OPENAI_API_KEY_FILE) {
        "openai"
    }
    else {
        "codex_cli"
    }
}

if ($expectedModelProvider -eq "codex_cli") {
    $codex = Get-Command codex -ErrorAction SilentlyContinue
    if (-not $codex) {
        throw "Codex CLI is unavailable. Install it or start with -ModelProvider openai."
    }
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $loginStatus = (& $codex login status 2>&1 | Out-String).Trim()
        $loginExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($loginExitCode -ne 0 -or $loginStatus -match "(?i)\bnot logged in\b" -or $loginStatus -notmatch "(?i)\blogged in(?:\s+using)?\b") {
        throw "Codex CLI is not authenticated. Run 'codex login' once, then start My AI Twin again."
    }
}

$backendUrl = "http://127.0.0.1:8787"
$existingHealth = Get-MouchenBackendHealth $backendUrl
if ($existingHealth -and (
    $existingHealth.model_provider -ne $expectedModelProvider -or
    $existingHealth.runtime_contract -ne $expectedRuntimeContract
)) {
    $actualProvider = if ($existingHealth.model_provider) { $existingHealth.model_provider } else { "unknown" }
    throw "My AI Twin backend on port 8787 is incompatible (provider '$actualProvider'). Stop the existing My AI Twin backend before restarting."
}
if (-not $existingHealth) {
    $backendUrl = $null
}

if (-not $backendUrl) {
    $backendUrl = "http://127.0.0.1:8787"
    Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8787") `
        -WorkingDirectory $backendRoot `
        -WindowStyle Hidden

    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        $startedHealth = Get-MouchenBackendHealth $backendUrl
        if ($startedHealth -and (
            $startedHealth.model_provider -ne $expectedModelProvider -or
            $startedHealth.runtime_contract -ne $expectedRuntimeContract
        )) {
            $actualProvider = if ($startedHealth.model_provider) { $startedHealth.model_provider } else { "unknown" }
            throw "My AI Twin backend started with an incompatible runtime (provider '$actualProvider')."
        }
        if ($startedHealth) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        throw "My AI Twin backend failed to start."
    }
}

$env:MOUCHEN_BACKEND_URL = $backendUrl
Start-Process -FilePath $pythonw -ArgumentList @($launcher) -WorkingDirectory $desktopRoot
