[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$TaskBackupDirectory)

$ErrorActionPreference = 'Stop'
foreach ($name in 'Invest-Daily-Market-Sync-Guard', 'Invest-Daily-Report-Guard') {
    $file = Get-Item -LiteralPath (Join-Path $TaskBackupDirectory "$name.xml")
    if ($null -eq $file) {
        throw "Missing task backup for $name"
    }
    $xml = Get-Content -LiteralPath $file.FullName -Raw
    Register-ScheduledTask -TaskName $name -Xml $xml -Force | Out-Null
}
Write-Host 'RESTORED_TASKS'
