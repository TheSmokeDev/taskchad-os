# Register the weekly GitHub Signal digest (starred backlog + trending).
# ASCII-only: Windows PowerShell 5.1 reads BOM-less scripts as ANSI.

$TaskName = "SecondBrain-GitHubSignal"
$TaskPath = Join-Path $PSScriptRoot "run_github_signal.bat"
$Description = "Homie GitHub signal every Monday 9 AM - resurfaces starred-repo backlog against active work, plus trending"

if (-not (Test-Path $TaskPath)) {
    Write-Error "Missing $TaskPath"
    exit 1
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $TaskPath -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "9:00 AM"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

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

Write-Host "Task '$TaskName' registered (Monday 9:00 AM weekly)."
Write-Host "Manual run: Start-ScheduledTask -TaskName '$TaskName'"
