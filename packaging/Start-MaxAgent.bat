@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%tools\ahk" set "PATH=%ROOT%tools\ahk;%PATH%"
if not exist "%ROOT%runtime\python.exe" (
    echo [MaxAgent] ERROR: Missing "%ROOT%runtime\python.exe".
    exit /b 1
)
if not exist "%ROOT%runtime\bootstrap.py" (
    echo [MaxAgent] ERROR: Missing "%ROOT%runtime\bootstrap.py".
    exit /b 1
)
"%ROOT%runtime\python.exe" -u "%ROOT%runtime\bootstrap.py" --root "%ROOT%." %*
exit /b %ERRORLEVEL%
