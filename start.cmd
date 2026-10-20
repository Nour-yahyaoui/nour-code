@echo off
REM ---------------------------------------------------------------------------
REM  nour-code - one-command Windows launcher
REM
REM    start.cmd              create .venv if needed, install, diagnose, serve
REM    start.cmd --doctor     only run the self-check
REM
REM  Requires Python 3.11+ on PATH (https://www.python.org/downloads/windows/).
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 goto use_python
set "PY=py -3"
goto check_python

:use_python
set "PY=python"

:check_python
%PY% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" 2>nul
if errorlevel 1 goto no_python

if not exist ".venv\Scripts\python.exe" goto create_venv
goto check_deps

:create_venv
echo Creating .venv ...
%PY% -m venv .venv
if errorlevel 1 exit /b 1

:check_deps
set "VENV_PY=.venv\Scripts\python.exe"
"%VENV_PY%" -c "import fastapi, uvicorn, httpx, pydantic, yaml" 2>nul
if errorlevel 1 goto install_deps
goto run

:install_deps
echo Installing dependencies ...
"%VENV_PY%" -m pip install --upgrade pip >nul
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

:run
"%VENV_PY%" -m nour_code --doctor
if errorlevel 1 goto diagnose_failed
if /i "%~1"=="--doctor" exit /b 0

echo.
echo Starting nour-code on http://localhost:8000 , press Ctrl+C to stop
"%VENV_PY%" -m nour_code --port 8000
exit /b %errorlevel%

:no_python
echo [FAIL] Python 3.11+ was not found on PATH.
echo        Install it from https://www.python.org/downloads/windows/ and tick
echo        "Add python.exe to PATH", then run this script again.
exit /b 1

:diagnose_failed
if /i "%~1"=="--doctor" exit /b 1
echo.
echo Fix the [FAIL] lines above, then run start.cmd again.
exit /b 1
