@echo off
setlocal EnableExtensions
title NInfer 0.18.0-rtx3090 (127.0.0.1:8082) + /dashboard
cd /d "%~dp0"

set "EXE=%~dp0dist\ninfer-rtx3090-windows-x64-0.18.0-rtx3090\ninfer-serve.exe"
if not exist "%EXE%" (
  echo [x] Missing: "%EXE%"
  pause
  exit /b 1
)

set "MODEL=%~dp0models\Swift-1.5-Qwen3.8-27b.ninfer"
if not exist "%MODEL%" set "MODEL=%~dp0dist\ninfer-rtx3090-windows-x64-0.16.2-rtx3090\models\qwen3_8_27b.ninfer"
if not exist "%MODEL%" (
  echo [x] No .ninfer model found.
  pause
  exit /b 1
)

if not exist "%~dp0logs" mkdir "%~dp0logs"

rem Open http://127.0.0.1:8082/dashboard once /health responds (background waiter)
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i=0; $i -lt 180; $i++) { try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8082/health' -TimeoutSec 2; if ($r.StatusCode -eq 200) { Start-Process 'http://127.0.0.1:8082/dashboard'; break } } catch {}; Start-Sleep -Seconds 2 }"

echo Starting NInfer 0.18.0-rtx3090 on http://127.0.0.1:8082 ...
echo Dashboard will open at http://127.0.0.1:8082/dashboard once loaded.
echo Close this window or press Ctrl+C to stop the engine.
echo.

"%EXE%" "%MODEL%" ^
  --host 127.0.0.1 ^
  --port 8082 ^
  --spec mtp ^
  --draft-tokens 3 ^
  --prefill-cublas ^
  --prefill-chunk 4096 ^
  --max-context 65536 ^
  --kv-capacity 65536 ^
  --kv-dtype rk8v4 ^
  --fit-context 65536 ^
  --vision ^
  --model-suspend ^
  --log-stats-interval-ms 5000 ^
  --request-log-jsonl "%~dp0logs\studio-requests.jsonl"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [x] ninfer-serve exited with code %RC%.
  pause
)
endlocal
