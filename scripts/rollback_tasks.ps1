[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$TaskBackupDirectory)

$ErrorActionPreference = 'Stop'
foreach ($name in 'Invest-Daily-Market-Sync-Guard', 'Invest-Daily-Report-Guard') {
    $file = Get-ChildItem -LiteralPath $TaskBackupDirectory -Filter "$name-*.xml" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $file) {
        throw "Missing task backup for $name"
    }
    schtasks.exe /Create /TN $name /XML $file.FullName /F | Out-Null
}
Write-Host 'RESTORED_TASKS'
