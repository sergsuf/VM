<#
.SYNOPSIS
    Build a single self-extracting MaxAgent-Portable.exe for TeamViewer delivery.
.DESCRIPTION
    1. Builds the portable bundle via build_portable_client.ps1 (unless -BundleDir is provided).
    2. Compresses the bundle into MaxAgent.zip.
    3. Compiles packaging\MaxAgentSfxStub.cs using built-in Windows .NET Framework csc.exe
       with MaxAgent.zip embedded as a resource, producing a single MaxAgent-Portable.exe.
    When run on a client PC, MaxAgent-Portable.exe extracts into .\MaxAgent\ next to the .exe,
    picks up any adjacent connect.json, and launches MaxAgent\Start-MaxAgent.bat.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$OutExe = "dist\MaxAgent-Portable.exe",

    [Parameter(Mandatory = $false)]
    [string]$BundleDir = "",

    [Parameter(Mandatory = $false)]
    [string]$Version = "1.0.0",

    [Parameter(Mandatory = $false)]
    [string]$ConnectFile = "",

    [switch]$SkipDownloads
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutExeFull = [System.IO.Path]::GetFullPath($OutExe)
$OutParent = Split-Path -Parent $OutExeFull
if ($OutParent) {
    New-Item -ItemType Directory -Force -Path $OutParent | Out-Null
}

$TempWork = Join-Path $env:TEMP ("MaxAgentSfxBuild_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TempWork | Out-Null

try {
    if (-not $BundleDir) {
        $BundleDir = Join-Path $TempWork "bundle"
        $BuildScript = Join-Path $PSScriptRoot "build_portable_client.ps1"
        $BuildParams = @{
            OutDir  = $BundleDir
            Version = $Version
        }
        if ($ConnectFile) { $BuildParams["ConnectFile"] = $ConnectFile }
        if ($SkipDownloads) { $BuildParams["SkipDownloads"] = $true }
        & $BuildScript @BuildParams
    } else {
        $BundleDir = [System.IO.Path]::GetFullPath($BundleDir)
        if ($ConnectFile -and (Test-Path $ConnectFile)) {
            Copy-Item -Path $ConnectFile -Destination (Join-Path $BundleDir "connect.json") -Force
        }
    }

    $ZipPath = Join-Path $TempWork "MaxAgent.zip"
    if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
    Write-Host "[build_sfx_exe] Packing $BundleDir -> $ZipPath..."
    Compress-Archive -Path (Join-Path $BundleDir "*") -DestinationPath $ZipPath -CompressionLevel Optimal

    $CscCandidates = @(
        "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
    )
    $CscExe = $CscCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $CscExe) {
        throw "Built-in .NET csc.exe was not found in $env:WINDIR\Microsoft.NET."
    }

    $StubCs = Join-Path $PSScriptRoot "MaxAgentSfxStub.cs"
    Write-Host "[build_sfx_exe] Compiling single-file SFX -> $OutExeFull..."
    & $CscExe `
        /nologo `
        /target:exe `
        /optimize+ `
        /platform:anycpu `
        /out:"$OutExeFull" `
        /reference:System.IO.Compression.dll `
        /reference:System.IO.Compression.FileSystem.dll `
        /resource:"$ZipPath,MaxAgent.zip" `
        "$StubCs"

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $OutExeFull)) {
        throw "csc.exe failed with exit code $LASTEXITCODE"
    }

    $SizeMB = [Math]::Round((Get-Item $OutExeFull).Length / 1MB, 2)
    Write-Host "[build_sfx_exe] Done: $OutExeFull ($SizeMB MB)"
}
finally {
    if (Test-Path $TempWork) {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $TempWork
    }
}
