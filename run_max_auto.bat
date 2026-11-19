@echo off
chcp 65001 >nul
pushd "%~dp0"

set "PY=C:\venvs\max-auto\Scripts\python.exe"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    if exist "Z:\_vm_state\restore_state_from_z.ps1" (
        echo [MAX-AUTO] Python venv не знайдено. Автоматичне відновлення з Z:\_vm_state...
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "Z:\_vm_state\restore_state_from_z.ps1" -OnlyPythonVenv -NoStartRunner
    )
)

if not exist "%PY%" (
    echo [ERROR] Python not found: "%PY%"
    echo Create venv in C:\venvs\max-auto or .venv first, or run Z:\_vm_state\restore_max_auto.bat
    pause
    popd
    exit /b 1
)

:loop
set "ARGS=%*"
if not "%~1"=="" goto :run_cmd

tasklist /FI "IMAGENAME eq 3dsmax.exe" 2>nul | find /I "3dsmax.exe" >nul
if not errorlevel 1 goto :max_running

echo [MAX-AUTO] Процес 3dsmax.exe зараз не запущений.
set "ARGS=run --interactive"
goto :run_cmd

:max_running
echo [MAX-AUTO] Виявлено запущений процес 3dsmax.exe.
set "ARGS=watch --stop-on-ready"

:run_cmd
echo ==================================================
echo [MAX-AUTO] Виконання: "%PY%" -u -m cli.main %ARGS%
echo ==================================================
"%PY%" -u -m cli.main %ARGS%

powershell -NoProfile -Command "$sig='[DllImport(\"kernel32.dll\")]public static extern IntPtr GetConsoleWindow();[DllImport(\"user32.dll\")]public static extern bool SetForegroundWindow(IntPtr hWnd);[DllImport(\"user32.dll\")]public static extern bool ShowWindow(IntPtr hWnd,int nCmdShow);';$w=Add-Type -MemberDefinition $sig -Name Win -Namespace Console -PassThru;$h=$w::GetConsoleWindow();if($h -ne [IntPtr]::Zero){$w::ShowWindow($h,9);$w::SetForegroundWindow($h)|Out-Null}"

echo.
echo ==================================================
choice /C YN /M "Роботу завершено. Чи можна завершувати роботу?"
if errorlevel 2 goto :loop
popd
exit /b 0
