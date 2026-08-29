param(
    [Parameter(Mandatory = $true)]
    [string]$TargetFile
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $TargetFile)) {
    throw "Target file does not exist: $TargetFile"
}

$Cert = Get-ChildItem -Path Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -like "*Theara*" } | Select-Object -First 1

if (-not $Cert) {
    Write-Host "Creating code signing certificate..."
    $Cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject "CN=Theara Software" `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -NotAfter (Get-Date).AddYears(5)
}

Write-Host "Signing $TargetFile..."
$SignResult = Set-AuthenticodeSignature -FilePath $TargetFile -Certificate $Cert -HashAlgorithm SHA256
Write-Host "Sign Status: $($SignResult.Status)"
