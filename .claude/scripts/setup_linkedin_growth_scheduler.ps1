# Register the draft-only LinkedIn growth packet for the current Windows user.

$TaskName = "SecondBrain-LinkedInGrowth"
$TaskPath = Join-Path $PSScriptRoot "run_linkedin_growth.bat"
$Description = "The Homie - LinkedIn authority and network-growth packet daily at 7:15 AM"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $TaskPath `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -Daily -At "07:15"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
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

Write-Host "Task '$TaskName' created (daily 07:15, draft-only)."
Write-Host "Verify: Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Run now: Start-ScheduledTask -TaskName '$TaskName'"
