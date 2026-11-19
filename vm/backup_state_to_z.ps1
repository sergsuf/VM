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

# 6. Copy restore script and create 1-click .bat launchers on Z:\_vm_state
$restorePs1 = Join-Path $PSScriptRoot "restore_state_from_z.ps1"
if (Test-Path $restorePs1) {
    Copy-Item -Path $restorePs1 -Destination (Join-Path $BackupRoot "restore_state_from_z.ps1") -Force
}

$batContent = @"
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_state_from_z.ps1"
pause
"@
$batContent | Set-Content -Path (Join-Path $BackupRoot "restore_worker.bat") -Encoding ascii
Write-Host "[Backup] Created 1-click launcher -> $BackupRoot\restore_worker.bat"

$maxAutoBatContent = @"
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_state_from_z.ps1" -OnlyPythonVenv -NoStartRunner
pause
"@
$maxAutoBatContent | Set-Content -Path (Join-Path $BackupRoot "restore_max_auto.bat") -Encoding ascii
Write-Host "[Backup] Created 1-click launcher -> $BackupRoot\restore_max_auto.bat"
Write-Host "[Backup] Done!"
