[CmdletBinding()]
param(
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$DataDate = (Get-Date -Format 'yyyy-MM-dd'),

    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$NotBefore = '15:30',

    [string]$DatabasePath,

    [string]$PythonDirectory = 'D:\SoftWare\conda\envs\stock-analysis-py312',

    [string]$TdxPluginPath = 'D:\software\tdx\PYPlugins\user',

    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $ProjectRoot 'data\databases\market.db'
}
$DatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
$PythonExe = Join-Path $PythonDirectory 'python.exe'
$LogDirectory = Join-Path $ProjectRoot 'logs'
$LogPath = Join-Path $LogDirectory 'daily_sync_guard.log'

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
function Write-GuardLog([string]$Message) {
    Add-Content -LiteralPath $LogPath -Encoding utf8 `
        -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

$today = Get-Date
$target = [datetime]::ParseExact(
    $DataDate,
    'yyyy-MM-dd',
    [System.Globalization.CultureInfo]::InvariantCulture
)
$cutoff = [datetime]::ParseExact(
    "$DataDate $NotBefore",
    'yyyy-MM-dd HH:mm',
    [System.Globalization.CultureInfo]::InvariantCulture
)
if ($target.DayOfWeek -in @('Saturday', 'Sunday')) {
    exit 0
}
if ($today -lt $cutoff) {
    exit 0
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-GuardLog "ERROR Python was not found: $PythonExe"
    exit 1
}

$env:PYTHONPATH = $TdxPluginPath
$env:PYTHONIOENCODING = 'utf-8'
Push-Location $ProjectRoot
try {
    if (Get-Process -Name 'TdxW' -ErrorAction SilentlyContinue) {
        $calendarOutput = & $PythonExe -m stock_data.daily_helper `
            is-trading-day --date $DataDate
        if ($LASTEXITCODE -eq 0 -and $calendarOutput -notcontains 'TRADE_DAY=True') {
            exit 0
        }
    }

    $statusText = & $PythonExe scripts\check_daily_sync_status.py `
        --db $DatabasePath --date $DataDate
    $statusCode = $LASTEXITCODE
    if ($statusCode -eq 0) {
        $reportScript = Join-Path $PSScriptRoot 'ensure_daily_report.ps1'
        if ($WhatIf) {
            & $reportScript -ReportDate $DataDate -DatabasePath $DatabasePath -WhatIf
        }
        else {
            # Browser timeouts must not consume the guard task's five-minute execution budget.
            $reportArguments = @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', ('"{0}"' -f $reportScript),
                '-ReportDate', $DataDate,
                '-DatabasePath', ('"{0}"' -f $DatabasePath)
            )
            Start-Process -FilePath 'powershell.exe' -ArgumentList $reportArguments `
                -WorkingDirectory $ProjectRoot -WindowStyle Hidden
        }
        exit 0
    }
    if ($statusCode -ne 10) {
        Write-GuardLog "ERROR Status check failed with exit code $statusCode"
        exit 1
    }

    $runnerMutex = [System.Threading.Mutex]::new(
        $false,
        'Global\InvestDailyMarketSyncRunner'
    )
    $runnerAvailable = $runnerMutex.WaitOne(0)
    if ($runnerAvailable) {
        $runnerMutex.ReleaseMutex()
    }
    $runnerMutex.Dispose()
    if (-not $runnerAvailable) {
        exit 0
    }

    $status = $statusText | ConvertFrom-Json
    $message = 'INCOMPLETE stock={0}/{1}, etf={2}/{3}' -f `
        $status.stock.completed, $status.stock.expected, `
        $status.etf.completed, $status.etf.expected
    if ($WhatIf) {
        Write-Host "WOULD_LAUNCH $message"
        exit 10
    }

    $runner = Join-Path $PSScriptRoot 'run_daily_market_sync.ps1'
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $runner),
        '-DataDate', $DataDate,
        '-DatabasePath', ('"{0}"' -f $DatabasePath),
        '-PythonDirectory', ('"{0}"' -f $PythonDirectory),
        '-TdxPluginPath', ('"{0}"' -f $TdxPluginPath)
    )
    Start-Process `
        -FilePath 'powershell.exe' `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden
    Write-GuardLog "LAUNCHED $message"
}
finally {
    Pop-Location
}
