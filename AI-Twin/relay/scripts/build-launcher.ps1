$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework/v4.0.30319/csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework C# compiler was not found on this Windows computer.' }
$outputPath = Join-Path $repoRoot 'AI-Twin-Relay.exe'
& $compiler /nologo /target:winexe /reference:System.Windows.Forms.dll "/out:$outputPath" (Join-Path $repoRoot 'windows/Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Launcher build failed.' }
Write-Output "Built $outputPath"
