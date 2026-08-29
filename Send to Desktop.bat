@echo off
setlocal
cd /d "%~dp0"
set "EXE_PATH=%~dp0Theara.exe"
if not exist "%EXE_PATH%" (
    echo [ERROR] Theara.exe was not found in this folder.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $desktop = $ws.SpecialFolders.Item('Desktop'); $s = $ws.CreateShortcut((Join-Path $desktop 'Theara.lnk')); $s.TargetPath = '%EXE_PATH%'; $s.WorkingDirectory = '%~dp0'; $s.IconLocation = '%EXE_PATH%,0'; $s.Description = 'Theara - Mail & 2FA Code Tool'; $s.Save()"
echo.
echo ====================================================
echo  [OK] Theara shortcut created on your Desktop!
echo ====================================================
echo.
ping 127.0.0.1 -n 3 >nul
