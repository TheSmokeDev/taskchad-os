# Setup Windows Task Scheduler for The Homie bot watchdog.
#
# Polls the bot's /health endpoint every 5 minutes and restarts the bot when a
# gateway (Telegram / Discord) is proven dead, or the process is hung or gone.
# This is the watcher that did not exist when the bot sat wedged for 6 weeks:
# service.py is crash-only, the bot's own scheduled task only restarts on
# process exit, and nothing polled /health.
#
# NOTE: this file must stay ASCII-only. Windows PowerShell 5.1 reads .ps1 as
# ANSI when there is no BOM, so a stray em-dash in a comment corrupts the parse
# and the script dies with "string is missing the terminator".
#
# Run as the normal interactive user (NOT elevated) so the task runs under the
# account that owns the bot's profile paths.

$TaskName = "SecondBrain-BotWatchdog"
$TaskPath = Join-Path $PSScriptRoot "run_watchdog.bat"
$Description = "The Homie - poll bot /health every 5 minutes, restart if wedged"

if (-not (Test-Path $TaskPath)) {
    Write-Error "Missing $TaskPath"
    exit 1
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Task '$TaskName' already exists. Removing old task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $TaskPath -WorkingDirectory $PSScriptRoot

# Every 5 minutes, forever. Short interval on purpose: the whole point is that a
# wedge is caught in minutes instead of weeks. One poll is a sub-second HTTP GET.
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 365)

# ExecutionTimeLimit is short because a watchdog that hangs is useless. The
# restart budget (5/hour, in the state file) prevents restart storms, so
# IgnoreNew is safe.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description $Description

Write-Host ""
Write-Host "Task '$TaskName' created - polling the bot every 5 minutes."
Write-Host ""
Write-Host "Verify:   Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Inspect:  uv run python bot_watchdog.py --status"
Write-Host "Disable:  Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:   Unregister-ScheduledTask -TaskName '$TaskName'"
