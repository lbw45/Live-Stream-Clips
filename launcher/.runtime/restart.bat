@echo off
rem ============================================================
rem  slicer-workbench - full restart (services + desktop window)
rem  Thin wrapper around:  slicer-workbench\launcher\orchestrator.py restart
rem
rem  Use this when you want a clean slate (also picks up FRONTEND
rem  changes, since the window reloads launcher\ui\index.html from disk).
rem
rem  Kept ASCII-only on purpose (see start.bat header for why).
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
"%PY%" "%ROOT%\launcher\orchestrator.py" restart

echo.
timeout /t 3 /nobreak >nul
endlocal
