param([string]$Config, [switch]$Once)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run scripts/setup.ps1 first.' }
$syncArguments = @('-B', (Join-Path $repoRoot 'companion.py'))
if ($Config) { $syncArguments += @('--config', $Config) }
if (-not $Once) { $syncArguments += '--watch' }
& $venvPython @syncArguments
exit $LASTEXITCODE
