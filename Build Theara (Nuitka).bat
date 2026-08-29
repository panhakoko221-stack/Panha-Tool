@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Build Theara (Nuitka).ps1" %*
if errorlevel 1 (
    echo.
    echo Theara Nuitka build failed.
    pause
    exit /b 1
)
echo.
echo Theara Nuitka build is ready in dist\Theara-Nuitka\main.dist folder.
pause
