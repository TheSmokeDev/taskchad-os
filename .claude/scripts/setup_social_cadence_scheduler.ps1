# Setup Windows Task Scheduler for The Homie Social Post Cadence (auto-draft)
# Run this script as Administrator if registration is denied for the current user.
#
# The cadence tick auto-DRAFTS content for cadence-enabled channels (LinkedIn by
# default) and dispatches ONLY operator-approved + scheduled posts through the
# default-deny gate. It never auto-approves and never auto-posts an unapproved
# draft. Requires SOCIAL_CADENCE_ENABLED=true in .claude/scripts/.env (otherwise
# the tick no-ops). To stop auto-drafting, disable/remove the task OR set
# SOCIAL_CADENCE_ENABLED=false.

$TaskName = "SecondBrain-SocialCadence"
$TaskPath = Join-Path $PSScriptRoot "run_social_cadence.bat"
$Description = "The Homie - Social post cadence (auto-draft) daily at 7 AM"

# Check if task already exists
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Task '$TaskName' already exists. Removing old task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Create the action
$action = New-ScheduledTaskAction `
    -Execute $TaskPath `
    -WorkingDirectory $PSScriptRoot

# Create trigger - daily at 7 AM
$trigger = New-ScheduledTaskTrigger `
    -Daily `
    -At "07:00"

# Create settings (15 min limit — the tick makes a background-model draft call)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

# Create principal (run as current user)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Register the task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description $Description

Write-Host ""
Write-Host "Task '$TaskName' created successfully (daily 07:00, auto-draft only)."
Write-Host ""
Write-Host "To verify: Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "To run now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To disable: Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove: Unregister-ScheduledTask -TaskName '$TaskName'"
