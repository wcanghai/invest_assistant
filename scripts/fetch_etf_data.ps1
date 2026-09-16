[CmdletBinding()]
param(
    [ValidateSet('Full', 'Daily')]
    [string]$Mode = 'Daily',

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$EndDate = (Get-Date -Format 'yyyy-MM-dd'),

    [string]$StartDate,

    [string]$DatabasePath,

    [string]$TdxPluginPath = 'D:\software\tdx\PYPlugins\user',

    [ValidateRange(1, 30)]
    [int]$BatchSize = 5,

    [switch]$SkipMaster
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $ProjectRoot 'data\databases\market.db'
}
if (-not (Get-Process -Name 'TdxW' -ErrorAction SilentlyContinue)) {
    throw 'TdxW.exe is not running. Start and sign in to TDX first.'
}
$env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot;$TdxPluginPath"
$env:PYTHONIOENCODING = 'utf-8'
$command = if ($Mode -eq 'Full') { 'full-load' } else { 'daily-update' }
$effectiveStartDate = if ([string]::IsNullOrWhiteSpace($StartDate)) {
    if ($Mode -eq 'Full') { '2004-01-01' } else { $EndDate }
}
else {
    [datetime]::ParseExact(
        $StartDate,
        'yyyy-MM-dd',
        [System.Globalization.CultureInfo]::InvariantCulture
    ) | Out-Null
    $StartDate
}
$arguments = @(
    '-m', 'invest', 'market', 'etf', $command,
    '--db', ([System.IO.Path]::GetFullPath($DatabasePath)),
    '--start-date', $effectiveStartDate,
    '--end-date', $EndDate,
    '--batch-size', [string]$BatchSize
)
if ($SkipMaster) {
    $arguments += '--skip-master'
}
Push-Location $ProjectRoot
try {
    & python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "ETF data fetch failed. Python exit code: $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
