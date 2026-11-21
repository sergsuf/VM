@echo off
chcp 65001 >nul
setlocal

set "MEGA_URL=%~1"
if "%MEGA_URL%"=="" set "MEGA_URL=https://mega.nz/folder/AG5GUC7J#MG3WqOP3hQ-_MHJLAmzNfA"

set "TARGET_DIR=%~2"
if "%TARGET_DIR%"=="" set "TARGET_DIR=C:\Temp\mega_test"

set "PS_SCRIPT=%~dp0vm\mega_download.ps1"
if not exist "%PS_SCRIPT%" (
    echo [ERROR] Не знайдено скрипт: "%PS_SCRIPT%"
    exit /b 1
)

echo ============================================================
echo [MEGA-TEST] URL       : %MEGA_URL%
echo [MEGA-TEST] TargetDir : %TARGET_DIR%
echo [MEGA-TEST] Mode      : Portable megatools (direct folder structure)
echo ============================================================

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" -Url "%MEGA_URL%" -TargetDir "%TARGET_DIR%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [MEGA-TEST] Успішно завершено! Перевірте папку: "%TARGET_DIR%"
) else (
    echo [MEGA-TEST] Помилка виконання! Код виходу: %EXIT_CODE%
)

if not defined GITHUB_ACTIONS (
    if /i not "%NO_PAUSE%"=="1" pause
)

exit /b %EXIT_CODE%
