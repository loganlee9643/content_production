@echo off
setlocal
title Content Production Frontend

set "PROJECT_DIR=%~dp0.."
for %%I in ("%PROJECT_DIR%") do set "PROJECT_DIR=%%~fI"
set "FRONTEND_DIR=%PROJECT_DIR%\frontend"

cd /d "%FRONTEND_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$listener = Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue; if ($listener) { Write-Host 'Frontend already appears to be running on port 5173.'; exit 10 }"

if %ERRORLEVEL% EQU 10 (
  echo.
  echo Frontend URL: http://127.0.0.1:5173
  echo Close this window, or stop the existing frontend before restarting.
  pause
  exit /b 0
)

echo Starting frontend...
echo Frontend URL: http://127.0.0.1:5173
npm run dev

echo.
echo Frontend stopped.
pause
