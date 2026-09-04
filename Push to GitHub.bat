@echo off
setlocal
cd /d "%~dp0"
title Push Panha-Tool to GitHub

echo ========================================================
echo        PUSHING CODE TO GITHUB (Panha-Tool)
echo ========================================================
echo.

git add .
git commit -m "Update Theara and release manifest" 2>nul
git push -u origin main

echo.
echo ========================================================
echo        PUSH COMPLETED!
echo ========================================================
echo.
pause
