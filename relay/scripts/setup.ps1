param([string]$Python = 'python', [switch]$RuntimeOnly)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv (Join-Path $repoRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed. Python 3.11 or newer is required.' }
}
$requirements = if ($RuntimeOnly) { 'requirements.txt' } else { 'requirements-dev.txt' }
& $venvPython -m pip install -r (Join-Path $repoRoot $requirements)
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Output 'Local environment ready. Use .venv/Scripts/python.exe from this checkout.'
