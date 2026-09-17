[CmdletBinding()]
param([string]$PythonExe = 'python', [switch]$WhatIf)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Version = & $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:2])))"
if ($LASTEXITCODE -ne 0 -or $Version -ne '3.12') { throw 'Python 3.12 is required.' }
$Environment = Join-Path $ProjectRoot '.venv-report'
if ($WhatIf) {
    Write-Host "WOULD_INSTALL_LOCKED_ENVIRONMENT $Environment"
    exit 0
}
if (-not (Test-Path -LiteralPath $Environment)) {
    & $PythonExe -m venv $Environment
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the project environment.' }
}
$ProjectPython = Join-Path $Environment 'Scripts\python.exe'
& $ProjectPython -m pip install -r (Join-Path $ProjectRoot 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
& $ProjectPython -m pip install --no-deps -e ($ProjectRoot + '[all,dev]')
if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
& $ProjectPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed.' }
