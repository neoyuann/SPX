@echo off
setlocal enabledelayedexpansion
title Corporate event monitor
cd /d "%~dp0"

echo.
echo   CORPORATE EVENT MONITOR
echo   =======================
echo.

REM ---- Find Python -----------------------------------------------------
REM "py" is the Windows Python Launcher and is the most reliable, because it
REM works even when python.exe was never added to PATH. Fall back to python.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo   Python is not installed on this computer.
  echo.
  echo   1. Go to:  https://www.python.org/downloads/
  echo   2. Click the big yellow "Download Python" button.
  echo   3. Run the installer. IMPORTANT: on the first screen, tick the box
  echo      "Add python.exe to PATH" at the bottom before clicking Install.
  echo   4. When it finishes, double-click this file again.
  echo.
  pause
  exit /b 1
)

echo   Using: !PY!
echo.

REM ---- Install dependencies on first run only ---------------------------
!PY! -c "import feedparser, requests, bs4, yaml, lxml, dateutil" >nul 2>&1
if errorlevel 1 (
  echo   First run - installing what it needs. This takes a minute...
  echo.
  !PY! -m pip install --upgrade pip >nul 2>&1
  !PY! -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Install failed. Copy the red text above and check your internet
    echo   connection or company firewall - pip needs to reach pypi.org.
    echo.
    pause
    exit /b 1
  )
  echo.
  echo   Done.
  echo.
)

REM ---- Go ---------------------------------------------------------------
echo   Starting the tracker. Your browser will open automatically.
echo.
echo   KEEP THIS WINDOW OPEN while you use it.
echo   Closing this window stops the tracker.
echo.
echo   If the browser does not open by itself, go to:
echo       http://127.0.0.1:8765
echo.

!PY! -m tracker serve --open

echo.
echo   The tracker has stopped.
pause
