<#
.SYNOPSIS
    Restore GitHub Actions runner, agy CLI, gh CLI, and git config from Z:\_vm_state
    after a Hyper-V checkpoint rollback, and start the interactive runner.
#>
param(
    [string]$BackupRoot = "Z:\_vm_state",
    [string]$RunnerDir = "C:\actions-runner",
    [switch]$NoStartRunner
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $BackupRoot)) {
    throw "Backup folder not found: $BackupRoot"
}

function Restore-DirMirror {
    param(
        [string]$Source,
        [string]$Dest,
        [string[]]$ExcludeDirs = @()
    )
    if (-not (Test-Path $Source)) {
        Write-Host "[Restore] Skipping missing backup: $Source"
        return
    }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    $args = @($Source, $Dest, "/E", "/R:2", "/W:1", "/NFL", "/NDL", "/NP", "/NJH", "/NJS")
    if ($ExcludeDirs.Count -gt 0) {
        $args += "/XD"
        $args += $ExcludeDirs
    }
    & robocopy @args | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed ($LASTEXITCODE) restoring $Source -> $Dest"
    }
    $global:LASTEXITCODE = 0
    Write-Host "[Restore] Restored $Source -> $Dest"
}

# 1. Restore GitHub Actions Runner
Restore-DirMirror -Source (Join-Path $BackupRoot "actions-runner") -Dest $RunnerDir

# 2. Restore Antigravity CLI (agy) & .gemini
$agyDest = Join-Path $env:LOCALAPPDATA "agy"
Restore-DirMirror -Source (Join-Path $BackupRoot "agy_local") -Dest $agyDest
Restore-DirMirror -Source (Join-Path $BackupRoot "gemini_home") -Dest (Join-Path $env:USERPROFILE ".gemini")

# Ensure agy\bin is in User PATH
$agyBin = Join-Path $agyDest "bin"
if (Test-Path $agyBin) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$agyBin*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$agyBin", "User")
        $env:Path = "$env:Path;$agyBin"
        Write-Host "[Restore] Added $agyBin to User PATH"
    }
}

# 3. Restore GitHub CLI config & token
Restore-DirMirror -Source (Join-Path $BackupRoot "gh_cli") -Dest (Join-Path $env:APPDATA "GitHub CLI")
$tokenFile = Join-Path $BackupRoot "gh_token.txt"
if ((Test-Path $tokenFile) -and (Get-Command gh -ErrorAction SilentlyContinue)) {
    Get-Content $tokenFile -Raw | & gh auth login --with-token
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[Restore] Restored gh CLI authentication"
    } else {
        Write-Host "[Restore] Warning: gh auth login --with-token returned $LASTEXITCODE"
    }
    $global:LASTEXITCODE = 0
}

# 4. Restore .gitconfig
$gitCfgBackup = Join-Path $BackupRoot ".gitconfig"
if (Test-Path $gitCfgBackup) {
    Copy-Item -Path $gitCfgBackup -Destination (Join-Path $env:USERPROFILE ".gitconfig") -Force
    Write-Host "[Restore] Restored .gitconfig"
}

# 5. Ensure PowerShell ExecutionPolicy Bypass for CurrentUser
try {
    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy Bypass -Force
    Write-Host "[Restore] Set CurrentUser ExecutionPolicy to Bypass"
} catch {
    Write-Host "[Restore] Warning: failed to set ExecutionPolicy: $($_.Exception.Message)"
}

# 6. Register Scheduled Task so runner starts automatically at logon after any checkpoint rollback
try {
    $taskName = "ActionsRunner-AUTO"
    $actionCmd = '/c "if not exist C:\actions-runner\run.cmd (powershell.exe -NoProfile -ExecutionPolicy Bypass -File Z:\_vm_state\restore_state_from_z.ps1 -NoStartRunner) & if exist C:\actions-runner\run.cmd (cd /d C:\actions-runner && C:\actions-runner\run.cmd)"'
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $actionCmd
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "[Restore] Registered Scheduled Task: $taskName"
} catch {
    Write-Host "[Restore] Warning: failed to register Scheduled Task: $($_.Exception.Message)"
}

# 7. Start interactive runner if not already running
if (-not $NoStartRunner) {
    $running = Get-Process -Name "Runner.Listener" -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "[Restore] Runner.Listener is already running (PID $($running.Id))."
    } else {
        $runCmd = Join-Path $RunnerDir "run.cmd"
        if (Test-Path $runCmd) {
            Write-Host "[Restore] Starting interactive GitHub Actions Runner..."
            Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "`"$runCmd`"" -WorkingDirectory $RunnerDir
        } else {
            Write-Host "[Restore] Warning: $runCmd not found."
        }
    }
}

Write-Host "[Restore] Complete!"
