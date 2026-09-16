[CmdletBinding()]
param([switch]$WhatIf)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackupRoot = Join-Path $ProjectRoot 'backups\scheduled_tasks'
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
foreach ($name in 'Invest-Daily-Market-Sync-Guard', 'Invest-Daily-Report-Guard') {
    $xml = Join-Path $BackupRoot "$name-$stamp.xml"
    schtasks.exe /Query /TN $name /XML | Out-File -LiteralPath $xml -Encoding utf8
}
if ($WhatIf) {
    Write-Host "WOULD_DEPLOY_TASKS backups=$BackupRoot"
    exit 0
}
Disable-ScheduledTask -TaskName 'Invest-Daily-Market-Sync-Guard' | Out-Null
Disable-ScheduledTask -TaskName 'Invest-Daily-Report-Guard' | Out-Null
& (Join-Path $PSScriptRoot 'register_daily_market_sync_guard_task.ps1')
& (Join-Path $PSScriptRoot 'register_report_guard_task.ps1')
Write-Host "DEPLOYED_TASKS backups=$BackupRoot"
