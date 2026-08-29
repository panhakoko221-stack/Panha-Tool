@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Build Theara.ps1" %*
if errorlevel 1 (
    echo.
    echo Theara build failed.
    pause
    exit /b 1
)
echo.
echo Theara build is ready in the dist folder.
pause
