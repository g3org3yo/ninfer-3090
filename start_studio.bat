@echo off
setlocal EnableExtensions
title NInfer Studio launcher

rem ---------------------------------------------------------------
rem  Thin wrapper. Engine flags, ports and paths live in exactly ONE place:
rem  webui\server.py  ->  ENGINE_FLAGS   (webui\launcher.py reads them)
rem
rem  Do NOT re-add --max-context / --kv-dtype here. This file used to carry
rem  its own copy, the Studio's copy drifted to 32768 bf16, and the engine
rem  then answered every long Hermes turn with
rem  "prepared prompt exceeds Engine max_context 32768". Hermes Agent also
rem  hard-refuses any model under 65536 tokens (agent_init raises
rem  "below the minimum 65,536 required by Hermes Agent").
rem  Change the flags in webui\server.py and both launchers follow.
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
  echo [x] Startup failed ^(exit code %RC%^). Logs: "%~dp0logs"
  pause
  exit /b %RC%
)
echo.
echo  NInfer Studio: http://127.0.0.1:8090/
echo  Engine API:   http://127.0.0.1:8082/v1
echo  Metrics:      http://127.0.0.1:8082/metrics
endlocal
