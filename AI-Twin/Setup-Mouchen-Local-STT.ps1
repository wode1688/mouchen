param(
    [string]$InstallDirectory = (Join-Path $PSScriptRoot ".tools\models")
)

$ErrorActionPreference = "Stop"
$modelName = "ggml-base-q5_1.bin"
$modelUrl = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$modelName"
$expectedLength = 59707625
$expectedSha256 = "422F1AE452ADE6F30A004D7E5C6A43195E4433BC370BF23FAC9CC591F01A8898"

$ffmpeg = Get-Command ffmpeg -ErrorAction Stop
$filterList = & $ffmpeg.Source -hide_banner -filters 2>&1 | Out-String
if ($filterList -notmatch "(?m)^\s*\.\.\s+whisper\s") {
    throw "The installed FFmpeg build does not include the whisper filter. Install a full FFmpeg build with --enable-whisper."
}

New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
$modelPath = Join-Path $InstallDirectory $modelName
$curl = Get-Command curl.exe -ErrorAction Stop
& $curl.Source `
    --fail `
    --location `
    --continue-at - `
    --retry 8 `
    --retry-delay 2 `
    --output $modelPath `
    $modelUrl
if ($LASTEXITCODE -ne 0) {
    throw "Model download failed with curl exit code $LASTEXITCODE."
}

$model = Get-Item -LiteralPath $modelPath
if ($model.Length -ne $expectedLength) {
    throw "Downloaded model length is $($model.Length), expected $expectedLength."
}
$actualSha256 = (Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash
if ($actualSha256 -ne $expectedSha256) {
    throw "Downloaded model checksum does not match the published model."
}

Write-Host "Local transcription runtime is ready."
Write-Host "FFmpeg: $($ffmpeg.Source)"
Write-Host "Model: $modelPath"
Write-Host "SHA256: $actualSha256"
