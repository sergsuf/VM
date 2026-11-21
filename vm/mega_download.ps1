#Requires -Version 5.1

<#
.SYNOPSIS
    Concise Mega.nz folder/file downloader using official MEGAcmd (mega-get).
    Auto-installs MEGAcmd silently if missing, starts MEGAcmdServer detached,
    and displays smooth monotonic % progress in the console.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$TargetDir,
    [double]$EstimatedTotalMB = 1500,
    [int]$EstimatedFiles = 190
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$script:MaxPercent = 0
$script:LastReportedPercent = -1

function Write-MegaLog {
    param([string]$Message, [ValidateSet('INFO', 'ERROR', 'OK')][string]$Level = 'INFO')
    Write-Host ("[{0:yyyy-MM-dd HH:mm:ss}] [MEGA][{1}] {2}" -f (Get-Date), $Level, $Message)
}

function Write-MegaProgress {
    param([int]$Percent, [string]$Message, [switch]$Force)
    $Percent = [Math]::Max($script:MaxPercent, [Math]::Min(100, $Percent))
    $script:MaxPercent = $Percent

    if ($Force -or ($Percent -ne $script:LastReportedPercent)) {
        $script:LastReportedPercent = $Percent
        $filled = [int][Math]::Floor(30 * $Percent / 100)
        $bar = ('#' * $filled) + ('-' * (30 - $filled))
        Write-Host ("[{0}] {1,3}% | {2}" -f $bar, $Percent, $Message)
    }
}

function Get-FallbackPercent {
    param([string]$Dir)
    $files = @(Get-ChildItem -LiteralPath $Dir -Recurse -File -ErrorAction SilentlyContinue)
    if ($files.Count -eq 0) { return 1 }
    $mb = ($files | Measure-Object -Property Length -Sum).Sum / 1MB
    $byteRatio = if ($EstimatedTotalMB -gt 0) { [Math]::Min(1.0, $mb / $EstimatedTotalMB) } else { 0.0 }
    $fileRatio = if ($EstimatedFiles -gt 0) { [Math]::Min(1.0, $files.Count / $EstimatedFiles) } else { 0.0 }
    return [Math]::Max(1, [Math]::Min(99, [int][Math]::Floor((0.85 * $byteRatio + 0.15 * $fileRatio) * 99)))
}

function Ensure-MegaCmd {
    $megaDir = @(
        (Join-Path $env:LOCALAPPDATA 'MEGAcmd'),
        (Join-Path ${env:ProgramFiles} 'MEGAcmd'),
        (Join-Path ${env:ProgramFiles(x86)} 'MEGAcmd')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ 'mega-get.bat') -PathType Leaf) } | Select-Object -First 1

    if (-not $megaDir) {
        Write-MegaLog 'MEGAcmd не знайдено. Завантаження та встановлення MEGAcmdSetup64.exe...' 'INFO'
        $inst = Join-Path $env:TEMP 'MEGAcmdSetup64.exe'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri 'https://mega.nz/MEGAcmdSetup64.exe' -OutFile $inst -UseBasicParsing -TimeoutSec 180
        $p = Start-Process -FilePath $inst -ArgumentList '/S' -Wait -PassThru
        Remove-Item -LiteralPath $inst -Force -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0) { throw "Встановлення MEGAcmd завершилось з кодом $($p.ExitCode)" }
        $megaDir = Join-Path $env:LOCALAPPDATA 'MEGAcmd'
        if (-not (Test-Path -LiteralPath (Join-Path $megaDir 'mega-get.bat') -PathType Leaf)) {
            throw "Не знайдено mega-get.bat у $megaDir"
        }
    }

    $serverExe = Join-Path $megaDir 'MEGAcmdServer.exe'
    if (-not (Get-Process -Name 'MEGAcmdServer' -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $serverExe -PathType Leaf)) {
        Start-Process -FilePath $serverExe -WindowStyle Hidden
        Start-Sleep -Seconds 2
    }

    return (Join-Path $megaDir 'mega-get.bat')
}

function Invoke-MegaDownload {
    param([string]$MegaGetBat, [string]$MegaUrl, [string]$Dir)

    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    Write-MegaProgress -Percent 1 -Message 'Starting Mega download...' -Force

    $captured = New-Object System.Collections.Generic.List[string]
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $MegaGetBat $MegaUrl $Dir 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$($_)" }
            if ([string]::IsNullOrWhiteSpace($line)) { return }
            $captured.Add($line)

            if ($line -match 'TRANSFERRING.*?\(\s*([\d.]+)\s*/\s*([\d.]+)\s*([KMG]?B)\s*:\s*([\d.]+)\s*%') {
                $pct = [int][Math]::Floor([double]$Matches[4])
                if ($pct -ge 100 -and $line -notmatch 'Download finished') { $pct = 99 }
                Write-MegaProgress -Percent $pct -Message "$($Matches[1])/$($Matches[2]) $($Matches[3])"
            } elseif ($line -match 'Download finished') {
                Write-MegaProgress -Percent 99 -Message 'Finalizing files...' -Force
            } else {
                Write-Host $line
                Write-MegaProgress -Percent (Get-FallbackPercent -Dir $Dir) -Message 'Downloading...'
            }
        }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevPref
    }

    if ($exitCode -ne 0) {
        $err = (@($captured | Where-Object { $_ -match '^ERROR' }) -join "`n")
        if (-not $err) { $err = ($captured -join "`n") }
        if ($err -match '(?i)already exists') {
            Write-MegaLog 'Файли вже існують у цільовій папці (пропущено).' 'OK'
            return
        }
        if ($err -match '(?i)overquota|bandwidth|quota|509') { throw "Перевищено ліміт трафіку Mega.nz: $err" }
        if ($err -match '(?i)invalid|not found|enoent|expired|removed|decryption') { throw "Невалідне або видалене посилання Mega.nz: $err" }
        if ($err -match '(?i)network|connection|timed out|resolve|offline') { throw "Помилка мережі Mega.nz: $err" }
        throw "mega-get завершився з кодом $exitCode`: $err"
    }
}

try {
    if ($Url -notmatch '^https?://(www\.)?mega\.(nz|co\.nz)/(folder|file)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+' -and
        $Url -notmatch '^https?://(www\.)?mega\.(nz|co\.nz)/#F?![A-Za-z0-9_-]+![A-Za-z0-9_-]+') {
        throw "Невалідний формат посилання Mega.nz: '$Url'"
    }

    Write-MegaLog "URL       : $Url"
    Write-MegaLog "TargetDir : $TargetDir"

    $megaGetBat = Ensure-MegaCmd
    Invoke-MegaDownload -MegaGetBat $megaGetBat -MegaUrl $Url -Dir $TargetDir

    $files = @(Get-ChildItem -LiteralPath $TargetDir -Recurse -File)
    if ($files.Count -eq 0) { throw "У '$TargetDir' не знайдено жодного файлу після завантаження." }

    $totalMB = [Math]::Round(($files | Measure-Object -Property Length -Sum).Sum / 1MB, 2)
    Write-MegaProgress -Percent 100 -Message "Done ($($files.Count) files, $totalMB MB)" -Force
    Write-MegaLog "Готово: $($files.Count) файлів, $totalMB MB" 'OK'
    exit 0
} catch {
    Write-MegaLog $_.Exception.Message 'ERROR'
    exit 1
}
