@echo off
setlocal enabledelayedexpansion
title Avalanche Scenario Mapper - local server
cd /d "%~dp0"

set PORT=8000
set PAGE=index_mobile.html

echo.
echo   Avalanche Scenario Mapper - local server
echo   folder: %CD%
echo.

if not exist "data\avaScen_Winter_mobile_hex.geojson" (
  echo   [!] data\avaScen_Winter_mobile_hex.geojson not found.
  echo.
  echo       Put index_mobile.html, assets\, vendor\ and these .bat files
  echo       INTO the docs folder that already contains data\ - then run again.
  echo.
  pause
  exit /b 1
)

rem ------------------------------------------------------------------
rem  Find an interpreter that REALLY runs. "where python" is not enough:
rem  Windows ships a Microsoft-Store stub called python.exe that only
rem  opens the Store - that is a common reason the server never starts.
rem ------------------------------------------------------------------
set PY=

py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY ( python -c "import sys" >nul 2>&1 && set "PY=python" )
if not defined PY ( python3 -c "import sys" >nul 2>&1 && set "PY=python3" )

if not defined PY (
  echo   [!] No working Python found.
  echo.
  echo       Easiest fix: double-click  make_offline_data.bat  instead.
  echo       It converts the GeoJSONs once, and afterwards you can simply
  echo       double-click index_mobile.html - no server, no install.
  echo.
  echo       Or install Python from https://www.python.org/downloads/
  echo       and tick "Add python.exe to PATH" during setup.
  echo.
  choice /c YN /m "   Run make_offline_data.bat now"
  if !errorlevel! equ 1 call "%~dp0make_offline_data.bat"
  exit /b 1
)

echo   python:  !PY!
echo   URL:     http://localhost:%PORT%/%PAGE%
echo.
echo   Keep this window open while testing. Close it to stop the server.
echo.

rem open the browser a few seconds AFTER the server had time to bind
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start "" ""http://localhost:%PORT%/%PAGE%"""

!PY! -m http.server %PORT%

echo.
echo   Server stopped. If it quit straight away, port %PORT% is probably in
echo   use - change PORT at the top of this file to 8080 and try again.
echo.
pause
