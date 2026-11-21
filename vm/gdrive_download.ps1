#Requires -Version 5.1

<#
.SYNOPSIS
    Zero-dependency Google Drive file downloader for Windows PowerShell 5.1.
    Supports both direct downloads (small files) and virus-scan confirmation
    bypass (large files via drive.usercontent.google.com), with smooth % progress.
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

function Write-GdLog {
    param([string]$Message, [ValidateSet('INFO', 'ERROR', 'OK')][string]$Level = 'INFO')
    Write-Host ("[{0:yyyy-MM-dd HH:mm:ss}] [GDRIVE][{1}] {2}" -f (Get-Date), $Level, $Message)
}

function Write-GdProgress {
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

function Get-GoogleDriveId {
    param([string]$InputUrl)
    if ($InputUrl -match 'drive\.google\.com/file/d/([A-Za-z0-9_-]+)') { return $Matches[1] }
    if ($InputUrl -match 'docs\.google\.com/[^/]+/d/([A-Za-z0-9_-]+)') { return $Matches[1] }
    if ($InputUrl -match '[?&]id=([A-Za-z0-9_-]+)') { return $Matches[1] }
    if ($InputUrl -match 'drive\.google\.com/drive/folders/([A-Za-z0-9_-]+)') {
        throw "Посилання вказує на папку Google Drive, а не на файл. Для завантаження без Python передайте посилання на файл/архів."
    }
    throw "Невалідний формат посилання Google Drive: '$InputUrl'"
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

function New-GdRequest {
    param([string]$RequestUrl, [System.Net.CookieContainer]$Cookies)
    $req = [System.Net.HttpWebRequest]::Create($RequestUrl)
    $req.Method = 'GET'
    $req.CookieContainer = $Cookies
    $req.UserAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    $req.AllowAutoRedirect = $true
    $req.Timeout = 30000
    $req.ReadWriteTimeout = 60000
    return $req
}

function Read-ResponseString {
    param([System.Net.HttpWebResponse]$Response)
    $stream = $Response.GetResponseStream()
    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
    try {
        return $reader.ReadToEnd()
    } finally {
        $reader.Close()
        $stream.Close()
    }
}

function Get-ConfirmUrlFromHtml {
    param([string]$Html, [string]$FileId)

    if ($Html -match '(?i)Too many users have viewed or downloaded this file recently') {
        throw "Перевищено ліміт скачувань Google Drive (quota exceeded). Спробуйте пізніше або використайте інше посилання."
    }
    if ($Html -match '(?i)You need access|Request access') {
        throw "Немає доступу до файлу Google Drive (потрібен публічний доступ 'Anyone with the link')."
    }
    if ($Html -match '(?i)Sorry, the file you have requested does not exist') {
        throw "Файл не знайдено на Google Drive (404 / видалено)."
    }

    $formAction = 'https://drive.usercontent.google.com/download'
    if ($Html -match '<form[^>]+id=["'']download-form["''][^>]+action=["'']([^"'']+)["'']') {
        $formAction = [System.Net.WebUtility]::HtmlDecode($Matches[1])
    } elseif ($Html -match '<form[^>]+action=["'']([^"'']*usercontent\.google\.com/download[^"'']*)["'']') {
        $formAction = [System.Net.WebUtility]::HtmlDecode($Matches[1])
    }

    $params = New-Object System.Collections.Generic.List[string]
    $inputMatches = [regex]::Matches($Html, '<input\b[^>]*>', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    foreach ($m in $inputMatches) {
        $tag = $m.Value
        if ($tag -notmatch 'type=["'']?hidden["'']?') { continue }
        $nameMatch = [regex]::Match($tag, 'name=["'']([^"'']+)["'']', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        $valMatch = [regex]::Match($tag, 'value=["'']([^"'']*)["'']', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if ($nameMatch.Success -and $valMatch.Success) {
            $k = [System.Uri]::EscapeDataString([System.Net.WebUtility]::HtmlDecode($nameMatch.Groups[1].Value))
            $v = [System.Uri]::EscapeDataString([System.Net.WebUtility]::HtmlDecode($valMatch.Groups[1].Value))
            $params.Add("$k=$v")
        }
    }

    if ($params.Count -gt 0) {
        $sep = if ($formAction -match '\?') { '&' } else { '?' }
        return "$formAction$sep$($params -join '&')"
    }

    if ($Html -match 'confirm=([0-9A-Za-z_-]+)') {
        return "https://drive.usercontent.google.com/download?id=$FileId&export=download&confirm=$($Matches[1])"
    }

    throw "Не вдалося знайти посилання підтвердження на сторінці Google Drive."
}

function Save-ResponseToFile {
    param(
        [System.Net.HttpWebResponse]$Response,
        [string]$TempPath,
        [long]$KnownSize = 0
    )

    $totalBytes = if ($Response.ContentLength -gt 0) { [long]$Response.ContentLength } else { $KnownSize }
    $inStream = $Response.GetResponseStream()
    $outStream = [System.IO.File]::Open($TempPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
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
                Write-GdProgress -Percent ([Math]::Max(1, $pct)) -Message "$dlMB / $totMB MB"
            } else {
                $dlMB = [Math]::Round($downloaded / 1MB, 2)
                Write-GdProgress -Percent 50 -Message "$dlMB MB"
            }
        }
    } finally {
        $outStream.Close()
        $inStream.Close()
    }
}

try {
    $fileId = Get-GoogleDriveId -InputUrl $Url
    Write-GdLog "URL       : $Url"
    Write-GdLog "File ID   : $fileId"
    Write-GdLog "TargetDir : $TargetDir"

    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    Write-GdProgress -Percent 1 -Message 'Connecting to Google Drive...' -ForcePrint

    $cookies = New-Object System.Net.CookieContainer
    $initialUrl = "https://drive.google.com/uc?export=download&id=$fileId"

    $req1 = New-GdRequest -RequestUrl $initialUrl -Cookies $cookies
    $resp = $req1.GetResponse()

    $htmlName = ''
    $cdHeader = $resp.Headers['Content-Disposition']
    $contentType = [string]$resp.ContentType

    if (($contentType -match '(?i)text/html') -and (-not $cdHeader)) {
        try {
            $html = Read-ResponseString -Response $resp
        } finally {
            $resp.Close()
        }

        if ($html -match '<span[^>]+class=["'']uc-name-size["''][^>]*>\s*<a[^>]*>([^<]+)</a>') {
            $htmlName = [System.Net.WebUtility]::HtmlDecode($Matches[1])
        }

        $confirmUrl = Get-ConfirmUrlFromHtml -Html $html -FileId $fileId
        Write-GdLog "Виявлено сторінку підтвердження, переходжу до прямого завантаження..." 'INFO'
        Write-GdProgress -Percent 2 -Message 'Confirmed download, streaming...' -ForcePrint

        $req2 = New-GdRequest -RequestUrl $confirmUrl -Cookies $cookies
        $resp = $req2.GetResponse()
        $cdHeader = $resp.Headers['Content-Disposition']
        $contentType = [string]$resp.ContentType

        if (($contentType -match '(?i)text/html') -and (-not $cdHeader)) {
            $errHtml = Read-ResponseString -Response $resp
            $resp.Close()
            if ($errHtml -match '(?i)Too many users have viewed or downloaded') {
                throw "Перевищено ліміт скачувань Google Drive (quota exceeded)."
            }
            throw "Google Drive повернув HTML замість файлу після підтвердження."
        }
    }

    $tempPath = Join-Path $TargetDir "$fileId.part"
    try {
        $cdName = Get-FileNameFromContentDisposition -HeaderValue $cdHeader
        $rawName = if ($FileName) { $FileName } elseif ($cdName) { $cdName } elseif ($htmlName) { $htmlName } else { "$fileId.bin" }
        $resolvedName = Get-SafeFileName -Name $rawName -Fallback "$fileId.bin"
        $targetPath = Join-Path $TargetDir $resolvedName

        if ((-not $Force) -and (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
            $existingLen = (Get-Item -LiteralPath $targetPath).Length
            $respLen = [long]$resp.ContentLength
            if (($respLen -le 0) -or ($existingLen -eq $respLen)) {
                $mb = [Math]::Round($existingLen / 1MB, 2)
                Write-GdProgress -Percent 100 -Message "Already exists ($resolvedName, $mb MB)" -ForcePrint
                Write-GdLog "Файл вже існує: '$targetPath' (пропущено)." 'OK'
                exit 0
            }
        }

        Save-ResponseToFile -Response $resp -TempPath $tempPath
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
    Write-GdProgress -Percent 100 -Message "Done ($resolvedName, $totalMB MB)" -ForcePrint
    Write-GdLog "Готово: '$targetPath' ($totalMB MB)" 'OK'
    exit 0
} catch {
    Write-GdLog $_.Exception.Message 'ERROR'
    exit 1
}
