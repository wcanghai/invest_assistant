[CmdletBinding()]
param(
    [string]$TaskName = 'Invest-Daily-Market-Sync-Guard',

    [ValidateRange(1, 1440)]
    [int]$IntervalMinutes = 10,

    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$NotBefore = '15:30',

    [switch]$Unregister,

    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$HiddenLauncher = Join-Path $PSScriptRoot 'launch_daily_sync_guard_hidden.vbs'
if (-not (Test-Path -LiteralPath $HiddenLauncher)) {
    throw "Hidden daily sync launcher was not found: $HiddenLauncher"
}

if ($Unregister) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "Scheduled task does not exist: $TaskName"
        exit 0
    }
    if ($WhatIf) {
        Write-Host "WOULD_UNREGISTER $TaskName"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Scheduled task unregistered: $TaskName" -ForegroundColor Green
    exit 0
}

$arguments = @(
    '//B',
    '//NoLogo',
    ('"{0}"' -f $HiddenLauncher),
    '"-NotBefore"',
    ('"{0}"' -f $NotBefore)
) -join ' '

$action = New-ScheduledTaskAction `
    -Execute 'wscript.exe' `
    -Argument $arguments `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)

# A long-running repetition trigger is used because Windows' daily trigger cmdlet
# does not expose a repetition interval. The guard itself enforces weekdays,
# trading days and the post-close start time.
$startAt = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $startAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

Write-Host 'Windows scheduled task configuration:' -ForegroundColor Green
Write-Host "  Task name:       $TaskName"
Write-Host "  User:            $userId"
Write-Host "  First check:     $startAt"
Write-Host "  Check interval:  $IntervalMinutes minutes"
Write-Host "  Sync not before: $NotBefore on weekdays"
Write-Host "  Action:          wscript.exe $arguments"

if ($WhatIf) {
    Write-Host 'WhatIf enabled. The Windows scheduled task was not registered.'
    exit 0
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Silently check every 10 minutes and visibly launch missing A-share and ETF daily sync after market close.' `
    -Force | Out-Null

$registered = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Scheduled task registered: $TaskName" -ForegroundColor Green
Write-Host "  State:         $($registered.State)"
Write-Host "  Next run time: $($info.NextRunTime)"
