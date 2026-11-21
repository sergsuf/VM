<#
.SYNOPSIS
    Build a self-contained portable MaxAgent client bundle for Windows (F-19, F-20).
.DESCRIPTION
    Stages PSF-signed Windows python-embed (3.12.x), configures python312._pth with
    Lib\site-packages and uncommented `import site`, installs Python runtime packages,
    bundles AutoHotkey64.exe, copies Start-MaxAgent.bat and runtime\bootstrap.py, and
    populates app\releases\<Version>\ + app\active.json.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$OutDir = "dist\MaxAgent-Portable",

    [Parameter(Mandatory = $false)]
    [string]$Version = "1.0.0",

    [Parameter(Mandatory = $false)]
    [string]$RuntimeVersion = "1.0.0",

    [Parameter(Mandatory = $false)]
    [string]$PythonEmbedVersion = "3.12.8",

    [Parameter(Mandatory = $false)]
    [string]$AutoHotkeyVersion = "2.0.18",

    [Parameter(Mandatory = $false)]
    [string]$ConnectFile = "",

    [switch]$SkipDownloads
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BundleRoot = [System.IO.Path]::GetFullPath($OutDir)

$RuntimeDir = Join-Path $BundleRoot "runtime"
$SitePackages = Join-Path $RuntimeDir "Lib\site-packages"
$AhkDir = Join-Path $BundleRoot "tools\ahk"
$AppDir = Join-Path $BundleRoot "app"
$ReleaseDir = Join-Path $AppDir "releases\$Version"

New-Item -ItemType Directory -Force -Path $RuntimeDir, $SitePackages, $AhkDir, $ReleaseDir | Out-Null

# 1. Stage Windows python-embed + pip packages
if (-not $SkipDownloads) {
    $PyZipUrl = "https://www.python.org/ftp/python/$PythonEmbedVersion/python-$PythonEmbedVersion-embed-amd64.zip"
    $PyZipPath = Join-Path $env:TEMP "python-$PythonEmbedVersion-embed-amd64.zip"
    Write-Host "[build_portable_client] Downloading $PyZipUrl..."
    Invoke-WebRequest -Uri $PyZipUrl -OutFile $PyZipPath -UseBasicParsing
    Expand-Archive -Path $PyZipPath -DestinationPath $RuntimeDir -Force
}

$PthPath = Join-Path $RuntimeDir "python312._pth"
$PthLines = @(
    "python312.zip",
    ".",
    "Lib\site-packages",
    "import site"
)
Set-Content -Path $PthPath -Value $PthLines -Encoding ASCII

$RuntimePython = Join-Path $RuntimeDir "python.exe"
if (-not $SkipDownloads -and (Test-Path $RuntimePython)) {
    $GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
    $GetPipPath = Join-Path $env:TEMP "get-pip.py"
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipPath -UseBasicParsing
    & $RuntimePython $GetPipPath --no-warn-script-location
    & $RuntimePython -m pip install --no-warn-script-location --target $SitePackages PyYAML click Pillow psutil uiautomation
}

# 2. Stage AutoHotkey64.exe
if (-not $SkipDownloads) {
    $AhkZipUrl = "https://www.autohotkey.com/download/2.0/AutoHotkey_$AutoHotkeyVersion.zip"
    $AhkZipPath = Join-Path $env:TEMP "AutoHotkey_$AutoHotkeyVersion.zip"
    $AhkTmpDir = Join-Path $env:TEMP "ahk_extract_$AutoHotkeyVersion"
    Write-Host "[build_portable_client] Downloading $AhkZipUrl..."
    Invoke-WebRequest -Uri $AhkZipUrl -OutFile $AhkZipPath -UseBasicParsing
    if (Test-Path $AhkTmpDir) { Remove-Item -Recurse -Force $AhkTmpDir }
    Expand-Archive -Path $AhkZipPath -DestinationPath $AhkTmpDir -Force
    $AhkExe = Get-ChildItem -Path $AhkTmpDir -Filter "AutoHotkey64.exe" -Recurse | Select-Object -First 1
    if (-not $AhkExe) {
        throw "AutoHotkey64.exe not found inside $AhkZipUrl"
    }
    Copy-Item -Path $AhkExe.FullName -Destination (Join-Path $AhkDir "AutoHotkey64.exe") -Force
}

# 3. Copy Start-MaxAgent.bat, runtime\bootstrap.py, and runtime\version.json
Copy-Item -Path (Join-Path $RepoRoot "packaging\Start-MaxAgent.bat") -Destination (Join-Path $BundleRoot "Start-MaxAgent.bat") -Force
Copy-Item -Path (Join-Path $RepoRoot "remote\client\bootstrap.py") -Destination (Join-Path $RuntimeDir "bootstrap.py") -Force

@{
    runtime_version = $RuntimeVersion
    app_version     = $Version
} | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $RuntimeDir "version.json") -Encoding UTF8

# 4. Stage app\releases\<Version>\ and app\active.json
$BundleFolders = @("remote", "cli", "ahk", "config", "max_scripts", "vm")
foreach ($Folder in $BundleFolders) {
    $Src = Join-Path $RepoRoot $Folder
    if (Test-Path $Src) {
        $Dst = Join-Path $ReleaseDir $Folder
        Copy-Item -Path $Src -Destination $Dst -Recurse -Force
        Get-ChildItem -Path $Dst -Include "__pycache__", "*.pyc", "*.pyo" -Recurse -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
}
Set-Content -Path (Join-Path $ReleaseDir "VERSION.txt") -Value $Version -Encoding ASCII

@{
    version          = $Version
    previous         = $null
    pending_verify   = $false
    attempts         = 0
    blocked_versions = @()
} | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $AppDir "active.json") -Encoding UTF8

if ($ConnectFile -and (Test-Path $ConnectFile)) {
    Copy-Item -Path $ConnectFile -Destination (Join-Path $BundleRoot "connect.json") -Force
}

Write-Host "[build_portable_client] Portable bundle ready at $BundleRoot"
