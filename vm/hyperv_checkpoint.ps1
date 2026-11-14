<#
.SYNOPSIS
    Revert a Hyper-V VM to a clean checkpoint (snapshot) and start it.
.EXAMPLE
    .\hyperv_checkpoint.ps1 -VmName "3dsMax2025-Test" -CheckpointName "Clean-Installed-2025"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$VmName,

    [Parameter(Mandatory = $true)]
    [string]$CheckpointName,

    [switch]$WaitHeartbeat
)

$ErrorActionPreference = "Stop"

Write-Host "[Hyper-V] Stopping VM '$VmName' if running..."
$vm = Get-VM -Name $VmName
if ($vm.State -ne "Off") {
    Stop-VM -Name $VmName -TurnOff -Force
}

Write-Host "[Hyper-V] Restoring checkpoint '$CheckpointName'..."
Restore-VMSnapshot -VMName $VmName -Name $CheckpointName -Confirm:$false

Write-Host "[Hyper-V] Starting VM '$VmName'..."
Start-VM -Name $VmName

if ($WaitHeartbeat) {
    Write-Host "[Hyper-V] Waiting for VM heartbeat..."
    do {
        Start-Sleep -Seconds 2
        $hb = (Get-VMIntegrationService -VMName $VmName -Name "Heartbeat").PrimaryStatusDescription
    } while ($hb -ne "OK")
    Write-Host "[Hyper-V] VM is up and responding."
}
