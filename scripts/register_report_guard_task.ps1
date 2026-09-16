$ErrorActionPreference = 'Stop'
$ReportRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'launch_report_guard_hidden.vbs'
$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument ('//B //NoLogo "{0}"' -f $launcher) -WorkingDirectory $ReportRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'Invest-Daily-Report-Guard' -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Silent 10-minute checks; 08:00/15:30 collection, retries and backfill.' `
    -Force | Out-Null
Start-ScheduledTask -TaskName 'Invest-Daily-Report-Guard'
