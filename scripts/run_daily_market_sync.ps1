[CmdletBinding()]
param(
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$DataDate = (Get-Date -Format 'yyyy-MM-dd'),

    [string]$DatabasePath,

    [string]$PythonDirectory = 'D:\SoftWare\conda\envs\stock-analysis-py312',

    [string]$TdxPluginPath = 'D:\software\tdx\PYPlugins\user'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $ProjectRoot 'data\databases\market.db'
}
$DatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
$PythonExe = Join-Path $PythonDirectory 'python.exe'
$Mutex = [System.Threading.Mutex]::new($false, 'Global\InvestDailyMarketSyncRunner')
$HasMutex = $false

try {
    $HasMutex = $Mutex.WaitOne(0)
    if (-not $HasMutex) {
        Write-Host 'Another daily market sync is already running.' -ForegroundColor Yellow
        exit 0
    }
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "Python was not found: $PythonExe"
    }
    if (-not (Get-Process -Name 'TdxW' -ErrorAction SilentlyContinue)) {
        throw 'TdxW.exe is not running. Start and sign in to TDX first.'
    }

    $env:Path = $PythonDirectory + ';' + $env:Path
    $env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot;$TdxPluginPath"
    $env:PYTHONIOENCODING = 'utf-8'

    Push-Location $ProjectRoot
    try {
        $calendarOutput = & $PythonExe -m stock_data.daily_helper `
            is-trading-day --date $DataDate
        if ($LASTEXITCODE -ne 0) {
            throw 'Trading calendar check failed.'
        }
        if ($calendarOutput -notcontains 'TRADE_DAY=True') {
            Write-Host "$DataDate is not an A-share trading day. Nothing to do."
            exit 0
        }

        $statusText = & $PythonExe scripts\check_daily_sync_status.py `
            --db $DatabasePath --date $DataDate
        $statusCode = $LASTEXITCODE
        if ($statusCode -notin @(0, 10)) {
            throw "Daily status check failed: $statusCode"
        }
        $status = $statusText | ConvertFrom-Json
        if ($status.complete) {
            Write-Host "A-share and ETF data are already complete for $DataDate."
            & (Join-Path $PSScriptRoot 'ensure_daily_report.ps1') `
                -ReportDate $DataDate -DatabasePath $DatabasePath
            exit 0
        }

        if (-not $status.stock.complete) {
            if ($status.stock.current_date -lt $DataDate) {
                & (Join-Path $PSScriptRoot 'refresh_daily_stock_data.ps1') `
                    -DataDate $DataDate `
                    -Scope AllA `
                    -ChunkSize 100 `
                    -Domains 'bar,capital,action,trade'
                if ($LASTEXITCODE -ne 0) {
                    throw "A-share daily sync failed: $LASTEXITCODE"
                }
            }
            else {
                $missingCodes = @($status.stock.missing_codes)
                Write-Host "Retrying $($missingCodes.Count) incomplete A-share stocks."
                for ($offset = 0; $offset -lt $missingCodes.Count; $offset += 100) {
                    $end = [math]::Min($offset + 99, $missingCodes.Count - 1)
                    $chunk = @($missingCodes[$offset..$end])
                    & $PythonExe -m invest market stock daily-update `
                        --date $DataDate `
                        --stocks ($chunk -join ',') `
                        --domains 'bar,capital,action,trade' `
                        --db $DatabasePath
                    if ($LASTEXITCODE -ne 0) {
                        throw "A-share retry batch failed: $LASTEXITCODE"
                    }
                }
            }
        }
        else {
            Write-Host "A-share data is already complete for $DataDate."
        }

        if (-not $status.etf.complete) {
            & (Join-Path $PSScriptRoot 'fetch_etf_data.ps1') `
                -Mode Daily `
                -EndDate $DataDate `
                -DatabasePath $DatabasePath
            if ($LASTEXITCODE -ne 0) {
                throw "ETF daily sync failed: $LASTEXITCODE"
            }
        }
        else {
            Write-Host "ETF data is already complete for $DataDate."
        }

        & $PythonExe scripts\check_daily_sync_status.py `
            --db $DatabasePath --date $DataDate
        if ($LASTEXITCODE -ne 0) {
            throw 'Daily sync finished, but completeness verification failed.'
        }
        Write-Host "Daily A-share and ETF sync completed for $DataDate." -ForegroundColor Green
        & (Join-Path $PSScriptRoot 'ensure_daily_report.ps1') `
            -ReportDate $DataDate -DatabasePath $DatabasePath
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Error $_
    exit 1
}
finally {
    if ($HasMutex) {
        $Mutex.ReleaseMutex()
    }
    $Mutex.Dispose()
}
