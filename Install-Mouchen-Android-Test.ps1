$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$apkPath = Join-Path $repositoryRoot "artifacts\release-v0.1.0-alpha05\Mouchen-PrivateAlpha-0.1.0-alpha05.apk"
$expectedSha256 = "AF390D76D5CFCD70E634BD157B820FE1486540F57093A3008EA10352290D958E"
$applicationId = "com.mouchen.app.alpha.debug"
$activityName = "com.mouchen.app.MainActivity"

function Find-Adb {
    $command = Get-Command adb -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "C:\tmp\mouchen-platform-tools\platform-tools\adb.exe",
        (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    throw "ADB was not found. Install Android platform-tools, then run this script again."
}

if (-not (Test-Path -LiteralPath $apkPath)) {
    throw "The alpha05 APK is missing: $apkPath"
}

$actualSha256 = (Get-FileHash -LiteralPath $apkPath -Algorithm SHA256).Hash
if ($actualSha256 -ne $expectedSha256) {
    throw "APK SHA256 mismatch. Expected $expectedSha256 but found $actualSha256."
}

$adbPath = Find-Adb
& $adbPath start-server | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "ADB server could not start."
}

$deviceLines = @(& $adbPath devices | Select-Object -Skip 1 | Where-Object { $_.Trim() })
$authorizedDevices = @(
    $deviceLines |
        Where-Object { $_ -match "^([^\s]+)\s+device$" } |
        ForEach-Object { ([regex]::Match($_, "^([^\s]+)")).Groups[1].Value }
)
$unauthorizedDevices = @($deviceLines | Where-Object { $_ -match "\s+unauthorized$" })

if ($unauthorizedDevices.Count -gt 0) {
    throw "The phone is waiting for USB debugging authorization. Unlock it, approve the computer fingerprint, then rerun this script."
}
if ($authorizedDevices.Count -eq 0) {
    throw "No authorized Android phone was found. Connect one phone by USB and enable Developer options > USB debugging."
}
if ($authorizedDevices.Count -gt 1) {
    throw "More than one Android device is connected. Leave only the phone that should receive My AI Twin."
}

$serial = $authorizedDevices[0]
$installOutput = & $adbPath -s $serial install -r $apkPath 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $installOutput -notmatch "(?m)^Success\s*$") {
    throw "APK installation failed.`n$installOutput"
}

$launchOutput = & $adbPath -s $serial shell am start -n "$applicationId/$activityName" 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or $launchOutput -match "Error type") {
    throw "My AI Twin was installed but could not be opened.`n$launchOutput"
}

$installedVersion = (& $adbPath -s $serial shell dumpsys package $applicationId 2>$null |
    Select-String -Pattern "versionName=|versionCode=" |
    Select-Object -First 2 |
    ForEach-Object { $_.Line.Trim() }) -join "; "

Write-Host "My AI Twin alpha05 installed and opened."
Write-Host "Device: $serial"
Write-Host "APK SHA256: $actualSha256"
if ($installedVersion) {
    Write-Host "Installed package: $installedVersion"
}
Write-Host "Next: follow the in-app permission checklist, then copy the URL, User ID, and Token from backend\data\android-test-connection.txt into Connections."
