[CmdletBinding()]
param([int]$Port = 8766)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot '.venv-report\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw 'Create .venv-report and install requirements-report.txt first.'
}
Push-Location $ProjectRoot
try {
    $env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot"
    & $PythonExe -X utf8 -m invest report web --port $Port
}
finally {
    Pop-Location
}
