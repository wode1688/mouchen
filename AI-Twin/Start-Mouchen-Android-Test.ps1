$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendRoot = Join-Path $repositoryRoot "backend"
$runtimeRoot = Join-Path $backendRoot "data"
$python = (Get-Command python -ErrorAction Stop).Source
$port = 8788

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

function Test-MouchenHealth([string]$Url, [string]$CertificatePath) {
    try {
        $savedErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $payload = & "$env:SystemRoot\System32\curl.exe" `
            --silent `
            --fail `
            --max-time 2 `
            --cacert $CertificatePath `
            "$Url/health" 2>$null
        $curlExitCode = $LASTEXITCODE
        $ErrorActionPreference = $savedErrorActionPreference
        if ($curlExitCode -ne 0) {
            return $false
        }
        $health = $payload | ConvertFrom-Json
        return $health.status -eq "ok"
    }
    catch {
        $ErrorActionPreference = "Stop"
        return $false
    }
}

function Get-OrCreateToken([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $saved = (Get-Content -LiteralPath $Path -Raw).Trim()
        if ($saved.Length -ge 32) {
            return $saved
        }
    }

    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    $created = [BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant()
    Set-Content -LiteralPath $Path -Value $created -Encoding ASCII
    return $created
}

$listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    throw "Port $port is already used by process $($listener.OwningProcess): $($existing.Name). Stop the old test backend first."
}

$tokenPath = Join-Path $runtimeRoot "android-test-token.txt"
$token = Get-OrCreateToken $tokenPath
$env:MOUCHEN_API_TOKEN = $token
$env:MOUCHEN_API_BEARER_TOKEN = $token
$env:MOUCHEN_DB_PATH = Join-Path $runtimeRoot "mouchen.db"

$openAiApiReady = -not [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)
$openAiKeyLoadedFromStore = $false
$previousOpenAiApiKey = $env:OPENAI_API_KEY
$openAiEncryptedKeyPath = Join-Path $runtimeRoot "openai-api-key.dpapi"
$openAiConfigurationPath = Join-Path $runtimeRoot "openai-api-config.json"
if (Test-Path -LiteralPath $openAiEncryptedKeyPath) {
    try {
        $secureOpenAiKey = (Get-Content -LiteralPath $openAiEncryptedKeyPath -Raw).Trim() |
            ConvertTo-SecureString
        $openAiKeyBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureOpenAiKey)
        try {
            $env:OPENAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($openAiKeyBstr)
            $openAiKeyLoadedFromStore = $true
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($openAiKeyBstr)
        }

        if (Test-Path -LiteralPath $openAiConfigurationPath) {
            $openAiConfiguration = Get-Content -LiteralPath $openAiConfigurationPath -Raw |
                ConvertFrom-Json
            if ($openAiConfiguration.base_url) {
                $env:OPENAI_BASE_URL = [string]$openAiConfiguration.base_url
            }
            if ($openAiConfiguration.api_style) {
                $env:OPENAI_API_STYLE = [string]$openAiConfiguration.api_style
            }
            if ($openAiConfiguration.routine_model) {
                $env:OPENAI_ROUTINE_MODEL = [string]$openAiConfiguration.routine_model
            }
            if ($openAiConfiguration.complex_model) {
                $env:OPENAI_COMPLEX_MODEL = [string]$openAiConfiguration.complex_model
            }
        }
        $openAiApiReady = $true
    }
    catch {
        throw "The encrypted GPT configuration cannot be loaded. Run Set-Mouchen-OpenAI-Key.ps1 again."
    }
}

$localSttModel = Join-Path $repositoryRoot ".tools\models\ggml-base-q5_1.bin"
$localSttExpectedLength = 59707625
$localSttExpectedSha256 = "422F1AE452ADE6F30A004D7E5C6A43195E4433BC370BF23FAC9CC591F01A8898"
$localSttReady = $false
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpeg -and (Test-Path -LiteralPath $localSttModel)) {
    $model = Get-Item -LiteralPath $localSttModel
    if ($model.Length -eq $localSttExpectedLength) {
        $modelSha256 = (Get-FileHash -LiteralPath $localSttModel -Algorithm SHA256).Hash
        if ($modelSha256 -eq $localSttExpectedSha256) {
            try {
                $savedErrorActionPreference = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                $filterList = & $ffmpeg.Source -hide_banner -filters 2>&1 | Out-String
                $ffmpegExitCode = $LASTEXITCODE
                $ErrorActionPreference = $savedErrorActionPreference
                $localSttReady = $ffmpegExitCode -eq 0 -and $filterList -match "(?m)^\s*\.\.\s+whisper\s"
            }
            catch {
                $ErrorActionPreference = "Stop"
                $localSttReady = $false
            }
        }
    }
}
if ($localSttReady) {
    $env:MOUCHEN_LOCAL_STT_ENABLED = "true"
    $env:MOUCHEN_LOCAL_STT_FFMPEG = $ffmpeg.Source
    $env:MOUCHEN_LOCAL_STT_MODEL = $localSttModel
    $env:MOUCHEN_LOCAL_STT_TIMEOUT_SECONDS = "180"
    $env:MOUCHEN_LOCAL_STT_THREADS = "4"
    $env:MOUCHEN_LOCAL_STT_USE_GPU = "false"
}
else {
    $env:MOUCHEN_LOCAL_STT_ENABLED = "false"
}

$codexReady = $false
try {
    # Codex writes its successful login status to stderr. PowerShell 5.1 turns
    # that into a terminating NativeCommandError when ErrorActionPreference is
    # Stop, so temporarily allow the native process to report its real exit code.
    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $null = & codex login status 2>&1
    $codexReady = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $savedErrorActionPreference
}
catch {
    $ErrorActionPreference = "Stop"
    $codexReady = $false
}
if ($codexReady) {
    $env:MOUCHEN_CODEX_CLI_ENABLED = "true"
}

$lanAddress = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.AddressState -eq "Preferred" -and
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*" -and
        $_.InterfaceAlias -notmatch "vEthernet|WSL|Loopback"
    } |
    Select-Object -First 1 -ExpandProperty IPAddress
if (-not $lanAddress) {
    throw "No LAN IPv4 address was found. Connect the computer and phone to the same network first."
}

$tlsRoot = Join-Path $repositoryRoot ".tools\tls"
$tlsCertificate = Join-Path $tlsRoot "mouchen-private-alpha.crt"
$tlsPrivateKey = Join-Path $tlsRoot "mouchen-private-alpha.key"
if (-not (Test-Path -LiteralPath $tlsCertificate) -or -not (Test-Path -LiteralPath $tlsPrivateKey)) {
    throw "Private-alpha TLS files are missing. Run Setup-Mouchen-Private-TLS.ps1, trust the generated CA on the test device, then start the backend."
}

$stdoutPath = Join-Path $runtimeRoot "android-test-backend.log"
$stderrPath = Join-Path $runtimeRoot "android-test-backend.err.log"
try {
    $backendProcess = Start-Process `
        -FilePath $python `
        -ArgumentList @(
            "-m", "uvicorn", "app.main:app",
            "--host", "0.0.0.0",
            "--port", "$port",
            "--ssl-certfile", $tlsCertificate,
            "--ssl-keyfile", $tlsPrivateKey
        ) `
        -WorkingDirectory $backendRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
}
finally {
    if ($openAiKeyLoadedFromStore) {
        if ($null -eq $previousOpenAiApiKey) {
            Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
        }
        else {
            $env:OPENAI_API_KEY = $previousOpenAiApiKey
        }
    }
}

# Windows curl uses Schannel, which does not consistently match an IP-address
# subjectAltName. The private certificate always includes the DNS name localhost,
# so use that name for the local readiness probe while Android keeps using the
# LAN IP subjectAltName.
$loopbackUrl = "https://localhost:$port"
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 250
    if (Test-MouchenHealth $loopbackUrl $tlsCertificate) {
        $ready = $true
        break
    }
    if ($backendProcess.HasExited) {
        break
    }
}
if (-not $ready) {
    $errorTail = if (Test-Path -LiteralPath $stderrPath) {
        (Get-Content -LiteralPath $stderrPath -Tail 20) -join [Environment]::NewLine
    }
    else {
        "No error log is available."
    }
    throw "My AI Twin backend failed to start.`n$errorTail"
}

$pidPath = Join-Path $runtimeRoot "android-test-backend.pid"
Set-Content -LiteralPath $pidPath -Value $backendProcess.Id -Encoding ASCII

$claudeStatus = "binary=unknown; auth=unknown; inference=unavailable; reason=probe_error; model=unknown; elapsed_ms=0"
try {
    # Probe one minimal inference after the backend is already healthy. A
    # missing login, provider 503, or timeout only degrades Claude review; it
    # never stops the backend. The probe emits sanitized JSON and enforces its
    # own 25-second deadline, including Windows process-tree termination.
    $savedErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $probeScript = Join-Path $backendRoot "app\startup_health.py"
    $probeOutput = & $python $probeScript --timeout-seconds 25 2>$null | Select-Object -Last 1
    $probeExitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedErrorActionPreference
    if ($probeExitCode -eq 0 -and $probeOutput) {
        $probe = $probeOutput | ConvertFrom-Json
        $claudeStatus = "binary=$($probe.binary); auth=$($probe.auth); inference=$($probe.inference); reason=$($probe.reason); model=$($probe.model); elapsed_ms=$($probe.elapsed_ms)"
    }
}
catch {
    $ErrorActionPreference = "Stop"
}

$backendUrl = "https://${lanAddress}:$port"
$connectionPath = Join-Path $runtimeRoot "android-test-connection.txt"
$modelStatus = if ($openAiApiReady) {
    "GPT-5.6 Sol through the configured Responses API is enabled"
}
elseif ($codexReady) {
    "GPT-5.6 Sol through Codex login is enabled"
}
else {
    "Degraded model route; configure an API key or run codex login first"
}
$localSttStatus = if ($localSttReady) { "Local speech transcription is enabled" } else { "Local speech transcription is disabled; run Setup-Mouchen-Local-STT.ps1" }
$connectionText = @"
My AI Twin Android private-alpha connection
Backend URL: $backendUrl
User ID: demo-user
Token: $token
Proactive cloud analysis: ON
Minimized redacted context only: ON
Model status: $modelStatus
Claude review status: $claudeStatus
Speech status: $localSttStatus
Backend PID: $($backendProcess.Id)
"@
Set-Content -LiteralPath $connectionPath -Value $connectionText -Encoding UTF8

Write-Host ""
Write-Host "My AI Twin Android private-alpha connection"
Write-Host "Backend URL: $backendUrl"
Write-Host "User ID: demo-user"
Write-Host "Token: saved in the connection details file below"
Write-Host "Proactive cloud analysis: ON"
Write-Host "Minimized redacted context only: ON"
Write-Host "Model status: $modelStatus"
Write-Host "Claude review status: $claudeStatus"
Write-Host "Speech status: $localSttStatus"
Write-Host "Backend PID: $($backendProcess.Id)"
Write-Host "Connection details: $connectionPath"
Write-Host "Log: $stdoutPath"
Write-Host "Error log: $stderrPath"
Write-Host ""
Write-Host "If the phone cannot connect, allow Python on private networks in Windows Firewall."
