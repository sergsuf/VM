<#
.SYNOPSIS
    Executes an orchestrated task on the Windows VM (Antigravity CLI, max-auto, or PowerShell)
    and captures full logs for GitHub Actions artifact upload.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("antigravity_cli", "max_auto", "powershell")]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$PromptOrCommand,

    [string]$CliExecutable = "antigravity",
    [string]$CliArgsPrefix = "-p",
    [string]$WorkingDir = ""
)

$ErrorActionPreference = "Continue"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "orchestrator_run.log"

$sessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
$header = @"
============================================================
[Orchestrator] Timestamp : $(Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz")
[Orchestrator] Hostname  : $env:COMPUTERNAME
[Orchestrator] User      : $env:USERNAME
[Orchestrator] SessionId : $sessionId $(if ($sessionId -eq 0) { "(WARNING: Session 0 - GUI/AHK may not work!)" } else { "(Interactive Desktop Session)" })
[Orchestrator] Mode      : $Mode
[Orchestrator] Command   : $PromptOrCommand
============================================================
"@
$header | Tee-Object -FilePath $logFile

if ($WorkingDir -and (Test-Path $WorkingDir)) {
    Push-Location $WorkingDir
} else {
    Push-Location $repoRoot
}

$exitCode = 0
try {
    $venvActivate = Join-Path $repoRoot ".venv\Scripts\Activate.ps1"
    if (Test-Path $venvActivate) {
        Write-Host "[Orchestrator] Activating .venv..."
        . $venvActivate
    }

    switch ($Mode) {
        "antigravity_cli" {
            $cmdArgs = @()
            if ($CliArgsPrefix.Trim()) {
                $cmdArgs += ($CliArgsPrefix.Trim() -split "\s+")
            }
            $cmdArgs += $PromptOrCommand
            Write-Host "[Orchestrator] Running: $CliExecutable $($cmdArgs -join ' ')" | Tee-Object -FilePath $logFile -Append
            & $CliExecutable @cmdArgs 2>&1 | Tee-Object -FilePath $logFile -Append
            $exitCode = $LASTEXITCODE
        }
        "max_auto" {
            $cmdArgs = $PromptOrCommand.Trim() -split "\s+"
            Write-Host "[Orchestrator] Running: max-auto $($cmdArgs -join ' ')" | Tee-Object -FilePath $logFile -Append
            & max-auto @cmdArgs 2>&1 | Tee-Object -FilePath $logFile -Append
            $exitCode = $LASTEXITCODE
        }
        "powershell" {
            Write-Host "[Orchestrator] Running PowerShell snippet..." | Tee-Object -FilePath $logFile -Append
            Invoke-Expression $PromptOrCommand 2>&1 | Tee-Object -FilePath $logFile -Append
            if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
                $exitCode = $LASTEXITCODE
            } elseif (-not $?) {
                $exitCode = 1
            }
        }
    }
}
catch {
    $_ | Out-String | Tee-Object -FilePath $logFile -Append
    $exitCode = 1
}
finally {
    Pop-Location
}

"`n[Orchestrator] Finished with exit code: $exitCode" | Tee-Object -FilePath $logFile -Append
exit $exitCode
