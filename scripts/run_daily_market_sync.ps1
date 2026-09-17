[CmdletBinding()]
param(
    [string]$DataDate = (Get-Date -Format 'yyyy-MM-dd'),
    [string]$DatabasePath,
    [string]$PythonDirectory,
    [string]$TdxPluginPath
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
$LogFile = Join-Path $LogDirectory ("sync_{0}.log" -f $DataDate)
$Arguments = @('-X', 'utf8', '-m', 'invest', 'jobs', 'market', '--date', $DataDate)
if ($DatabasePath) { $Arguments += @('--db', [System.IO.Path]::GetFullPath($DatabasePath)) }
& $PythonExe @Arguments *>> $LogFile
exit $LASTEXITCODE
