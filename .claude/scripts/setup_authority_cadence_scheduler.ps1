param(
    [switch]$Enable
)

# Registers ONE coordinated GEO Authority task with two triggers:
#   06:30 daily                  source research + packet persistence
#   07:00 Mon/Tue/Wed/Fri       deterministic content slot
#
# The task is registered disabled unless -Enable is passed. Even when enabled,
# the Python entrypoint remains inert until AUTHORITY_ENGINE_ENABLED=true.
# Credential rotation and visible-session refresh must be completed before an
# operator uses -Enable or flips the environment gate.

$ErrorActionPreference = "Stop"

$TaskName = "SecondBrain-GEOAuthority"
$TaskPath = Join-Path $PSScriptRoot "run_authority_cadence.bat"
$HiddenRunner = Join-Path $PSScriptRoot "run_hidden.vbs"
$WScriptPath = Join-Path $env:SystemRoot "System32\wscript.exe"
$ScheduledLogDir = Join-Path (Split-Path -Parent $PSScriptRoot) "data\logs\scheduled"
$LogPath = Join-Path $ScheduledLogDir "run_authority_cadence.log"
$Description = "The Homie - source-backed GEO Authority research and review cadence"

# These are local-wallclock triggers; Windows applies the Pacific DST rules.
# Refuse a different host timezone rather than silently changing the cadence.
$HostTimeZone = Get-TimeZone
if ($HostTimeZone.Id -ne "Pacific Standard Time") {
    throw "Authority cadence requires host timezone 'Pacific Standard Time'; found '$($HostTimeZone.Id)'. No task was changed."
}
foreach ($RequiredPath in @($TaskPath, $HiddenRunner, $WScriptPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required scheduler executable or wrapper is missing: $RequiredPath"
    }
}

$action = New-ScheduledTaskAction `
    -Execute $WScriptPath `
    -Argument ('//B //Nologo "{0}" "{1}" "{2}"' -f $HiddenRunner, $TaskPath, $LogPath) `
    -WorkingDirectory $PSScriptRoot

$researchTrigger = New-ScheduledTaskTrigger -Daily -At "06:30"
$slotTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Monday, Tuesday, Wednesday, Friday `
    -At "07:00"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances Queue `
    -Disable:(-not $Enable) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

New-Item -ItemType Directory -Path $ScheduledLogDir -Force | Out-Null
$existingTask = Get-ScheduledTask -TaskPath "\" | Where-Object { $_.TaskName -eq $TaskName }
if ($existingTask) {
    $BackupDir = Join-Path $ScheduledLogDir "task-backups"
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    $BackupStamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffffffZ")
    $BackupPath = Join-Path $BackupDir "$TaskName-before-$BackupStamp.xml"
    Export-ScheduledTask -TaskName $TaskName -TaskPath "\" |
        Set-Content -LiteralPath $BackupPath -Encoding Unicode
    Write-Host "Previous task definition saved to '$BackupPath'."
}

# Replace atomically after the backup succeeds, with the desired enabled state
# already in the definition (no unregister gap or briefly enabled default).
Register-ScheduledTask `
    -TaskName $TaskName `
    -TaskPath "\" `
    -Action $action `
    -Trigger @($researchTrigger, $slotTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Force `
    -Description $Description | Out-Null

$status = if ($Enable) { "ENABLED" } else { "DISABLED" }
Write-Host "Task '$TaskName' registered $status."
Write-Host "Research: daily 06:30 Pacific time"
Write-Host "Content: Monday, Tuesday, Wednesday, Friday 07:00 Pacific time"
Write-Host "Hidden-run log: $LogPath"
Write-Host "Runtime gate: AUTHORITY_ENGINE_ENABLED=true is still required"
Write-Host "Verify: Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Enable later: Enable-ScheduledTask -TaskName '$TaskName'"
