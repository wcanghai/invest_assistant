$ErrorActionPreference = 'Stop'
$ReportRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ReportRoot
New-Item -ItemType Directory -Path (Join-Path $ReportRoot 'logs') -Force | Out-Null
$ReportLog = Join-Path $ReportRoot ('logs\report_guard_{0}.log' -f (Get-Date -Format 'yyyy-MM-dd'))
$PythonExe = Join-Path $ReportRoot '.venv-report\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw 'Project Python environment is missing.'
}
$env:PYTHONPATH = "$ReportRoot\src;$ReportRoot"
& $PythonExe -X utf8 -m invest jobs report *>> $ReportLog
exit $LASTEXITCODE
