@echo off
rem ============================================================
rem  Live Stream Clips - first-run setup / doctor
rem
rem  Usage:
rem    setup.bat          install dependencies + check everything, then offer start
rem    setup.bat check    checks only, installs nothing, starts nothing
rem
rem  ASCII-only on purpose: cmd.exe reads .bat bytes as GBK, so
rem  non-ASCII content risks mojibake. See start.bat for the note.
rem ============================================================
setlocal enabledelayedexpansion

rem ROOT = repo root (this script lives in the repo root)
for %%I in ("%~dp0.") do set "ROOT=%%~fI"
if not defined JACK_WORKSPACE_ROOT set "JACK_WORKSPACE_ROOT=%ROOT%\workspace"
set "WS=%JACK_WORKSPACE_ROOT%"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=install"

rem Pre-expand program-files paths (parenthesised var names break if-blocks)
set "EDGE86=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "EDGE64=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"

echo ============================================================
echo  Live Stream Clips - setup   ^(mode: %MODE%^)
echo  Repo root : %ROOT%
echo  Workspace : %WS%
echo ============================================================
echo.

rem ---- 1. Python -----------------------------------------------------------
set "PY=%WS%\runtime\python\python.exe"
set "PYSRC=portable runtime"
if exist "%PY%" goto :py_found
set "PY=python"
set "PYSRC=system"
where python >nul 2>nul
if errorlevel 1 (
  echo [FAIL] Python not found.
  echo        Install Python 3.9 - 3.12 ^(64-bit^) from https://www.python.org
  echo        and tick "Add python.exe to PATH" in the installer,
  echo        or place a portable Python at: %WS%\runtime\python\python.exe
  echo.
  pause
  exit /b 1
)
:py_found
rem Make sure the interpreter can actually launch. A broken PATH entry
rem (e.g. a leftover IDE launcher stub) would fail every later step with
rem cryptic errors - fail HERE with clear guidance instead.
"%PY%" -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [FAIL] The Python at "%PY%" cannot start on this machine.
  echo        Common cause: a leftover IDE/venv launcher stub in PATH
  echo        ^(its real interpreter was moved or deleted^).
  echo        Fix: install a clean Python 3.9-3.12 from python.org and
  echo        tick "Add python.exe to PATH", or place a portable Python
  echo        at: %WS%\runtime\python\python.exe
  echo.
  pause
  exit /b 1
)
echo [OK]  Python   %PY%   ^(%PYSRC%^)
"%PY%" -c "import sys; print('      version:', sys.version.split()[0])"

"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info[0:2] >= (3, 9) and sys.version_info[0:2] < (3, 13) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [WARN] This Python is outside the tested range 3.9 - 3.12; continuing.
)

rem ---- 2. dependencies -----------------------------------------------------
rem Check first: a portable runtime may already carry every dependency,
rem so a missing pip is only fatal when something actually needs installing.
set "DEPS_OK=1"
"%PY%" -c "import webview, fastapi, uvicorn, httpx, PIL, numpy, playwright, faster_whisper, pyJianYingDraft" >nul 2>nul
if errorlevel 1 set "DEPS_OK=0"

if "%DEPS_OK%"=="1" (
  echo [OK]  All core dependencies importable.
) else if "%MODE%"=="install" (
  "%PY%" -m pip --version >nul 2>nul
  if errorlevel 1 (
    echo [FAIL] Dependencies are missing and this Python has no pip.
    echo        Fix: download https://bootstrap.pypa.io/get-pip.py then run
    echo             "%PY%" get-pip.py
    echo        ^(or run setup with a normal Python installation^)
    echo.
    pause
    exit /b 1
  )
  echo [*]  Installing Python dependencies ^(requirements.txt^) ...
  "%PY%" -m pip install -r "%ROOT%\requirements.txt"
  if errorlevel 1 (
    echo [FAIL] pip install failed. Check network / proxy, then retry.
    echo.
    pause
    exit /b 1
  )
) else (
  echo [FAIL] Some dependencies are missing. Run "setup.bat" to install.
)

rem ---- 3. ffmpeg -----------------------------------------------------------
if exist "%WS%\bin\ffmpeg.exe" (
  echo [OK]  ffmpeg   %WS%\bin\ffmpeg.exe
  goto :ff_done
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [WARN] ffmpeg not found.
  echo        Put ffmpeg.exe and ffprobe.exe into:  %WS%\bin
  echo        Download ^(release essentials^): https://www.gyan.dev/ffmpeg/builds/
) else (
  echo [OK]  ffmpeg found on PATH.
)
:ff_done

rem ---- 4. Edge -------------------------------------------------------------
if exist "%EDGE86%" (
  echo [OK]  Microsoft Edge found.
  goto :edge_done
)
if exist "%EDGE64%" (
  echo [OK]  Microsoft Edge found.
  goto :edge_done
)
echo [WARN] Microsoft Edge not found ^(needed for login windows and publishing^).
:edge_done

rem ---- 5. OCR (optional) ---------------------------------------------------
"%PY%" -c "import rapidocr_onnxruntime" >nul 2>nul
if errorlevel 1 (
  echo [INFO] OCR optional package not installed ^(slice-boundary OCR check is skipped^).
  echo        Optional install:  "%PY%" -m pip install rapidocr-onnxruntime
) else (
  echo [OK]  OCR package installed.
)

rem ---- 6. model note -------------------------------------------------------
echo [INFO] Whisper models auto-download on first transcribe.
echo        Slow network? Run before first use:
echo            set HF_ENDPOINT=https://hf-mirror.com

echo.
echo ============================================================
if "%MODE%"=="check" (
  echo Check finished. Fix the [FAIL]/[WARN] items above,
  echo then run "setup.bat" ^(without check^) to install and start.
  echo.
  set "DUMMY="
  set /p "DUMMY=Press Enter to close... "
  endlocal
  exit /b 0
)

echo ============================================================
echo Setup finished. Starting the app now ^(launcher\.runtime\start.bat^)...
echo ^(服务与桌面窗口由 start.bat 拉起；若失败，报错会停在这个窗口里^)
echo ============================================================
endlocal
call "%~dp0launcher\.runtime\start.bat"
goto :eof
