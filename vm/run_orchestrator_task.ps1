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

    [string]$CliExecutable = "agy",
    [string]$CliArgsPrefix = "-p",
    [string]$WorkingDir = ""
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "orchestrator_run.log"

function Write-OrchLog {
    param([Parameter(ValueFromPipeline = $true)]$InputObject)
    process {
        $text = if ($null -eq $InputObject) { "" } else { $InputObject.ToString() }
        Write-Host $text
        Add-Content -Path $logFile -Value $text -Encoding utf8
    }
}

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
$header | Out-File -FilePath $logFile -Encoding utf8

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
    } elseif (Test-Path "C:\venvs\max-auto\Scripts\Activate.ps1") {
        Write-Host "[Orchestrator] Activating C:\venvs\max-auto..."
        . "C:\venvs\max-auto\Scripts\Activate.ps1"
    }

    switch ($Mode) {
        "antigravity_cli" {
            $cmdArgs = @()
            if ($CliArgsPrefix.Trim()) {
                $cmdArgs += ($CliArgsPrefix.Trim() -split "\s+")
            }
            $cmdArgs += $PromptOrCommand
            Write-OrchLog "[Orchestrator] Running: $CliExecutable $($cmdArgs -join ' ')"
            & $CliExecutable @cmdArgs 2>&1 | Write-OrchLog
            $exitCode = $LASTEXITCODE
        }
        "max_auto" {
            $cmdArgs = $PromptOrCommand.Trim() -split "\s+"
            Write-OrchLog "[Orchestrator] Running: max-auto $($cmdArgs -join ' ')"
            & max-auto @cmdArgs 2>&1 | Write-OrchLog
            $exitCode = $LASTEXITCODE
        }
        "powershell" {
            Write-OrchLog "[Orchestrator] Running PowerShell snippet..."
            Invoke-Expression $PromptOrCommand 2>&1 | Write-OrchLog
            if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
                $exitCode = $LASTEXITCODE
            } elseif (-not $?) {
                $exitCode = 1
            }
        }
    }
}
catch {
    ($_ | Out-String) | Write-OrchLog
    $exitCode = 1
}
finally {
    Pop-Location
}

Write-OrchLog "`n[Orchestrator] Finished with exit code: $exitCode"
exit $exitCode
