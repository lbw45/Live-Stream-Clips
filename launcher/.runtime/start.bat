@echo off
rem ============================================================
rem  Live Stream Clips - double-click launcher (thin wrapper)
rem  The real logic lives in:  launcher\orchestrator.py
rem
rem  Kept ASCII-only on purpose: cmd.exe reads .bat bytes as GBK,
rem  so non-ASCII content risks mojibake. Do not localise this file.
rem ============================================================
setlocal

rem ROOT = repo root (two levels up from this script)
for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"
rem Workspace: env var wins; otherwise <repo>\workspace
if not defined JACK_WORKSPACE_ROOT set "JACK_WORKSPACE_ROOT=%ROOT%\workspace"
set "WS=%JACK_WORKSPACE_ROOT%"
set "PY=%WS%\runtime\python\python.exe"

rem Portable runtime wins; otherwise fall back to the system Python that
rem setup.bat installed the dependencies into (same order as
rem resolve_python() in orchestrator.py).
if exist "%PY%" goto :have_py
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] No Python found. Run setup.bat first ^(it installs everything^),
  echo         or place a portable Python at: %WS%\runtime\python\python.exe
  echo.
  pause
  exit /b 1
)
:have_py

if not exist "%ROOT%\launcher\orchestrator.py" (
  echo [ERROR] Source tree not found:
  echo         %ROOT%
  echo.
  pause
  exit /b 1
)

cd /d "%ROOT%"
"%PY%" "%ROOT%\launcher\orchestrator.py" start

if errorlevel 1 (
  echo.
  echo [ERROR] Startup failed. Check logs under the workspace runtime-data folder.
  pause
  exit /b 1
)

rem Services and the desktop window are started detached, so this console
rem window can go away safely once startup finished.
timeout /t 3 /nobreak >nul
endlocal
