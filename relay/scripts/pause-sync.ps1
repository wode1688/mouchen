param([string]$Config)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run scripts/setup.ps1 first.' }
$syncArguments = @('-B', (Join-Path $repoRoot 'companion.py'), '--pause')
if ($Config) { $syncArguments += @('--config', $Config) }
& $venvPython @syncArguments
exit $LASTEXITCODE
