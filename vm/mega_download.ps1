#Requires -Version 5.1

<#
.SYNOPSIS
    Mega.nz downloader using official MEGAcmd (mega-get) without browser.
    Automatically installs MEGAcmd silently if missing, starts MEGAcmdServer
    detached (so stdout pipes never hang), streams progress in real time,
    handles errors, and optionally packs result to .zip.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Url,

    [Parameter(Mandatory = $true)]
    [string]$TargetDir,

    [switch]$AsZip,
    [string]$ZipName = 'mega_download.zip',
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$script:MegaCmdInstallerUrl = 'https://mega.nz/MEGAcmdSetup64.exe'

function Write-MegaLog {
    param(
        [string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR', 'OK')]
        [string]$Level = 'INFO'
    )
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Write-Host("[$ts] [MEGA][$Level] $Message")
}

function Test-MegaUrl {
    param([string]$Link)
    if ([string]::IsNullOrWhiteSpace($Link)) { return $false }
    return ($Link -match '^https?://(www\.)?mega\.(nz|co\.nz)/(folder|file)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+' -or
            $Link -match '^https?://(www\.)?mega\.(nz|co\.nz)/#F?![A-Za-z0-9_-]+![A-Za-z0-9_-]+')
}

function Ensure-MegaCmd {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'MEGAcmd'),
        (Join-Path ${env:ProgramFiles} 'MEGAcmd'),
        (Join-Path ${env:ProgramFiles(x86)} 'MEGAcmd')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ 'mega-get.bat') -PathType Leaf) }

    $megaDir = $candidates | Select-Object -First 1
    if (-not $megaDir) {
        Write-MegaLog "MEGAcmd не знайдено. Завантаження та тихе встановлення MEGAcmdSetup64.exe..." 'INFO'
        $instPath = Join-Path $env:TEMP 'MEGAcmdSetup64.exe'
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $script:MegaCmdInstallerUrl -OutFile $instPath -UseBasicParsing -TimeoutSec 180
        } catch {
            throw "Не вдалося завантажити MEGAcmd з $($script:MegaCmdInstallerUrl): $($_.Exception.Message)"
        }

        $p = Start-Process -FilePath $instPath -ArgumentList '/S' -Wait -PassThru
        Remove-Item -LiteralPath $instPath -Force -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0) {
            throw "Встановлення MEGAcmd завершилось з кодом $($p.ExitCode)"
        }

        $megaDir = Join-Path $env:LOCALAPPDATA 'MEGAcmd'
        if (-not (Test-Path -LiteralPath (Join-Path $megaDir 'mega-get.bat') -PathType Leaf)) {
            throw "Не знайдено mega-get.bat після встановлення у $megaDir"
        }
        Write-MegaLog "MEGAcmd успішно встановлено: $megaDir" 'OK'
    } else {
        Write-MegaLog "Знайдено MEGAcmd: $megaDir" 'INFO'
    }

    # Start MEGAcmdServer detached so it does not inherit caller stdout/stderr pipes
    $serverExe = Join-Path $megaDir 'MEGAcmdServer.exe'
    $running = Get-Process -Name 'MEGAcmdServer' -ErrorAction SilentlyContinue
    if (-not $running -and (Test-Path -LiteralPath $serverExe -PathType Leaf)) {
        Write-MegaLog "Запуск фонового MEGAcmdServer у detached-режимі..." 'INFO'
        Start-Process -FilePath $serverExe -WindowStyle Hidden
        Start-Sleep -Seconds 2
    }

    return (Join-Path $megaDir 'mega-get.bat')
}

function Invoke-MegaGetDownload {
    param(
        [string]$MegaGetBat,
        [string]$MegaUrl,
        [string]$DownloadDir
    )

    New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null

    $capturedLines = New-Object System.Collections.Generic.List[string]
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $MegaGetBat $MegaUrl $DownloadDir 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $_.Exception.Message
            } else {
                "$($_)"
            }
            if (-not [string]::IsNullOrWhiteSpace($line)) {
                $capturedLines.Add($line)
                Write-Host($line)
            }
        }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevPref
    }

    if ($exitCode -ne 0) {
        $errText = ($capturedLines -join "`n")
        if ($errText -match '(?i)already exists') {
            Write-MegaLog "Файли вже присутні у цільовій папці (перекачування пропущено)." 'OK'
            return
        }
        if ($errText -match '(?i)overquota|bandwidth|quota|509|transfer quota') {
            throw "Перевищено ліміт трафіку Mega.nz (Bandwidth/Quota limit). Спробуйте пізніше або через інший IP. Деталі: $errText"
        }
        if ($errText -match '(?i)invalid|not found|enoent|expired|removed|decryption') {
            throw "Невалідне або видалене посилання Mega.nz: $MegaUrl. Деталі: $errText"
        }
        if ($errText -match '(?i)network|connection|timed out|resolve|offline') {
            throw "Помилка мережі при підключенні до Mega.nz. Деталі: $errText"
        }
        throw "mega-get завершився з кодом $exitCode. Деталі: $errText"
    }
}

function Pack-FolderToZip {
    param(
        [string]$SourceDir,
        [string]$OutputZipPath
    )

    $files = @(Get-ChildItem -LiteralPath $SourceDir -Recurse -File)
    if ($files.Count -eq 0) {
        throw "У папці '$SourceDir' немає жодного файлу для пакування в ZIP."
    }

    if (Test-Path -LiteralPath $OutputZipPath) {
        Remove-Item -LiteralPath $OutputZipPath -Force
    }

    $tarExe = Get-Command 'tar.exe' -ErrorAction SilentlyContinue
    if ($tarExe) {
        Write-MegaLog "Пакування $($files.Count) файлів у ZIP через tar.exe -> $OutputZipPath" 'INFO'
        Push-Location $SourceDir
        try {
            & $tarExe.Source -a -c -f $OutputZipPath *
            if ($LASTEXITCODE -ne 0) {
                throw "tar.exe повернув код $LASTEXITCODE"
            }
        } finally {
            Pop-Location
        }
    } else {
        Write-MegaLog "Пакування $($files.Count) файлів у ZIP через Compress-Archive -> $OutputZipPath" 'INFO'
        Compress-Archive -Path (Join-Path $SourceDir '*') -DestinationPath $OutputZipPath -CompressionLevel Optimal -Force
    }
}

# --- Main ---
try {
    Write-MegaLog "URL       : $Url" 'INFO'
    Write-MegaLog "TargetDir : $TargetDir" 'INFO'
    Write-MegaLog "AsZip     : $($AsZip.IsPresent)" 'INFO'

    if (-not (Test-MegaUrl -Link $Url)) {
        throw "Невалідний формат посилання Mega.nz: '$Url'"
    }

    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null

    $zipOutputPath = Join-Path $TargetDir $ZipName
    if ($AsZip -and (-not $Force) -and (Test-Path -LiteralPath $zipOutputPath -PathType Leaf)) {
        $existingZip = Get-Item -LiteralPath $zipOutputPath
        if ($existingZip.Length -gt 0) {
            $sizeMB = [Math]::Round($existingZip.Length / 1MB, 2)
            Write-MegaLog "ZIP-архів вже існує ($sizeMB MB): $zipOutputPath. Пропуск завантаження (використайте -Force для перезавантаження)." 'OK'
            exit 0
        }
    }

    $megaGetBat = Ensure-MegaCmd
    $downloadSubDir = if ($AsZip) { Join-Path $TargetDir '_files' } else { $TargetDir }

    Write-MegaLog "Запуск скачування з Mega.nz у '$downloadSubDir'..." 'INFO'
    Invoke-MegaGetDownload -MegaGetBat $megaGetBat -MegaUrl $Url -DownloadDir $downloadSubDir

    $downloadedFiles = @(Get-ChildItem -LiteralPath $downloadSubDir -Recurse -File)
    if ($downloadedFiles.Count -eq 0) {
        throw "Скачування завершилось, але у '$downloadSubDir' не знайдено жодного файлу."
    }

    $totalBytes = ($downloadedFiles | Measure-Object -Property Length -Sum).Sum
    $totalMB = [Math]::Round($totalBytes / 1MB, 2)
    Write-MegaLog "Скачано файлів: $($downloadedFiles.Count), загальний розмір: $totalMB MB" 'OK'

    if ($AsZip) {
        Pack-FolderToZip -SourceDir $downloadSubDir -OutputZipPath $zipOutputPath
        $zipInfo = Get-Item -LiteralPath $zipOutputPath
        $zipMB = [Math]::Round($zipInfo.Length / 1MB, 2)
        Write-MegaLog "Готово! ZIP-архів створено: $zipOutputPath ($zipMB MB)" 'OK'
    }

    exit 0
} catch {
    Write-MegaLog $_.Exception.Message 'ERROR'
    exit 1
}
