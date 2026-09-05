$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidPath = Join-Path $repositoryRoot "backend\data\android-test-backend.pid"
if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "No recorded My AI Twin Android test backend was found."
    exit 0
}

$backendPid = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $backendPid" -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidPath -Force
    Write-Host "The test backend is already stopped."
    exit 0
}
if ($process.Name -notmatch "^python(?:w)?\.exe$" -or $process.CommandLine -notmatch "uvicorn\s+app\.main:app") {
    throw "PID $backendPid no longer belongs to the My AI Twin backend; nothing was stopped."
}

Stop-Process -Id $backendPid
Remove-Item -LiteralPath $pidPath -Force
Write-Host "My AI Twin Android test backend stopped."
