@echo off
rem ============================================================
rem  slicer-workbench - reload services only, keep the desktop window
rem  Thin wrapper around:  slicer-workbench\launcher\orchestrator.py reload-services
rem
rem  Use this after changing BACKEND code: the services are detached
rem  background processes, so closing the window does NOT stop them.
rem  Without a reload you would get "new window + old backend".
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
"%PY%" "%ROOT%\launcher\orchestrator.py" reload-services

echo.
timeout /t 3 /nobreak >nul
endlocal
