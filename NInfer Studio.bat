@echo off
setlocal EnableExtensions
title NInfer Studio - launch
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  Double-click this file to start both halves of NInfer Studio:
rem    engine  (ninfer-serve.exe, 127.0.0.1:8082)
rem    web UI  (webui\server.py, 127.0.0.1:8090)
rem  Paths, flags and ports come from webui\server.py via launcher.py.
rem  Safe to run twice: services already up are left alone.
rem ---------------------------------------------------------------

set "PY=py -3"
%PY% -V >nul 2>&1 || set "PY=python"
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [x] Python 3 not found on PATH.
  echo     Install Python 3 or point PY in this file at python.exe.
  pause
  exit /b 1
)

%PY% "%~dp0webui\launcher.py" start %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [x] Startup failed ^(exit code %RC%^).
  echo     Logs for diagnosis: "%~dp0logs"
  echo     This window stays open - read the message above.
  pause
  exit /b %RC%
)

echo.
echo [ok] NInfer Studio is up. Closing this window in 4 seconds...
timeout /t 4 /nobreak >nul
endlocal
