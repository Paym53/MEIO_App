@echo off
rem ============================================================================
rem  MEIO worker: starts the local API (and the ngrok tunnel) on the license PC
rem ============================================================================
rem  Double-click to start. Settings: local_settings.bat next to this file (a filled-in copy of
rem  local_settings.example.bat; never committed).
rem    This window ........ the API and its log. Stop: Ctrl+C, then answer Y.
rem    "MEIO tunnel" ...... ngrok (minimised), only if NGROK_DOMAIN is set. Close it to take the app offline.
rem ============================================================================
setlocal
cd /d "%~dp0"
title MEIO API

rem ---- 1. settings -------------------------------------------------------------
if not exist "local_settings.bat" (
  call :fail "local_settings.bat is missing. Copy local_settings.example.bat to local_settings.bat and fill it in."
  exit /b 1
)
call local_settings.bat
if "%MEIO_TOKEN%"=="paste-your-token-here" (
  call :fail "Set MEIO_TOKEN in local_settings.bat (the example file explains how to make one)."
  exit /b 1
)
if not exist "%MEIO_PYTHON%" (
  call :fail "Python not found: %MEIO_PYTHON%  (MEIO_PYTHON in local_settings.bat)"
  exit /b 1
)
set PYTHONUTF8=1

rem ---- 2. tunnel (optional) ----------------------------------------------------
if not defined NGROK_DOMAIN (
  echo NGROK_DOMAIN is empty: the API runs on this PC only, http://127.0.0.1:8000/docs
  goto api
)
"%NGROK_EXE%" version >nul 2>&1
if errorlevel 1 (
  call :fail "ngrok not found: %NGROK_EXE%  (install it, or set NGROK_EXE in local_settings.bat)"
  exit /b 1
)
echo Tunnel: https://%NGROK_DOMAIN%  ^>  http://127.0.0.1:8000   (window "MEIO tunnel", minimised)
start "MEIO tunnel" /min "%NGROK_EXE%" http 127.0.0.1:8000 --url=https://%NGROK_DOMAIN%

rem ---- 3. API (runs until Ctrl+C) ----------------------------------------------
:api
"%MEIO_PYTHON%" api\server.py
echo.
echo The API has stopped. If the "MEIO tunnel" window is still open, close it to take the app offline.
if not defined MEIO_NO_PAUSE pause
exit /b 0

:fail
echo.
echo ERROR: %~1
echo.
if not defined MEIO_NO_PAUSE pause
exit /b 0
