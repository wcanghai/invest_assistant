[CmdletBinding()]
param(
    [string]$TaskName = 'Invest-Csi500-Daily-Refresh',

    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = '18:00',

    [ValidateSet('Csi500', 'AllA')]
    [string]$Scope = 'Csi500',

    [ValidateRange(1, 100)]
    [int]$ChunkSize = 25,

    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$DailyScript = Join-Path $PSScriptRoot 'refresh_daily_stock_data.ps1'
if (-not (Test-Path -LiteralPath $DailyScript)) {
    throw "Daily refresh script was not found: $DailyScript"
}

$arguments = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $DailyScript),
    '-Scope', $Scope,
    '-ChunkSize', $ChunkSize
) -join ' '

Write-Host 'Scheduled task configuration:' -ForegroundColor Green
Write-Host "  Task name: $TaskName"
Write-Host "  Run time:  $At, Monday to Friday"
Write-Host "  Scope:     $Scope"
Write-Host "  Command:   powershell.exe $arguments"

if ($WhatIf) {
    Write-Host 'WhatIf enabled. The scheduled task was not registered.'
    exit 0
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek @(
    'Monday',
    'Tuesday',
    'Wednesday',
    'Thursday',
    'Friday'
) -At $At
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12)
$userId = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Refresh A-share security pool and daily stock data after market close.' `
    -Force | Out-Null

Write-Host "Scheduled task registered: $TaskName" -ForegroundColor Green
