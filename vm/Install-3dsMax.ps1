#Requires -Version 5.1

<#
.SYNOPSIS
    Silent installer for Autodesk 3ds Max (2024-2028) with automatic ISO discovery
    across Z:\3dsMax\Install and C:\..F:\ (root + \Install), multi-version selection,
    and Browse fallback.
#>

[CmdletBinding()]
param(
    [ValidateSet('2024', '2025', '2026', '2027', '2028')]
    [string]$Version,
    [string]$IsoPath,
    [string[]]$InstallTasks,
    [int]$InstallerTimeoutSeconds = 1800,
    [int]$StatusRefreshSeconds = 2,
    [switch]$SelectIso,
    [switch]$Interactive,
    [switch]$KeepIsoMounted
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:SupportedVersions = @('2024', '2025', '2026', '2027', '2028')
$script:StatusDisplayTop = $null
$script:StatusSpinnerPhase = 0
$script:ActiveDisplayName = 'Autodesk 3ds Max'
$script:ActiveLogPath = 'C:\Install\3dsMax-install.log'
$script:ActiveStatusPath = 'C:\Install\3dsMax-status.txt'

function Get-VersionConfig {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('2024', '2025', '2026', '2027', '2028')]
        [string]$TargetVersion
    )
    return @{
        Version          = $TargetVersion
        DisplayName      = "Autodesk 3ds Max $TargetVersion"
        MaxExecutable    = "C:\Program Files\Autodesk\3ds Max $TargetVersion\3dsmax.exe"
        LogPath          = "C:\Install\3dsMax-$TargetVersion-install.log"
        StatusPath       = "C:\Install\3dsMax-$TargetVersion-status.txt"
        TempInstallerLog = "3dsMax_${TargetVersion}_install.log"
        EstimatedFiles   = 19000
    }
}

function Get-VersionFromFileName {
    param([string]$FilePath)
    if (-not $FilePath) { return $null }
    $name = [System.IO.Path]::GetFileName($FilePath)
    if ($name -match '(?<!\d)(202[4-8])(?!\d)') {
        return $Matches[1]
    }
    return $null
}

function Get-IsoSearchDirectories {
    $candidates = New-Object System.Collections.Generic.List[string]
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)

    $rawDirs = New-Object System.Collections.Generic.List[string]
    if ($PSScriptRoot) { $rawDirs.Add($PSScriptRoot) }
    $rawDirs.Add('Z:\3dsMax\Install')
    $rawDirs.Add('Z:\')

    foreach ($driveLetter in @('C', 'D', 'E', 'F')) {
        $rawDirs.Add("${driveLetter}:\Install")
        $rawDirs.Add("${driveLetter}:\")
    }

    foreach ($dir in $rawDirs) {
        if (-not $dir) { continue }
        $normalized = $dir.TrimEnd('\')
        if ($normalized.Length -eq 2 -and $normalized[1] -eq ':') {
            $normalized = "$normalized\"
        }
        if ($seen.Add($normalized) -and (Test-Path -LiteralPath $normalized -PathType Container -ErrorAction SilentlyContinue)) {
            $candidates.Add($normalized)
        }
    }

    return $candidates.ToArray()
}

function Find-Available3dsMaxIsos {
    $found = New-Object System.Collections.Generic.List[object]
    $seenPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)

    foreach ($dir in (Get-IsoSearchDirectories)) {
        $isoFiles = Get-ChildItem -LiteralPath $dir -Filter '*.iso' -File -ErrorAction SilentlyContinue
        foreach ($file in $isoFiles) {
            if ($file.Name -notmatch '(?i)3ds[\s._-]*max') { continue }
            $ver = Get-VersionFromFileName -FilePath $file.Name
            if (-not $ver -or ($script:SupportedVersions -notcontains $ver)) { continue }
            if (-not $seenPaths.Add($file.FullName)) { continue }

            $cfg = Get-VersionConfig -TargetVersion $ver
            $found.Add([PSCustomObject]@{
                Version     = $ver
                DisplayName = $cfg.DisplayName
                IsoPath     = $file.FullName
                FileName    = $file.Name
                SizeGB      = [Math]::Round($file.Length / 1GB, 2)
                IsInstalled = (Test-Path -LiteralPath $cfg.MaxExecutable -PathType Leaf)
            })
        }
    }

    return @($found | Sort-Object -Property Version, IsoPath)
}

function Write-Status {
    param(
        [ValidateRange(0, 100)][int]$Percent,
        [string]$Message
    )

    $statusDirectory = Split-Path -Parent $script:ActiveStatusPath
    if ($statusDirectory -and -not (Test-Path -LiteralPath $statusDirectory)) {
        New-Item -ItemType Directory -Path $statusDirectory -Force | Out-Null
    }
    Set-Content -LiteralPath $script:ActiveStatusPath -Value "$Percent%|$Message" -Encoding UTF8

    try {
        $width = [Math]::Max(10, [Math]::Min(40, [Console]::WindowWidth - 35))
        if ($null -eq $script:StatusDisplayTop) {
            $script:StatusDisplayTop = [Math]::Max(0, [Console]::CursorTop)
        }
    } catch {
        $width = 30
    }

    $filled = [int][Math]::Floor($width * $Percent / 100)
    $bar = ('#' * $filled) + ('-' * ($width - $filled))
    $title = "Installing $($script:ActiveDisplayName)..."

    if ($Message -match '\((\d+) files\)$') {
        $spinner = @('/', '-', '\', '|')[$script:StatusSpinnerPhase]
        $status = ("[{0}] {1,3}%  {2}  Files: {3}" -f $bar, $Percent, $spinner, $Matches[1])
        $script:StatusSpinnerPhase = ($script:StatusSpinnerPhase + 1) % 4
    } else {
        $status = ("[{0}] {1,3}%  Status: {2}" -f $bar, $Percent, $Message)
    }

    try {
        [Console]::SetCursorPosition(0, [Math]::Min($script:StatusDisplayTop, [Console]::WindowHeight - 2))
        [Console]::Write($title.PadRight([Console]::WindowWidth - 1))
        [Console]::SetCursorPosition(0, [Math]::Min($script:StatusDisplayTop + 1, [Console]::WindowHeight - 1))
        [Console]::Write($status.PadRight([Console]::WindowWidth - 1))
        $cursorTop = [Math]::Min($script:StatusDisplayTop + 1, [Console]::WindowHeight - 1)
        [Console]::SetCursorPosition([Math]::Min($status.Length, [Console]::WindowWidth - 1), $cursorTop)
    } catch {
        [Console]::Write("`r$title`n$status")
    }
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')

    $line = '{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}' -f (Get-Date), $Level, $Message
    Write-Output $line
    $logDirectory = [System.IO.Path]::GetDirectoryName($script:ActiveLogPath)
    if ($logDirectory -and -not (Test-Path -LiteralPath $logDirectory)) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    }
    Add-Content -LiteralPath $script:ActiveLogPath -Value $line -Encoding UTF8
}

function Resolve-UncPath {
    param([string]$Path)
    if ($Path -match '^([A-Za-z]):(.*)$') {
        $drive = Get-PSDrive -Name $Matches[1] -PSProvider FileSystem -ErrorAction SilentlyContinue
        if ($drive -and $drive.DisplayRoot) {
            return ($drive.DisplayRoot.TrimEnd('\') + $Matches[2])
        }
    }
    return $Path
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Select-IsoFileDialog {
    param([string]$InitialFile)

    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = 'Select Autodesk 3ds Max (2024-2028) ISO'
    $dialog.Filter = 'ISO images (*.iso)|*.iso|All files (*.*)|*.*'
    if ($InitialFile -and (Test-Path -LiteralPath (Split-Path -Parent $InitialFile) -PathType Container)) {
        $dialog.InitialDirectory = Split-Path -Parent $InitialFile
        if (Test-Path -LiteralPath $InitialFile -PathType Leaf) {
            $dialog.FileName = [System.IO.Path]::GetFileName($InitialFile)
        }
    } elseif (Test-Path -LiteralPath 'Z:\3dsMax\Install' -PathType Container) {
        $dialog.InitialDirectory = 'Z:\3dsMax\Install'
    } elseif (Test-Path -LiteralPath 'C:\Install' -PathType Container) {
        $dialog.InitialDirectory = 'C:\Install'
    }

    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        return $null
    }
    return $dialog.FileName
}

function Test-InstallerProcessRunning {
    $installerProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('Setup.exe', 'msiexec.exe') }
    return $null -ne $installerProcesses
}

function Select-TasksInteractively {
    param([object[]]$AvailableIsos)

    $selected = New-Object System.Collections.Generic.List[object]

    Write-Host ''
    Write-Host '=== Autodesk 3ds Max Silent Installer (2024-2028) ===' -ForegroundColor Cyan

    if (-not $AvailableIsos -or $AvailableIsos.Count -eq 0 -or $SelectIso) {
        if (-not $SelectIso) {
            Write-Host 'No 3ds Max (2024-2028) ISO files found in Z:\3dsMax\Install or C:\..F:\Install.' -ForegroundColor Yellow
            Write-Host 'Opening file browser to pick an ISO manually...'
        }
        $picked = Select-IsoFileDialog -InitialFile $IsoPath
        if (-not $picked) {
            throw 'ISO selection cancelled by user.'
        }
        $detectedVer = Get-VersionFromFileName -FilePath $picked
        if (-not $detectedVer) {
            $verInput = (Read-Host "Enter 3ds Max version for '$(Split-Path -Leaf $picked)' (2024-2028)").Trim()
            if ($script:SupportedVersions -notcontains $verInput) {
                throw "Unsupported version: '$verInput'. Expected one of: $($script:SupportedVersions -join ', ')."
            }
            $detectedVer = $verInput
        }
        $selected.Add([PSCustomObject]@{ Version = $detectedVer; IsoPath = $picked })
        return $selected.ToArray()
    }

    for ($i = 0; $i -lt $AvailableIsos.Count; $i++) {
        $item = $AvailableIsos[$i]
        $idx = $i + 1
        $statusTag = if ($item.IsInstalled) { '[INSTALLED]' } else { '[READY]    ' }
        $color = if ($item.IsInstalled) { 'DarkGray' } else { 'White' }
        Write-Host ("  [{0}] {1} {2}  ({3:N2} GB)  ->  {4}" -f $idx, $statusTag, $item.DisplayName, $item.SizeGB, $item.IsoPath) -ForegroundColor $color
    }

    Write-Host ''
    Write-Host '  Enter number(s) separated by space (e.g. "1 2"), "A" for all ready, or "B" to browse ISO:' -ForegroundColor Gray
    $rawChoice = (Read-Host 'Selection').Trim()

    if (-not $rawChoice) {
        throw 'No selection provided.'
    }

    if ($rawChoice -match '^(b|browse)$') {
        $picked = Select-IsoFileDialog -InitialFile $AvailableIsos[0].IsoPath
        if (-not $picked) {
            throw 'ISO selection cancelled by user.'
        }
        $detectedVer = Get-VersionFromFileName -FilePath $picked
        if (-not $detectedVer) {
            $verInput = (Read-Host "Enter 3ds Max version for '$(Split-Path -Leaf $picked)' (2024-2028)").Trim()
            if ($script:SupportedVersions -notcontains $verInput) {
                throw "Unsupported version: '$verInput'. Expected one of: $($script:SupportedVersions -join ', ')."
            }
            $detectedVer = $verInput
        }
        $selected.Add([PSCustomObject]@{ Version = $detectedVer; IsoPath = $picked })
        return $selected.ToArray()
    }

    if ($rawChoice -match '^(a|all)$') {
        $readyItems = @($AvailableIsos | Where-Object { -not $_.IsInstalled })
        if ($readyItems.Count -eq 0) {
            $readyItems = @($AvailableIsos)
        }
        foreach ($item in $readyItems) {
            $selected.Add([PSCustomObject]@{ Version = $item.Version; IsoPath = $item.IsoPath })
        }
        return $selected.ToArray()
    }

    $tokens = $rawChoice -split '[\s,;]+' | Where-Object { $_ -ne '' }
    $pickedIndexes = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($tok in $tokens) {
        $num = 0
        if (-not [int]::TryParse($tok, [ref]$num) -or $num -lt 1 -or $num -gt $AvailableIsos.Count) {
            throw "Invalid selection token: '$tok'. Enter numbers between 1 and $($AvailableIsos.Count), 'A', or 'B'."
        }
        if ($pickedIndexes.Add($num)) {
            $item = $AvailableIsos[$num - 1]
            $selected.Add([PSCustomObject]@{ Version = $item.Version; IsoPath = $item.IsoPath })
        }
    }

    return $selected.ToArray()
}

function Install-Single3dsMaxVersion {
    param(
        [Parameter(Mandatory = $true)][string]$TargetVersion,
        [Parameter(Mandatory = $true)][string]$TargetIsoPath
    )

    $cfg = Get-VersionConfig -TargetVersion $TargetVersion
    $script:StatusDisplayTop = $null
    $script:StatusSpinnerPhase = 0
    $script:ActiveDisplayName = $cfg.DisplayName
    $script:ActiveLogPath = $cfg.LogPath
    $script:ActiveStatusPath = $cfg.StatusPath
    $maxExecutable = $cfg.MaxExecutable
    $estimatedInstalledFiles = $cfg.EstimatedFiles

    if (Test-Path -LiteralPath $maxExecutable -PathType Leaf) {
        Write-Log "$($cfg.DisplayName) is already installed."
        Write-Status 100 "$($cfg.DisplayName) is already installed."
        Write-Host ''
        return
    }

    if (-not (Test-Path -LiteralPath $TargetIsoPath -PathType Leaf)) {
        throw "ISO file was not found: $TargetIsoPath"
    }

    $mountedByScript = $false
    $folderSnapshotJob = $null

    try {
        Write-Log "Starting silent installation of $($cfg.DisplayName) from $TargetIsoPath"
        Write-Status 0 'Mounting installation media...'

        $diskImage = Get-DiskImage -ImagePath $TargetIsoPath -ErrorAction SilentlyContinue
        if ($null -eq $diskImage -or -not $diskImage.Attached) {
            $diskImage = Mount-DiskImage -ImagePath $TargetIsoPath -PassThru
            $mountedByScript = $true
        }

        $drive = $null
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline -and -not $drive) {
            Start-Sleep -Milliseconds 250
            $drive = ($diskImage | Get-Volume | Where-Object { $_.DriveLetter } | Select-Object -First 1).DriveLetter
        }
        if (-not $drive) {
            throw 'The ISO mounted, but Windows did not expose a drive letter.'
        }

        $mediaRoot = "${drive}:"
        $setup = Join-Path $mediaRoot 'Setup.exe'
        if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) {
            $setup = Get-ChildItem -LiteralPath $mediaRoot -Filter 'Setup.exe' -File -Recurse |
                Select-Object -First 1 -ExpandProperty FullName
        }
        if (-not $setup) {
            throw "Setup.exe was not found below $mediaRoot"
        }

        Write-Log "Mounted ISO at $mediaRoot"
        Write-Log "Launching $setup -q"
        Write-Status 0 'Waiting for Autodesk Setup to start...'

        $maxInstallerLog = Join-Path ([System.IO.Path]::GetTempPath()) $cfg.TempInstallerLog
        $maxInstallerLogBaseline = if (Test-Path -LiteralPath $maxInstallerLog -PathType Leaf) {
            (Get-Item -LiteralPath $maxInstallerLog).LastWriteTimeUtc
        } else {
            $null
        }

        $setupProcess = Start-Process -FilePath $setup `
            -ArgumentList '-q' `
            -WorkingDirectory ([System.IO.Path]::GetDirectoryName($setup)) `
            -PassThru

        $deadline = (Get-Date).AddSeconds($InstallerTimeoutSeconds)
        $installRoot = Split-Path -Path $maxExecutable -Parent
        $monitorRoots = @(
            $installRoot,
            (Join-Path ([System.IO.Path]::GetTempPath()) 'odis_download_dest'),
            'C:\Program Files\Autodesk\AdODIS',
            'C:\Program Files\Common Files\Autodesk Shared',
            'C:\Program Files (x86)\Common Files\Autodesk Shared',
            'C:\ProgramData\Autodesk'
        )
        $installedFileCount = 0

        while ((Get-Date) -lt $deadline) {
            if (Test-Path -LiteralPath $maxInstallerLog -PathType Leaf) {
                $maxInstallerLastWriteTime = (Get-Item -LiteralPath $maxInstallerLog).LastWriteTimeUtc
                if ($null -eq $maxInstallerLogBaseline -or $maxInstallerLastWriteTime -gt $maxInstallerLogBaseline) {
                    $maxInstallerResult = Get-Content -LiteralPath $maxInstallerLog -Tail 500 -ErrorAction SilentlyContinue
                    if ($maxInstallerResult -match 'Installation success or error status: (1602|1603)') {
                        throw "$($cfg.DisplayName) installation failed or was cancelled (Windows Installer status $($Matches[1])). Review $maxInstallerLog."
                    }
                }
            }

            if ($setupProcess.HasExited) {
                Start-Sleep -Seconds 3
                if (-not (Test-InstallerProcessRunning)) {
                    if (Test-Path -LiteralPath $maxExecutable -PathType Leaf) {
                        Write-Status 100 'Installation completed successfully.'
                        Write-Host ''
                        Write-Log "$($cfg.DisplayName) installation completed successfully."
                        return
                    }
                    throw "Setup.exe exited with code $($setupProcess.ExitCode) before creating $maxExecutable. Review $($script:ActiveLogPath) and $maxInstallerLog."
                }
            }

            if ($null -ne $folderSnapshotJob -and $folderSnapshotJob.State -eq 'Completed') {
                $snapshot = Receive-Job -Job $folderSnapshotJob -ErrorAction SilentlyContinue
                if ($null -ne $snapshot) {
                    $installedFileCount = [int]$snapshot.FileCount
                }
                Remove-Job -Job $folderSnapshotJob -Force -ErrorAction SilentlyContinue
                $folderSnapshotJob = $null
            } elseif ($null -ne $folderSnapshotJob -and $folderSnapshotJob.State -in @('Failed', 'Stopped')) {
                Remove-Job -Job $folderSnapshotJob -Force -ErrorAction SilentlyContinue
                $folderSnapshotJob = $null
            }

            if ($null -eq $folderSnapshotJob -and -not (Test-Path -LiteralPath $maxExecutable -PathType Leaf)) {
                $folderSnapshotJob = Start-Job -ScriptBlock {
                    param([string[]]$Paths)
                    $count = 0
                    foreach ($path in $Paths) {
                        if (-not (Test-Path -LiteralPath $path -PathType Container)) { continue }
                        foreach ($file in Get-ChildItem -LiteralPath $path -File -Recurse -Force -ErrorAction SilentlyContinue) {
                            $count++
                        }
                    }
                    [PSCustomObject]@{ FileCount = $count }
                } -ArgumentList (,$monitorRoots)
            }

            if (-not (Test-Path -LiteralPath $maxExecutable -PathType Leaf)) {
                $calculatedPercent = [Math]::Min(95, [int](95 * $installedFileCount / $estimatedInstalledFiles))
                Write-Status $calculatedPercent "Installing $($cfg.DisplayName)... ($installedFileCount files)"
            }

            Start-Sleep -Milliseconds 500
        }

        if (-not (Test-Path -LiteralPath $maxExecutable -PathType Leaf)) {
            throw "Installation timed out after $InstallerTimeoutSeconds seconds. 3dsmax.exe was not found. Review $($script:ActiveLogPath)."
        }
    }
    finally {
        if ($null -ne $folderSnapshotJob) {
            Remove-Job -Job $folderSnapshotJob -Force -ErrorAction SilentlyContinue
        }
        if (-not $KeepIsoMounted -and $mountedByScript -and $TargetIsoPath) {
            try {
                Dismount-DiskImage -ImagePath $TargetIsoPath -ErrorAction SilentlyContinue | Out-Null
                Write-Log "Unmounted $TargetIsoPath"
            } catch {
                Write-Log "Could not unmount the ISO: $($_.Exception.Message)" 'WARNING'
            }
        }
    }
}

try {
    $tasks = New-Object System.Collections.Generic.List[object]

    if ($ListOnly) {
        $availableIsos = @(Find-Available3dsMaxIsos)
        if ($availableIsos.Count -eq 0) {
            Write-Host 'No 3ds Max (2024-2028) ISO files found.'
        } else {
            $availableIsos | Format-Table Version, DisplayName, SizeGB, IsInstalled, IsoPath -AutoSize | Out-String | Write-Host
        }
        exit 0
    }

    if ($InstallTasks -and $InstallTasks.Count -gt 0) {
        foreach ($rawItem in $InstallTasks) {
            foreach ($entry in ($rawItem -split ';;')) {
                if (-not $entry.Trim()) { continue }
                $parts = $entry -split '\|', 2
                if ($parts.Count -ne 2) {
                    throw "Malformed InstallTasks entry: '$entry'. Expected 'VERSION|ISOPATH'."
                }
                $tasks.Add([PSCustomObject]@{ Version = $parts[0].Trim(); IsoPath = $parts[1].Trim() })
            }
        }
    } else {
        $availableIsos = @(Find-Available3dsMaxIsos)

        if ($Interactive -and -not $Version -and -not $IsoPath) {
            foreach ($t in (Select-TasksInteractively -AvailableIsos $availableIsos)) {
                $tasks.Add($t)
            }
        } else {
            $targetVer = $Version
            $targetIso = $IsoPath

            if ($targetIso -and -not $targetVer) {
                $targetVer = Get-VersionFromFileName -FilePath $targetIso
            }

            if (-not $targetVer) {
                $newest = $availableIsos | Sort-Object -Property Version -Descending | Select-Object -First 1
                if ($newest) {
                    $targetVer = $newest.Version
                    if (-not $targetIso) { $targetIso = $newest.IsoPath }
                } else {
                    $targetVer = '2027'
                }
            }

            if (-not $targetIso) {
                $match = $availableIsos | Where-Object { $_.Version -eq $targetVer } | Select-Object -First 1
                if ($match) {
                    $targetIso = $match.IsoPath
                } elseif ($Interactive -or $SelectIso) {
                    $targetIso = Select-IsoFileDialog -InitialFile $null
                    if (-not $targetIso) {
                        throw 'ISO selection cancelled by user.'
                    }
                } else {
                    throw "No ISO found for Autodesk 3ds Max $targetVer in Z:\3dsMax\Install or C:\..F:\Install."
                }
            }

            $tasks.Add([PSCustomObject]@{ Version = $targetVer; IsoPath = $targetIso })
        }
    }

    if ($tasks.Count -eq 0) {
        throw 'No installation tasks selected.'
    }

    if (-not (Test-IsAdministrator)) {
        $uncScript = Resolve-UncPath $PSCommandPath
        $psExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $tasksPayload = @($tasks | ForEach-Object {
            $uncIso = Resolve-UncPath $_.IsoPath
            "$($_.Version)|$uncIso"
        }) -join ';;'
        $argStr = "-NoProfile -ExecutionPolicy Bypass -File `"$uncScript`" -InstallTasks `"$tasksPayload`" -InstallerTimeoutSeconds $InstallerTimeoutSeconds -StatusRefreshSeconds $StatusRefreshSeconds"
        if ($KeepIsoMounted) { $argStr += ' -KeepIsoMounted' }
        Write-Log "Requesting elevation for $($tasks.Count) installation task(s)..."
        $elevated = Start-Process -FilePath $psExe -ArgumentList $argStr -Verb RunAs -Wait -PassThru
        exit $elevated.ExitCode
    }

    $stepIdx = 0
    foreach ($task in $tasks) {
        $stepIdx++
        if ($tasks.Count -gt 1) {
            Write-Host ''
            Write-Host ("=== [{0}/{1}] Installing Autodesk 3ds Max {2} ===" -f $stepIdx, $tasks.Count, $task.Version) -ForegroundColor Cyan
        }
        Install-Single3dsMaxVersion -TargetVersion $task.Version -TargetIsoPath $task.IsoPath
    }

    exit 0
}
catch {
    Write-Status 0 $_.Exception.Message
    Write-Host ''
    Write-Log $_.Exception.Message 'ERROR'
    if ($_.InvocationInfo -and $_.InvocationInfo.Line) {
        Write-Log "Command: $($_.InvocationInfo.Line.Trim())" 'ERROR'
    }
    exit 1
}
