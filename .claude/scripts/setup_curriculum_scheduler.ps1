# Install the Homie persona curriculum tick at six-hour cadence.
$TaskName = "SecondBrain-PersonaCurriculum"
$TaskPath = Join-Path $PSScriptRoot "run_curriculum.bat"
$Description = "The Homie - source-grounded persona curriculum tick every six hours"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $TaskPath `
    -WorkingDirectory $PSScriptRoot

$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "00:10"),
    (New-ScheduledTaskTrigger -Daily -At "06:10"),
    (New-ScheduledTaskTrigger -Daily -At "12:10"),
    (New-ScheduledTaskTrigger -Daily -At "18:10")
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 90)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description $Description

Write-Host "Task '$TaskName' created."
Write-Host "Verify: Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Disable: Disable-ScheduledTask -TaskName '$TaskName'"
