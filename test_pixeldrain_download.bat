@echo off
chcp 65001 >nul
setlocal

set "PD_URL=%~1"
if "%PD_URL%"=="" set "PD_URL=https://pixeldrain.com/u/uDWVDL8c"

set "TARGET_DIR=%~2"
if "%TARGET_DIR%"=="" set "TARGET_DIR=C:\Temp\pixeldrain_test"

set "PS_SCRIPT=%~dp0vm\pixeldrain_download.ps1"
if not exist "%PS_SCRIPT%" (
    echo [ERROR] Не знайдено скрипт: "%PS_SCRIPT%"
    exit /b 1
)

echo ============================================================
echo [PIXELDRAIN-TEST] URL       : %PD_URL%
echo [PIXELDRAIN-TEST] TargetDir : %TARGET_DIR%
echo [PIXELDRAIN-TEST] Mode      : Pure PowerShell (Pixeldrain API)
echo ============================================================

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" -Url "%PD_URL%" -TargetDir "%TARGET_DIR%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [PIXELDRAIN-TEST] Успішно завершено! Перевірте папку: "%TARGET_DIR%"
) else (
    echo [PIXELDRAIN-TEST] Помилка виконання! Код виходу: %EXIT_CODE%
)

if not defined GITHUB_ACTIONS (
    if /i not "%NO_PAUSE%"=="1" pause
)

exit /b %EXIT_CODE%
