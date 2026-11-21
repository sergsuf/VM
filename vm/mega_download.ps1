#Requires -Version 5.1

<#
.SYNOPSIS
    Portable Mega.nz downloader using megatools.exe (no browser required).
    Supports public file/folder links, real-time output streaming, resume/skip,
    error classification, and optional local .zip packaging.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Url,

    [Parameter(Mandatory = $true)]
    [string]$TargetDir,

    [switch]$AsZip,
    [string]$ZipName = 'mega_download.zip',
    [switch]$Force,
    [string]$MegatoolsDir = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$script:MegatoolsVersion = '1.12.2.20261116'
$script:MegatoolsZipUrl = "https://xff.cz/megatools/builds/builds/megatools-$($script:MegatoolsVersion)-win64.zip"

function Write-MegaLog {
    param(
        [string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR', 'OK')]
        [string]$Level = 'INFO'
    )
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Write-Host "[$ts] [MEGA][$Level] $Message"
}

function Test-MegaUrl {
    param([string]$Link)
    if ([string]::IsNullOrWhiteSpace($Link)) { return $false }
    return ($Link -match '^https?://(www\.)?mega\.(nz|co\.nz)/(folder|file)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+' -or
            $Link -match '^https?://(www\.)?mega\.(nz|co\.nz)/#F?![A-Za-z0-9_-]+![A-Za-z0-9_-]+')
}

function Ensure-Megatools {
    param([string]$ToolsBaseDir)

    if (-not $ToolsBaseDir) {
        $repoRoot = Split-Path -Parent $PSScriptRoot
        $ToolsBaseDir = Join-Path $repoRoot 'tools\megatools'
    }

    $exePath = Join-Path $ToolsBaseDir 'megatools.exe'
    if (Test-Path -LiteralPath $exePath -PathType Leaf) {
        Write-MegaLog "Знайдено портативний megatools: $exePath" 'INFO'
        return $exePath
    }

    Write-MegaLog "megatools.exe не знайдено. Завантаження портативної версії $($script:MegatoolsVersion)..." 'INFO'
    New-Item -ItemType Directory -Path $ToolsBaseDir -Force | Out-Null

    $zipTemp = Join-Path $ToolsBaseDir 'megatools-win64.zip'
    $extractTemp = Join-Path $ToolsBaseDir '_extract'

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $script:MegatoolsZipUrl -OutFile $zipTemp -UseBasicParsing -TimeoutSec 120
    } catch {
        throw "Не вдалося завантажити megatools з $($script:MegatoolsZipUrl): $($_.Exception.Message)"
    }

    if (Test-Path -LiteralPath $extractTemp) {
        Remove-Item -LiteralPath $extractTemp -Recurse -Force
    }
    Expand-Archive -LiteralPath $zipTemp -DestinationPath $extractTemp -Force

    $foundExe = Get-ChildItem -LiteralPath $extractTemp -Filter 'megatools.exe' -Recurse -File | Select-Object -First 1
    if (-not $foundExe) {
        throw "У завантаженому архіві megatools не знайдено megatools.exe."
    }

    Copy-Item -LiteralPath $foundExe.FullName -Destination $exePath -Force
    Remove-Item -LiteralPath $zipTemp -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $extractTemp -Recurse -Force -ErrorAction SilentlyContinue

    Write-MegaLog "megatools.exe успішно встановлено: $exePath" 'OK'
    return $exePath
}

function Invoke-MegatoolsDownload {
    param(
        [string]$ExePath,
        [string]$MegaUrl,
        [string]$DownloadDir
    )

    New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $ExePath
    $psi.Arguments = "dl --path `"$DownloadDir`" `"$MegaUrl`""
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    $capturedErrors = New-Object System.Collections.Generic.List[string]
    $lastProgressLine = ''

    $outAction = {
        param($sender, $e)
        if ($null -ne $e.Data -and $e.Data.Trim() -ne '') {
            Write-Host $e.Data
        }
    }
    $errAction = {
        param($sender, $e)
        if ($null -ne $e.Data -and $e.Data.Trim() -ne '') {
            Write-Host "[STDERR] $($e.Data)"
            $Event.MessageData.Add($e.Data)
        }
    }

    $outEvent = Register-ObjectEvent -InputObject $proc -EventName 'OutputDataReceived' -Action $outAction
    $errEvent = Register-ObjectEvent -InputObject $proc -EventName 'ErrorDataReceived' -Action $errAction -MessageData $capturedErrors

    try {
        [void]$proc.Start()
        $proc.BeginOutputReadLine()
        $proc.BeginErrorReadLine()
        $proc.WaitForExit()
    } finally {
        Unregister-Event -SourceIdentifier $outEvent.Name -ErrorAction SilentlyContinue
        Unregister-Event -SourceIdentifier $errEvent.Name -ErrorAction SilentlyContinue
        Remove-Job -Id $outEvent.Id -Force -ErrorAction SilentlyContinue
        Remove-Job -Id $errEvent.Id -Force -ErrorAction SilentlyContinue
    }

    $exitCode = $proc.ExitCode
    $errText = ($capturedErrors -join "`n")

    if ($exitCode -ne 0) {
        if ($errText -match '(?i)509|bandwidth|quota|overquota') {
            throw "Перевищено ліміт трафіку Mega.nz (Bandwidth/Quota limit). Спробуйте пізніше або через інший IP. Деталі: $errText"
        }
        if ($errText -match '(?i)ENOENT|not found|invalid|parse|malformed|key') {
            throw "Невалідне або видалене посилання Mega.nz: $MegaUrl. Деталі: $errText"
        }
        if ($errText -match '(?i)could not resolve|timed out|connection|network|ssl|tls') {
            throw "Помилка мережі при підключенні до Mega.nz. Деталі: $errText"
        }
        throw "megatools завершився з кодом $exitCode. Деталі: $errText"
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

    $megatoolsExe = Ensure-Megatools -ToolsBaseDir $MegatoolsDir
    $downloadSubDir = if ($AsZip) { Join-Path $TargetDir '_files' } else { $TargetDir }

    Write-MegaLog "Запуск скачування з Mega.nz у '$downloadSubDir'..." 'INFO'
    Invoke-MegatoolsDownload -ExePath $megatoolsExe -MegaUrl $Url -DownloadDir $downloadSubDir

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
