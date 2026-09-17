[CmdletBinding()]
param(
    [string]$DataDate = (Get-Date -Format 'yyyy-MM-dd'),
    [string]$NotBefore,
    [string]$DatabasePath,
    [string]$PythonDirectory,
    [string]$TdxPluginPath,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot '.venv-report\Scripts\python.exe'
$PythonExe = & $PythonExe -c "from invest.core.settings import load_settings; import sys; print(load_settings().config.get('tdx', {}).get('python', sys.executable))"
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the configured TDX runtime.' }
if ($PythonDirectory) { $PythonExe = Join-Path $PythonDirectory 'python.exe' }
$env:PYTHONPATH = Join-Path $ProjectRoot 'src'
if ($TdxPluginPath) { $env:PYTHONPATH += ';' + $TdxPluginPath }
$LogDirectory = Join-Path $ProjectRoot 'logs\market'
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$Arguments = @('-X', 'utf8', '-m', 'invest', 'jobs', 'market', '--guard', '--date', $DataDate)
if ($NotBefore) { $Arguments += @('--not-before', $NotBefore) }
if ($DatabasePath) { $Arguments += @('--db', [System.IO.Path]::GetFullPath($DatabasePath)) }
if ($WhatIf) {
    & $PythonExe @Arguments '--dry-run'
    exit $LASTEXITCODE
}
# Keep the launcher short; Python records the actual business outcome.
$QuotedArguments = ($Arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join ' '
$RunTag = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
Start-Process -FilePath $PythonExe -ArgumentList $QuotedArguments -WindowStyle Hidden `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput (Join-Path $LogDirectory ("guard_{0}.log" -f $RunTag)) `
    -RedirectStandardError (Join-Path $LogDirectory ("guard_{0}.err.log" -f $RunTag))
