@echo off
setlocal
title Content Production Launcher

set "PROJECT_DIR=%~dp0.."
for %%I in ("%PROJECT_DIR%") do set "PROJECT_DIR=%%~fI"

echo Starting Content Production backend and frontend...
echo.
echo Backend URL : http://127.0.0.1:8000
echo Frontend URL: http://127.0.0.1:5173
echo.

start "Content Production Backend" "%PROJECT_DIR%\scripts\start_backend.bat"
timeout /t 3 /nobreak > nul
start "Content Production Frontend" "%PROJECT_DIR%\scripts\start_frontend.bat"

echo Started launcher windows.
echo You can close this launcher window.
timeout /t 5 /nobreak > nul
