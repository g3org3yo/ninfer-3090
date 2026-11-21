@echo off
setlocal EnableExtensions
title NInfer Studio - stop
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  Stops exactly the two processes of this install: whatever listens
rem  on 127.0.0.1:8090 (web UI) and on 127.0.0.1:8082 (engine).
rem  No other python.exe / ninfer-serve.exe is touched.
rem ---------------------------------------------------------------

set "PY=py -3"
%PY% -V >nul 2>&1 || set "PY=python"
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo [x] Python 3 not found on PATH, cannot run the launcher.
  pause
  exit /b 1
)

%PY% "%~dp0webui\launcher.py" stop %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo [x] Stop reported a problem ^(exit code %RC%^).
  echo     Check with: netstat -ano ^| findstr ":8090 :8082"
)
pause
endlocal
