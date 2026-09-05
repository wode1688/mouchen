param(
    [switch]$Rotate
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$tlsRoot = Join-Path $repositoryRoot ".tools\tls"
$certificatePath = Join-Path $tlsRoot "mouchen-private-alpha.crt"
$privateKeyPath = Join-Path $tlsRoot "mouchen-private-alpha.key"
$openssl = (Get-Command openssl -ErrorAction Stop).Source
$lanAddress = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.AddressState -eq "Preferred" -and
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*" -and
        $_.InterfaceAlias -notmatch "vEthernet|WSL|Loopback"
    } |
    Select-Object -First 1 -ExpandProperty IPAddress
if (-not $lanAddress) {
    throw "No LAN IPv4 address was found."
}

New-Item -ItemType Directory -Path $tlsRoot -Force | Out-Null
$existingPair = (Test-Path -LiteralPath $certificatePath) -and (Test-Path -LiteralPath $privateKeyPath)
if ($existingPair -and -not $Rotate) {
    $details = & $openssl x509 -in $certificatePath -noout -ext subjectAltName 2>&1 | Out-String
    if ($details -notmatch [regex]::Escape("IP Address:$lanAddress")) {
        throw "The existing certificate does not cover $lanAddress. Re-run with -Rotate, then rebuild and reinstall the APK."
    }
}
else {
    if ((Test-Path -LiteralPath $certificatePath) -xor (Test-Path -LiteralPath $privateKeyPath)) {
        throw "The TLS pair is incomplete. Preserve the remaining file, then re-run with -Rotate."
    }
    if ($existingPair) {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        Copy-Item -LiteralPath $certificatePath -Destination "$certificatePath.$stamp.bak"
        Copy-Item -LiteralPath $privateKeyPath -Destination "$privateKeyPath.$stamp.bak"
    }
    $subjectAltName = "subjectAltName=IP:$lanAddress,IP:127.0.0.1,IP:10.0.2.2,DNS:localhost,DNS:mouchen.local"
    & $openssl req `
        -x509 `
        -newkey rsa:3072 `
        -sha256 `
        -nodes `
        -keyout $privateKeyPath `
        -out $certificatePath `
        -days 3650 `
        -subj "/CN=My AI Twin Private Alpha" `
        -addext $subjectAltName `
        -addext "basicConstraints=critical,CA:TRUE" `
        -addext "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment" `
        -addext "extendedKeyUsage=serverAuth"
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL failed to create the private-alpha TLS certificate."
    }
}

$account = "$env:USERDOMAIN\$env:USERNAME"
& icacls.exe $privateKeyPath "/inheritance:r" "/grant:r" "${account}:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to restrict the private key permissions."
}
Write-Host "Private-alpha TLS is ready for $lanAddress."
Write-Host "Certificate: $certificatePath"
Write-Host "Private key: $privateKeyPath"
Write-Host "Install the generated CA on test devices explicitly; no certificate is bundled in source or APKs."
