[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

Push-Location -LiteralPath $ProjectRoot
try {
    Invoke-Python -Arguments @("-m", "pip", "install", "-r", "requirements.txt", "nuitka", "zstandard")
    Invoke-Python -Arguments @("tools\make_icon.py")

    if (-not $SkipTests) {
        $PreviousQtPlatform = $env:QT_QPA_PLATFORM
        try {
            $env:QT_QPA_PLATFORM = "offscreen"
            Invoke-Python -Arguments @("-m", "unittest", "discover", "-s", "tests", "-v")
        }
        finally {
            $env:QT_QPA_PLATFORM = $PreviousQtPlatform
        }
    }

    Get-Process Theara -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 300

    Write-Host "Compiling Theara with Nuitka (Native C++ mode - 0 False Positives)..." -ForegroundColor Cyan

    Invoke-Python -Arguments @(
        "-m", "nuitka",
        "--standalone",
        "--windows-disable-console",
        "--enable-plugin=pyside6",
        "--windows-icon-from-ico=assets\theara.ico",
        "--include-data-dir=assets=assets",
        "--assume-yes-for-downloads",
        "--company-name=Theara",
        "--product-name=Theara",
        "--file-version=1.0.0.0",
        "--product-version=1.0.0.0",
        "--file-description=Theara Mail and 2FA Code Tool",
        "--copyright=Copyright © 2026 Theara. All rights reserved.",
        "--output-dir=dist\Theara-Nuitka",
        "--output-filename=Theara.exe",
        "main.py"
    )

    $BundleDirectory = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot "dist\Theara-Nuitka\main.dist")).Path
    $Executable = Join-Path $BundleDirectory "Theara.exe"

    $SendToDesktop = Join-Path $ProjectRoot "Send to Desktop.bat"
    if (Test-Path -LiteralPath $SendToDesktop) {
        Copy-Item -LiteralPath $SendToDesktop -Destination $BundleDirectory -Force
    }

    Invoke-Python -Arguments @("tools\export_bundle_accounts.py", $BundleDirectory)

    foreach ($SensitiveName in @("accounts.secure.json", "settings.json", "microsoft_token_cache.secure.json")) {
        if (Get-ChildItem -LiteralPath $BundleDirectory -Recurse -File | Where-Object Name -EQ $SensitiveName) {
            throw "Sensitive app data was copied into the distributable: $SensitiveName"
        }
    }

    $Smoke = Start-Process -FilePath $Executable -ArgumentList "--smoke-test" -Wait -PassThru -WindowStyle Hidden
    if ($Smoke.ExitCode -ne 0) {
        throw "The built executable failed its startup check with exit code $($Smoke.ExitCode)."
    }

    $DistTheara = Join-Path $ProjectRoot "dist\Theara"
    if (Test-Path -LiteralPath $DistTheara) {
        Remove-Item -LiteralPath $DistTheara -Recurse -Force
    }
    Copy-Item -LiteralPath $BundleDirectory -Destination $DistTheara -Recurse -Force

    $Archive = Join-Path $ProjectRoot "dist\Theara-windows-x64.zip"
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive -Force
    }
    Compress-Archive -LiteralPath $DistTheara -DestinationPath $Archive -CompressionLevel Optimal

    $ArchiveShort = Join-Path $ProjectRoot "dist\Theara.zip"
    Copy-Item -LiteralPath $Archive -Destination $ArchiveShort -Force

    Invoke-Python -Arguments @("tools\generate_release_manifest.py", "1.0.1")

    Write-Host ""
    Write-Host "Theara (Native Nuitka) build completed successfully!" -ForegroundColor Green
    Write-Host "Application: $(Join-Path $DistTheara 'Theara.exe')"
    Write-Host "Archive:     $Archive"
    Write-Host "Archive 2:   $ArchiveShort"
}
finally {
    Pop-Location
}
