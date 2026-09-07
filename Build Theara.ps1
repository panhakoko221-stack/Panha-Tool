[CmdletBinding()]
param(
    [switch]$SkipTests,
    [string]$Version = "1.0.3"
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
    Invoke-Python -Arguments @("-m", "pip", "install", "-r", "requirements.txt", "-r", "requirements-build.txt")
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

    Invoke-Python -Arguments @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", "dist",
        "--workpath", "build",
        "Theara.spec"
    )

    $BundleDirectory = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot "dist\Theara")).Path
    $Executable = Join-Path $BundleDirectory "Theara.exe"

    $SendToDesktop = Join-Path $ProjectRoot "Send to Desktop.bat"
    if (Test-Path -LiteralPath $SendToDesktop) {
        Copy-Item -LiteralPath $SendToDesktop -Destination $BundleDirectory -Force
    }

    foreach ($SensitiveName in @(
        "accounts.txt",
        "accounts.csv",
        "accounts.secure.json",
        "settings.json",
        "microsoft_token_cache.secure.json"
    )) {
        if (Get-ChildItem -LiteralPath $BundleDirectory -Recurse -File | Where-Object Name -EQ $SensitiveName) {
            throw "Sensitive app data was copied into the distributable: $SensitiveName"
        }
    }

    $Smoke = Start-Process -FilePath $Executable -ArgumentList "--smoke-test" -Wait -PassThru -WindowStyle Hidden
    if ($Smoke.ExitCode -ne 0) {
        throw "The built executable failed its startup check with exit code $($Smoke.ExitCode)."
    }

    $Archive = Join-Path $ProjectRoot "dist\Theara-windows-x64.zip"
    if (Test-Path -LiteralPath $Archive) {
        Remove-Item -LiteralPath $Archive -Force
    }
    Compress-Archive -LiteralPath $BundleDirectory -DestinationPath $Archive -CompressionLevel Optimal

    $ArchiveShort = Join-Path $ProjectRoot "dist\Theara.zip"
    Copy-Item -LiteralPath $Archive -Destination $ArchiveShort -Force

    Invoke-Python -Arguments @("tools\generate_release_manifest.py", $Version)

    Write-Host ""
    Write-Host "Theara build completed successfully." -ForegroundColor Green
    Write-Host "Application: $Executable"
    Write-Host "Archive:     $Archive"
    Write-Host "Archive 2:   $ArchiveShort"
}
finally {
    Pop-Location
}
