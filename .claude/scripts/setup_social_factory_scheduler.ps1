# Setup Windows Task Scheduler for The Homie Social Content Factory.
# Run this script (as the current user) when you're ready to ARM daily content
# generation. This is OPT-IN: the task is NOT registered automatically.
#
# The factory generates copy + media (codex-image-gen images / HyperFrames
# vertical video) and QUEUES drafts for operator approval. It NEVER auto-posts
# unless HOMIE_SOCIAL_UNATTENDED=true (enforced inside the factory, per-post
# audited). To arm autopilot, set that flag in .claude/scripts/.env FIRST and
# understand it will post to real brand accounts unattended.
#
# To stop: Disable-ScheduledTask / Unregister-ScheduledTask (below), or remove
# the channel lines from run_social_factory.bat.

$TaskName = "SecondBrain-SocialFactory"
$TaskPath = Join-Path $PSScriptRoot "run_social_factory.bat"
$Description = "The Homie - Social content factory (Archon workflow, auto-draft) daily at 6 AM"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Task '$TaskName' already exists. Removing old task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $TaskPath `
    -WorkingDirectory $PSScriptRoot

# Daily at 6 AM - an hour before the text cadence (07:00), so a morning
# batch-approve digest can carry both.
$trigger = New-ScheduledTaskTrigger `
    -Daily `
    -At "06:00"

# Generous limit - a vertical video render is minutes per clip.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45)

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
Write-Host "Task '$TaskName' created (daily 06:00, auto-draft -> queue)."
Write-Host ""
Write-Host "To verify:  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "To run now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To disable: Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove:  Unregister-ScheduledTask -TaskName '$TaskName'"
