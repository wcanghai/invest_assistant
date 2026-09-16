[CmdletBinding()]
param(
    [string]$ReportDate = (Get-Date -Format 'yyyy-MM-dd'),
    [string]$DatabasePath,
    [switch]$LocalOnly
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot '.venv-report\Scripts\python.exe'
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $ProjectRoot 'data\databases\market.db'
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw 'Create .venv-report and install requirements-report.txt first.'
}
$ReportCommand = 'refresh'
if ($LocalOnly) { $ReportCommand = 'generate' }
Push-Location $ProjectRoot
try {
    $env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot"
    & $PythonExe -X utf8 -m invest report run $ReportCommand `
        --date $ReportDate --source-db $DatabasePath
    if ($LASTEXITCODE -ne 0) { throw "Report failed: $LASTEXITCODE" }
}
finally {
    Pop-Location
}
