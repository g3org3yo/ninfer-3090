@echo off
setlocal EnableExtensions
title NInfer Studio stop

set "EPORT=8082"
set "UIPORT=8090"

echo [>] stopping studio UI on :%UIPORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"127.0.0.1:%UIPORT% .*LISTENING"') do (
  taskkill /PID %%p /F >nul 2>&1 && echo     killed %%p
)

echo [>] stopping ninfer engine on :%EPORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"127.0.0.1:%EPORT% .*LISTENING"') do (
  taskkill /PID %%p /F >nul 2>&1 && echo     killed %%p
)

echo [ok] done.
ping -n 3 127.0.0.1 >nul
endlocal
