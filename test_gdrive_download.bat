@echo off
chcp 65001 >nul
setlocal

set "GD_URL=%~1"
if "%GD_URL%"=="" set "GD_URL=https://drive.google.com/file/d/1ltWrICeaHd3_gI6vUM5RmPzued4C7Wns/view?usp=sharing"

set "TARGET_DIR=%~2"
if "%TARGET_DIR%"=="" set "TARGET_DIR=C:\Temp\gdrive_test"

set "PS_SCRIPT=%~dp0vm\gdrive_download.ps1"
if not exist "%PS_SCRIPT%" (
    echo [ERROR] Не знайдено скрипт: "%PS_SCRIPT%"
    exit /b 1
)

echo ============================================================
echo [GDRIVE-TEST] URL       : %GD_URL%
echo [GDRIVE-TEST] TargetDir : %TARGET_DIR%
echo [GDRIVE-TEST] Mode      : Pure PowerShell (no Python / no exe)
echo ============================================================

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" -Url "%GD_URL%" -TargetDir "%TARGET_DIR%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [GDRIVE-TEST] Успішно завершено! Перевірте папку: "%TARGET_DIR%"
) else (
    echo [GDRIVE-TEST] Помилка виконання! Код виходу: %EXIT_CODE%
)

if not defined GITHUB_ACTIONS (
    if /i not "%NO_PAUSE%"=="1" pause
)

exit /b %EXIT_CODE%
