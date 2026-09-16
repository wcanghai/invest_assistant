[CmdletBinding()]
param(
    [string]$ReportDate = (Get-Date -Format 'yyyy-MM-dd'),
    [string]$DatabasePath,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot '.venv-report\Scripts\python.exe'
if ($WhatIf) {
    Write-Host "WOULD_ENSURE_DAILY_REPORT $ReportDate"
    return
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Warning 'Report environment is missing; market data sync is unaffected.'
    return
}
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $ProjectRoot 'data\databases\market.db'
}
$ReportMutex = [System.Threading.Mutex]::new($false, 'Local\InvestDailyReportEnsure')
$ReportLockHeld = $ReportMutex.WaitOne(0)
if (-not $ReportLockHeld) {
    $ReportMutex.Dispose()
    return
}
Push-Location $ProjectRoot
try {
    $env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot"
    & $PythonExe -X utf8 -m invest report run ensure --date $ReportDate --source-db $DatabasePath
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Report failed ($LASTEXITCODE); market data sync remains complete."
    }
}
finally {
    Pop-Location
    $ReportMutex.ReleaseMutex()
    $ReportMutex.Dispose()
}
