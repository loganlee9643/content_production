@echo off
setlocal
set "BACKEND_DIR=%~dp0..\apps\backend"
set "WEB_DIR=%~dp0..\apps\web"
set "BACKEND_PORT=8001"
set "WEB_PORT=3000"
set "API_ORIGIN=http://127.0.0.1:%BACKEND_PORT%"

if /i "%~1"=="backend" goto backend
if /i "%~1"=="frontend" goto frontend
if /i "%~1"=="worker" goto worker
if not "%~1"=="" exit /b 2

title Shortform Production Launcher
echo Backend URL : %API_ORIGIN%
echo Frontend URL: http://127.0.0.1:%WEB_PORT%/references
start "Shortform Backend" "%ComSpec%" /d /k call "%~f0" backend
start "Shortform Frontend" "%ComSpec%" /d /k call "%~f0" frontend
start "Shortform Collection Worker" "%ComSpec%" /d /k call "%~f0" worker
exit /b

:backend
call :setup_backend || exit /b 1
call :check_port %BACKEND_PORT% || exit /b 1
echo Starting Shortform Backend at %API_ORIGIN%
"%PYTHON%" -m uvicorn main:app --reload --host 127.0.0.1 --port %BACKEND_PORT%
exit /b %ERRORLEVEL%

:worker
call :setup_backend || exit /b 1
echo Waiting for Shortform Backend before starting the collection worker...
powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(60); do { try { $api = Invoke-RestMethod -Uri '%API_ORIGIN%/openapi.json' -TimeoutSec 2; if ($api.paths.PSObject.Properties['/api/reference-topics']) { exit 0 } } catch {} ; Start-Sleep -Seconds 2 } while ((Get-Date) -lt $deadline); exit 1"
if errorlevel 1 (
    echo Shortform Backend did not become ready. Check the backend window and restart the worker.
    exit /b 1
)
"%PYTHON%" -m collection
exit /b %ERRORLEVEL%

:setup_backend
cd /d "%BACKEND_DIR%" || exit /b 1
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
if not exist "config.yaml" (
    echo Missing apps\backend\config.yaml. Set it up using config.example.yaml.
    exit /b 1
)
exit /b 0

:frontend
cd /d "%WEB_DIR%" || exit /b 1
if not exist "node_modules\.bin\next.cmd" (
    echo Missing frontend dependencies. Run npm install in apps\web.
    exit /b 1
)
call :check_port %WEB_PORT% || exit /b 1
echo Starting Shortform Frontend at http://127.0.0.1:%WEB_PORT%/references
call npm.cmd run dev -- --hostname 0.0.0.0 --port %WEB_PORT%
exit /b %ERRORLEVEL%

:check_port
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %~1 -State Listen -ErrorAction SilentlyContinue) { exit 1 }"
if errorlevel 1 (
    echo Port %~1 is already in use. Stop the existing server before starting another.
    exit /b 1
)
exit /b 0
