@echo off
rem ============================================================
rem  slicer-workbench - stop all local services and close the window
rem  Thin wrapper around:  slicer-workbench\launcher\orchestrator.py stop
rem  Kept ASCII-only (see start.bat header for why).
rem ============================================================
setlocal

rem ROOT = repo root (two levels up from this script)
for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"
rem Workspace: env var wins; otherwise <repo>\workspace
if not defined JACK_WORKSPACE_ROOT set "JACK_WORKSPACE_ROOT=%ROOT%\workspace"
set "WS=%JACK_WORKSPACE_ROOT%"
set "PY=%WS%\runtime\python\python.exe"

if not exist "%PY%" (
  echo [ERROR] Portable Python not found: %PY%
  pause
  exit /b 1
)

cd /d "%ROOT%"
"%PY%" "%ROOT%\launcher\orchestrator.py" stop

echo.
timeout /t 2 /nobreak >nul
endlocal
