@echo off
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PY_EXE=python"
if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PY_EXE=%ROOT%.venv\Scripts\python.exe"
)

where "%PY_EXE%" >nul 2>&1
if errorlevel 1 (
    echo [MaxOperator] ERROR: Python not found. Install Python 3.10+ or create .venv.
    pause
    exit /b 1
)

if not exist "%ROOT%isos" mkdir "%ROOT%isos"
if not exist "%ROOT%assets" mkdir "%ROOT%assets"
if not exist "%ROOT%updates" mkdir "%ROOT%updates"

set "LAN_IP="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1 -ExpandProperty IPAddress; if ($ip) { $ip }"`) do set "LAN_IP=%%I"

set "PUBLIC_ARG="
if defined LAN_IP (
    set "PUBLIC_ARG=--public-url http://%LAN_IP%:8765"
    echo [MaxOperator] Detected LAN IP: %LAN_IP%
    echo [MaxOperator] Pairing connect.json will point to http://%LAN_IP%:8765
) else (
    echo [MaxOperator] Could not auto-detect LAN IP; using default pairing URL.
)

echo [MaxOperator] Starting Operator Control Panel on http://127.0.0.1:8765 ...
"%PY_EXE%" -u -m remote.operator.main ^
    --host 0.0.0.0 ^
    --port 8765 ^
    --iso-dir "%ROOT%isos" ^
    --assets-dir "%ROOT%assets" ^
    --updates-dir "%ROOT%updates" ^
    %PUBLIC_ARG% %*

if errorlevel 1 (
    echo.
    echo [MaxOperator] Exited with error %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)
