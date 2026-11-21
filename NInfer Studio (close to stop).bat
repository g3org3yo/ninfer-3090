@echo off
setlocal EnableExtensions
title NInfer Studio - CLOSE THIS WINDOW TO STOP
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  One window, like Strata. Starts the same engine (127.0.0.1:8082)
rem  and the same web UI (127.0.0.1:8090), opens the browser, and
rem  keeps this console open as their on/off switch:
rem
rem    close the window  -> engine + web UI stop
rem    Ctrl+C            -> engine + web UI stop
rem
rem  No separate "NInfer Studio - Stop.bat" needed any more (it still
rem  works, in case something is left running).
rem  Flags/ports/model still come from webui\server.py.
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

%PY% "%~dp0studio_foreground.py" %*
set "RC=%ERRORLEVEL%"

echo.
if exist "logs\engine.log" echo     engine log: "%~dp0logs\engine.log"
echo [ok] NInfer Studio stopped ^(exit code %RC%^). You can close this window.
pause
endlocal
