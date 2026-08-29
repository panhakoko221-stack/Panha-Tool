@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python, then run: pip install -r requirements.txt
    pause
    exit /b 1
)
where pythonw >nul 2>nul
if errorlevel 1 (
    echo pythonw.exe was not found in the active Python installation.
    pause
    exit /b 1
)
python -c "import PySide6, msal" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages...
    python -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo Could not install the required packages.
        pause
        exit /b 1
    )
)
start "Theara" pythonw "%~dp0main.py"
