[CmdletBinding()]
param([switch]$WhatIf)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TaskNames = @('Invest-Daily-Market-Sync-Guard', 'Invest-Daily-Report-Guard')
$PythonExe = Join-Path $ProjectRoot '.venv-report\Scripts\python.exe'
$ManifestPath = Join-Path $ProjectRoot 'data\databases\migration_manifest.json'
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw 'No verified migration manifest. Refusing to switch tasks.'
}
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ($Manifest.verification.status -ne 'ok') { throw 'Migration was not verified.' }
$statusText = & $PythonExe -X utf8 -m invest doctor
if ($LASTEXITCODE -ne 0) { throw 'Runtime check failed.' }
$status = $statusText | ConvertFrom-Json
foreach ($name in @('market', 'research', 'accounts', 'reports')) {
    if ($status.databases.$name.status -ne 'readable' -or
        $status.databases.$name.path -ne $Manifest.targets.$name -or
        $status.databases.$name.tables -notcontains 'schema_migration') {
        throw "Invalid migration target: $name"
    }
}
foreach ($name in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $name
    if (($task.Actions.Arguments -join ' ') -notlike "*$ProjectRoot*") {
        throw "Task belongs to another project: $name"
    }
    if ($task.State -eq 'Running') { throw "Task is still running: $name" }
}
if ($WhatIf) {
    Write-Host 'WOULD_ENABLE_VERIFIED_EXISTING_TASKS'
    exit 0
}
$BackupRoot = Join-Path $ProjectRoot ('backups\scheduled_tasks\' + (Get-Date -Format 'yyyyMMddTHHmmss'))
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
$originalState = @{}
try {
    foreach ($name in $TaskNames) {
        $originalState[$name] = (Get-ScheduledTask -TaskName $name).State
        Export-ScheduledTask -TaskName $name |
            Set-Content -LiteralPath (Join-Path $BackupRoot "$name.xml") -Encoding Unicode
    }
    foreach ($name in $TaskNames) { Enable-ScheduledTask -TaskName $name | Out-Null }
}
catch {
    foreach ($name in $originalState.Keys) {
        if ($originalState[$name] -eq 'Disabled') {
            Disable-ScheduledTask -TaskName $name | Out-Null
        }
    }
    throw
}
Write-Host "ENABLED_VERIFIED_TASKS backups=$BackupRoot"
