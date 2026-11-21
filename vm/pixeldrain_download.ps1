#Requires -Version 5.1

<#
.SYNOPSIS
    Zero-dependency Pixeldrain file downloader for Windows PowerShell 5.1.
    Fetches file metadata via Pixeldrain API, streams the file to disk,
    and displays smooth monotonic % progress in the console.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$TargetDir,
    [string]$FileName = '',
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$script:MaxPercent = 0
$script:LastReportedPercent = -1

function Write-PdLog {
    param([string]$Message, [ValidateSet('INFO', 'ERROR', 'OK')][string]$Level = 'INFO')
    Write-Host ("[{0:yyyy-MM-dd HH:mm:ss}] [PIXELDRAIN][{1}] {2}" -f (Get-Date), $Level, $Message)
}

function Write-PdProgress {
    param([int]$Percent, [string]$Message, [switch]$ForcePrint)
    $Percent = [Math]::Max($script:MaxPercent, [Math]::Min(100, $Percent))
    $script:MaxPercent = $Percent

    if ($ForcePrint -or ($Percent -ne $script:LastReportedPercent)) {
        $script:LastReportedPercent = $Percent
        $filled = [int][Math]::Floor(30 * $Percent / 100)
        $bar = ('#' * $filled) + ('-' * (30 - $filled))
        Write-Host ("[{0}] {1,3}% | {2}" -f $bar, $Percent, $Message)
    }
}

function Get-SafeFileName {
    param([string]$Name, [string]$Fallback)
    $candidate = if ([string]::IsNullOrWhiteSpace($Name)) { $Fallback } else { $Name.Trim() }
    foreach ($ch in [System.IO.Path]::GetInvalidFileNameChars()) {
        $candidate = $candidate.Replace($ch, '_')
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) { return $Fallback }
    return $candidate
}

function Get-PixeldrainId {
    param([string]$InputUrl)
    if ($InputUrl -match '^https?://(?:www\.)?pixeldrain\.com/(?:u|api/file)/([A-Za-z0-9_-]+)') {
        return $Matches[1]
    }
    throw "Невалідний формат посилання Pixeldrain: '$InputUrl'"
}

function Get-FileNameFromContentDisposition {
    param([string]$HeaderValue)
    if ([string]::IsNullOrWhiteSpace($HeaderValue)) { return '' }
    if ($HeaderValue -match "filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)") {
        return [System.Uri]::UnescapeDataString($Matches[1].Trim('"', ' '))
    }
    if ($HeaderValue -match 'filename\s*=\s*"([^"]+)"') {
        return $Matches[1]
    }
    if ($HeaderValue -match 'filename\s*=\s*([^;]+)') {
        return $Matches[1].Trim()
    }
    return ''
}

try {
    $fileId = Get-PixeldrainId -InputUrl $Url
    Write-PdLog "URL       : $Url"
    Write-PdLog "File ID   : $fileId"
    Write-PdLog "TargetDir : $TargetDir"

    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null

    $infoUrl = "https://pixeldrain.com/api/file/$fileId/info"
    $downloadUrl = "https://pixeldrain.com/api/file/$fileId`?download"

    $expectedSize = [long]0
    $apiName = ''
    try {
        $info = Invoke-RestMethod -Uri $infoUrl -Method Get -UseBasicParsing -TimeoutSec 30
        if ($null -ne $info) {
            if (($info.PSObject.Properties.Name -contains 'success') -and ($info.success -eq $false)) {
                $errMsg = if ($info.message) { $info.message } else { $info.value }
                throw "Pixeldrain API повернув помилку: $errMsg"
            }
            if ($info.name) { $apiName = [string]$info.name }
            if ($info.size) { $expectedSize = [long]$info.size }
        }
    } catch [System.Net.WebException] {
        $statusCode = 0
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        if ($statusCode -eq 404) {
            throw "Файл не знайдено на Pixeldrain (404): '$Url'"
        }
        Write-PdLog "Не вдалося отримати метадані /info (HTTP $statusCode), продовжую пряме завантаження..." 'INFO'
    }

    $resolvedName = Get-SafeFileName -Name $(if ($FileName) { $FileName } else { $apiName }) -Fallback "$fileId.bin"
    $targetPath = Join-Path $TargetDir $resolvedName

    if ((-not $Force) -and (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
        $existingLen = (Get-Item -LiteralPath $targetPath).Length
        if (($expectedSize -le 0) -or ($existingLen -eq $expectedSize)) {
            $mb = [Math]::Round($existingLen / 1MB, 2)
            Write-PdProgress -Percent 100 -Message "Already exists ($resolvedName, $mb MB)" -ForcePrint
            Write-PdLog "Файл вже існує: '$targetPath' (пропущено)." 'OK'
            exit 0
        }
    }

    Write-PdProgress -Percent 1 -Message "Connecting to Pixeldrain..." -ForcePrint

    $req = [System.Net.HttpWebRequest]::Create($downloadUrl)
    $req.Method = 'GET'
    $req.UserAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    $req.AllowAutoRedirect = $true
    $req.Timeout = 30000
    $req.ReadWriteTimeout = 60000

    $resp = $req.GetResponse()
    $tempPath = "$targetPath.part"
    try {
        $cdName = Get-FileNameFromContentDisposition -HeaderValue $resp.Headers['Content-Disposition']
        if ((-not $FileName) -and (-not $apiName) -and $cdName) {
            $resolvedName = Get-SafeFileName -Name $cdName -Fallback $resolvedName
            $targetPath = Join-Path $TargetDir $resolvedName
            $tempPath = "$targetPath.part"
        }

        $totalBytes = if ($resp.ContentLength -gt 0) { [long]$resp.ContentLength } else { $expectedSize }
        $inStream = $resp.GetResponseStream()
        $outStream = [System.IO.File]::Open($tempPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try {
            $buffer = New-Object byte[] 65536
            [long]$downloaded = 0
            while (($read = $inStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $outStream.Write($buffer, 0, $read)
                $downloaded += $read
                if ($totalBytes -gt 0) {
                    $pct = [int][Math]::Floor(($downloaded * 99.0) / $totalBytes)
                    $dlMB = [Math]::Round($downloaded / 1MB, 2)
                    $totMB = [Math]::Round($totalBytes / 1MB, 2)
                    Write-PdProgress -Percent ([Math]::Max(1, $pct)) -Message "$dlMB / $totMB MB"
                } else {
                    $dlMB = [Math]::Round($downloaded / 1MB, 2)
                    Write-PdProgress -Percent 50 -Message "$dlMB MB"
                }
            }
        } finally {
            $outStream.Close()
            $inStream.Close()
        }
    } finally {
        $resp.Close()
    }

    if (-not (Test-Path -LiteralPath $tempPath -PathType Leaf)) {
        throw "Тимчасовий файл не створено: '$tempPath'"
    }

    Move-Item -LiteralPath $tempPath -Destination $targetPath -Force
    $finalSize = (Get-Item -LiteralPath $targetPath).Length
    if ($finalSize -le 0) {
        throw "Завантажений файл порожній (0 байт): '$targetPath'"
    }

    $totalMB = [Math]::Round($finalSize / 1MB, 2)
    Write-PdProgress -Percent 100 -Message "Done ($resolvedName, $totalMB MB)" -ForcePrint
    Write-PdLog "Готово: '$targetPath' ($totalMB MB)" 'OK'
    exit 0
} catch {
    Write-PdLog $_.Exception.Message 'ERROR'
    exit 1
}
