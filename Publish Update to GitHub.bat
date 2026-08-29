@echo off
setlocal
cd /d "%~dp0"
title Theara - 1-Click GitHub Release Publisher

echo ========================================================
echo        THEARA - PUBLISH LIVE UPDATE TO GITHUB
echo ========================================================
echo.

python "tools\publish_to_github.py"

echo.
pause
