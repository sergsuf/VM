<#
.SYNOPSIS
    Back up GitHub Actions runner, agy CLI, gh CLI, and git config to Z:\_vm_state
    so they survive Hyper-V checkpoint rollbacks.
#>
param(
    [string]$BackupRoot = "Z:\_vm_state",
    [string]$RunnerDir = "C:\actions-runner"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path "Z:\")) {
    throw "Drive Z:\ is not available."
}

New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null

function Copy-DirMirror {
    param(
        [string]$Source,
        [string]$Dest,
        [string[]]$ExcludeDirs = @()
    )
    if (-not (Test-Path $Source)) {
        Write-Host "[Backup] Skipping missing: $Source"
        return
    }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    $args = @($Source, $Dest, "/MIR", "/R:2", "/W:1", "/NFL", "/NDL", "/NP", "/NJH", "/NJS")
    if ($ExcludeDirs.Count -gt 0) {
        $args += "/XD"
        $args += $ExcludeDirs
    }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed ($LASTEXITCODE) copying $Source -> $Dest"
    }
    $global:LASTEXITCODE = 0
    Write-Host "[Backup] Saved $Source -> $Dest"
}

# 1. GitHub Actions Runner (exclude _work and _diag)
Copy-DirMirror -Source $RunnerDir -Dest (Join-Path $BackupRoot "actions-runner") -ExcludeDirs @("_work", "_diag")

# 2. Antigravity CLI (agy) binary & .gemini auth/config
Copy-DirMirror -Source (Join-Path $env:LOCALAPPDATA "agy") -Dest (Join-Path $BackupRoot "agy_local")
Copy-DirMirror -Source (Join-Path $env:USERPROFILE ".gemini") -Dest (Join-Path $BackupRoot "gemini_home") -ExcludeDirs @("tmp", "cache")

# 3. GitHub CLI config & token
Copy-DirMirror -Source (Join-Path $env:APPDATA "GitHub CLI") -Dest (Join-Path $BackupRoot "gh_cli")
if (Get-Command gh -ErrorAction SilentlyContinue) {
    try {
        $token = (& gh auth token 2>$null)
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($token)) {
            $token.Trim() | Set-Content -Path (Join-Path $BackupRoot "gh_token.txt") -Encoding ascii -NoNewline
            Write-Host "[Backup] Saved gh auth token -> $BackupRoot\gh_token.txt"
        } else {
            Write-Host "[Backup] Warning: gh auth token returned empty."
        }
    } catch {
        Write-Host "[Backup] Warning: failed to export gh auth token."
    }
    $global:LASTEXITCODE = 0
}

# 4. .gitconfig
$gitCfg = Join-Path $env:USERPROFILE ".gitconfig"
if (Test-Path $gitCfg) {
    Copy-Item -Path $gitCfg -Destination (Join-Path $BackupRoot ".gitconfig") -Force
    Write-Host "[Backup] Saved $gitCfg"
}

# 5. Python 3.12 & C:\venvs\max-auto
$py312Src = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"
Copy-DirMirror -Source $py312Src -Dest (Join-Path $BackupRoot "python312")
Copy-DirMirror -Source "C:\venvs\max-auto" -Dest (Join-Path $BackupRoot "venv_max_auto")

# 6. Copy restore script and create 1-click .bat launchers (single-window runner!)
$restorePs1 = Join-Path $PSScriptRoot "restore_state_from_z.ps1"
if (Test-Path $restorePs1) {
    Copy-Item -Path $restorePs1 -Destination (Join-Path $BackupRoot "restore_state_from_z.ps1") -Force
}

$workerBatContent = @"
@echo off
title GitHub Actions Worker (AUTO)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "Z:\_vm_state\restore_state_from_z.ps1" -NoStartRunner
if errorlevel 1 (
    echo [ERROR] Restore failed!
    pause
    exit /b 1
)
tasklist /FI "IMAGENAME eq Runner.Listener.exe" 2>nul | find /I "Runner.Listener.exe" >nul
if not errorlevel 1 (
    echo [Restore] Runner.Listener.exe is already running.
    pause
    exit /b 0
)
if exist "C:\actions-runner\run.cmd" (
    echo [Restore] Starting interactive GitHub Actions Runner...
    cd /d C:\actions-runner
    call C:\actions-runner\run.cmd
) else (
    echo [ERROR] C:\actions-runner\run.cmd not found!
    pause
    exit /b 1
)
"@
$workerBatContent | Set-Content -Path (Join-Path $BackupRoot "restore_worker.bat") -Encoding ascii
$workerBatContent | Set-Content -Path "Z:\1_Restore_Worker.bat" -Encoding ascii
Write-Host "[Backup] Created single-window worker launchers -> $BackupRoot\restore_worker.bat & Z:\1_Restore_Worker.bat"

$maxAutoRestoreBat = @"
@echo off
title Restore Python & max-auto venv
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_state_from_z.ps1" -OnlyPythonVenv -NoStartRunner
pause
"@
$maxAutoRestoreBat | Set-Content -Path (Join-Path $BackupRoot "restore_max_auto.bat") -Encoding ascii
Write-Host "[Backup] Created launcher -> $BackupRoot\restore_max_auto.bat"

$installMaxBat = @"
@echo off
title Autodesk 3ds Max Installer (2024-2028)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "Z:\max-auto\vm\Install-3dsMax.ps1" -Interactive -PauseOnExit
"@
$installMaxBat | Set-Content -Path "Z:\2_Install_3dsMax.bat" -Encoding ascii
Write-Host "[Backup] Created launcher -> Z:\2_Install_3dsMax.bat"

$startMaxAutoBat = @"
@echo off
call "Z:\max-auto\run_max_auto.bat" %*
"@
$startMaxAutoBat | Set-Content -Path "Z:\3_Start_MaxAuto.bat" -Encoding ascii
Write-Host "[Backup] Created launcher -> Z:\3_Start_MaxAuto.bat"

$backupStateBat = @"
@echo off
title Backup VM State to Z:\_vm_state
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "Z:\max-auto\vm\backup_state_to_z.ps1"
pause
"@
$backupStateBat | Set-Content -Path "Z:\4_Backup_VM_State.bat" -Encoding ascii
Write-Host "[Backup] Created launcher -> Z:\4_Backup_VM_State.bat"

Write-Host "[Backup] Done!"
