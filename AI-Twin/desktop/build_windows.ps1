[CmdletBinding()]
param(
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$desktopRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$repositoryRoot = [IO.Path]::GetFullPath((Split-Path -Parent $desktopRoot))
$artifactRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot "artifacts\windows\portable"))
$toolRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot ".tools\windows-portable-build"))
$allowedArtifactRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot "artifacts\windows"))
$allowedToolRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot ".tools"))

if (-not $artifactRoot.StartsWith($allowedArtifactRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to build outside the repository artifact directory."
}
if (-not $toolRoot.StartsWith($allowedToolRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create the build environment outside the repository tool directory."
}

$workRoot = Join-Path $artifactRoot "work"
$distRoot = Join-Path $artifactRoot "dist"
$venvRoot = Join-Path $toolRoot "venv"
$zipPath = Join-Path $artifactRoot "Mouchen-Windows-Portable-0.1.0-alpha03.zip"
$hashPath = Join-Path $artifactRoot "SHA256SUMS.txt"
$reportPath = Join-Path $artifactRoot "packaged-self-test.json"
$uiReportPath = Join-Path $artifactRoot "packaged-ui-smoke.json"
$auditScript = Join-Path $desktopRoot "packaging\audit_sensitive.py"

function Write-PackagedProbeDiagnostic {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string]$ReportPath
    )
    if (-not $ReportPath -or -not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
        return
    }
    try {
        $diagnostic = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $probe = [string]$diagnostic.probe
        $errorType = [string]$diagnostic.error_type
        if ($probe -notin @("self-test", "ui-smoke")) {
            $probe = "unknown"
        }
        if ($errorType -notmatch '^[A-Za-z_][A-Za-z0-9_.]{0,127}$') {
            $errorType = "unknown"
        }
        Write-Host "$Name diagnostic: probe=$probe; error_type=$errorType"
    } catch {
        Write-Host "$Name diagnostic report was unavailable."
    }
}

function Invoke-PackagedProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string]$DiagnosticReport,
        [switch]$Hidden
    )
    $start = @{
        FilePath = $Executable
        ArgumentList = $Arguments
        PassThru = $true
    }
    if ($Hidden) {
        $start.WindowStyle = "Hidden"
    }
    $process = Start-Process @start
    if (-not $process.WaitForExit(60000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Write-PackagedProbeDiagnostic -Name $Name -ReportPath $DiagnosticReport
        throw "$Name timed out after 60 seconds."
    }
    if ($process.ExitCode -ne 0) {
        Write-PackagedProbeDiagnostic -Name $Name -ReportPath $DiagnosticReport
        throw "$Name exited with code $($process.ExitCode)."
    }
}

$pythonVersion = & $PythonExecutable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.14") {
    throw "Windows packaging requires Python 3.14 exactly."
}
Write-Host "[1/8] Auditing packaging sources"
& $PythonExecutable $auditScript source $desktopRoot
if ($LASTEXITCODE -ne 0) { throw "Packaging source sensitivity audit failed." }

foreach ($target in @($workRoot, $distRoot, $venvRoot)) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
foreach ($target in @($zipPath, $hashPath, $reportPath, $uiReportPath)) {
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Force
    }
}

New-Item -ItemType Directory -Force -Path $artifactRoot, $toolRoot | Out-Null
Write-Host "[2/8] Installing locked build dependencies"
& $PythonExecutable -m venv $venvRoot
if ($LASTEXITCODE -ne 0) { throw "Unable to create the isolated build environment." }
$buildPython = Join-Path $venvRoot "Scripts\python.exe"
& $buildPython -m pip install --disable-pip-version-check --no-input --require-hashes --only-binary=:all: -r (Join-Path $desktopRoot "packaging\requirements-build.lock")
if ($LASTEXITCODE -ne 0) { throw "Unable to install locked build dependencies." }

New-Item -ItemType Directory -Force -Path (Join-Path $desktopRoot ".runtime") | Out-Null
$pytestTemp = Join-Path $desktopRoot ".runtime\pytest-package"
Write-Host "[3/8] Running desktop tests"
& $buildPython -m pytest (Join-Path $desktopRoot "tests") -q --basetemp $pytestTemp
if ($LASTEXITCODE -ne 0) { throw "Desktop tests failed." }

$previousSourceDateEpoch = [Environment]::GetEnvironmentVariable("SOURCE_DATE_EPOCH", "Process")
try {
    Write-Host "[4/8] Building frozen application"
    [Environment]::SetEnvironmentVariable("SOURCE_DATE_EPOCH", "1786464000", "Process")
    & $buildPython -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot (Join-Path $desktopRoot "packaging\Mouchen.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
} finally {
    [Environment]::SetEnvironmentVariable("SOURCE_DATE_EPOCH", $previousSourceDateEpoch, "Process")
}

$applicationRoot = Join-Path $distRoot "Mouchen"
$executable = Join-Path $applicationRoot "Mouchen.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "The packaged executable was not produced."
}
Copy-Item -LiteralPath (Join-Path $desktopRoot "packaging\README-First-Run.txt") -Destination $applicationRoot

$forbiddenFiles = Get-ChildItem -LiteralPath $applicationRoot -Recurse -File | Where-Object {
    $_.Name -in @("settings.json", ".env") -or
    $_.Extension -in @(".db", ".pem", ".key", ".jks", ".keystore")
}
if ($forbiddenFiles) {
    throw "Runtime settings, databases, or key files were found in the package."
}
Write-Host "[5/8] Auditing packaged files"
& $buildPython $auditScript package $applicationRoot
if ($LASTEXITCODE -ne 0) { throw "Packaged artifact sensitivity audit failed." }

$previousDataDir = [Environment]::GetEnvironmentVariable("MOUCHEN_DESKTOP_DATA_DIR", "Process")
$previousBackend = [Environment]::GetEnvironmentVariable("MOUCHEN_BACKEND_URL", "Process")
$smokeData = Join-Path $artifactRoot "isolated-smoke-data"
try {
    [Environment]::SetEnvironmentVariable("MOUCHEN_DESKTOP_DATA_DIR", $smokeData, "Process")
    # PowerShell 7 can expose a cleared process variable to a child as an
    # empty string.  AppSettings correctly rejects that as an invalid URL, so
    # make the isolated first-run value explicit and deterministic.
    [Environment]::SetEnvironmentVariable("MOUCHEN_BACKEND_URL", "http://127.0.0.1:8787", "Process")
    Write-Host "[6/8] Running packaged self-test"
    Invoke-PackagedProbe -Executable $executable -Arguments @("--self-test", "--self-test-report", ('"' + $reportPath + '"')) -Name "Packaged self-test" -DiagnosticReport $reportPath -Hidden
    Write-Host "[7/8] Running packaged UI smoke test"
    Invoke-PackagedProbe -Executable $executable -Arguments @("--ui-smoke", "--self-test-report", ('"' + $uiReportPath + '"')) -Name "Packaged UI smoke test" -DiagnosticReport $uiReportPath
} finally {
    [Environment]::SetEnvironmentVariable("MOUCHEN_DESKTOP_DATA_DIR", $previousDataDir, "Process")
    [Environment]::SetEnvironmentVariable("MOUCHEN_BACKEND_URL", $previousBackend, "Process")
}

if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
    throw "Packaged self-test did not write its report."
}
$uiReport = Get-Content -LiteralPath $uiReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $uiReport.window_visible -or -not $uiReport.service_address -or -not $uiReport.login_action -or -not $uiReport.register_action -or -not $uiReport.password_masked) {
    throw "Packaged UI smoke test did not render the first-run account gate."
}
$report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $report.frozen -or $report.embedded_credentials) {
    throw "Packaged self-test did not prove a clean first-run account gate."
}
if (Test-Path -LiteralPath $smokeData) {
    throw "Packaged self-test unexpectedly opened the user data directory."
}

Write-Host "[8/8] Creating portable archive"
Compress-Archive -LiteralPath $applicationRoot -DestinationPath $zipPath -CompressionLevel Optimal
$exeHash = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
@(
    "$exeHash  Mouchen\Mouchen.exe",
    "$zipHash  Mouchen-Windows-Portable-0.1.0-alpha03.zip"
) | Set-Content -LiteralPath $hashPath -Encoding ASCII

Write-Host "Windows package built and self-tested."
Write-Host "EXE: $executable"
Write-Host "EXE SHA256: $exeHash"
Write-Host "ZIP: $zipPath"
Write-Host "ZIP SHA256: $zipHash"
