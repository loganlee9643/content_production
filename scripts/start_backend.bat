@echo off
setlocal
title Content Production Backend

set "PROJECT_DIR=%~dp0.."
for %%I in ("%PROJECT_DIR%") do set "PROJECT_DIR=%%~fI"
set "BACKEND_DIR=%PROJECT_DIR%\backend"
set "PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"

cd /d "%BACKEND_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if ($listener) { Write-Host 'Backend already appears to be running on port 8000.'; exit 10 }"

if %ERRORLEVEL% EQU 10 (
  echo.
  echo Backend URL: http://127.0.0.1:8000
  echo Close this window, or stop the existing backend before restarting.
  pause
  exit /b 0
)

echo Starting backend...
echo Backend URL: http://127.0.0.1:8000
"%PYTHON%" .\start_suno_server.py --force-login --login-timeout 900

echo.
echo Backend stopped.
pause
