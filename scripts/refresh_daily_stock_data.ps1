[CmdletBinding()]
param(
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$DataDate = (Get-Date -Format 'yyyy-MM-dd'),

    [ValidateSet('Csi500', 'AllA')]
    [string]$Scope = 'Csi500',

    [string]$DatabasePath,

    [string]$TdxPluginPath = 'D:\software\tdx\PYPlugins\user',

    [ValidateRange(1, 100)]
    [int]$ChunkSize = 25,

    [ValidateSet(
        'bar,capital,action,trade',
        'bar,capital,action,trade,financial'
    )]
    [string]$Domains = 'bar,capital,action,trade,financial',

    [switch]$SkipSecurityPool,

    [switch]$Force,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $ProjectRoot 'data\databases\market.db'
}
$DatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
$LogDirectory = Join-Path $ProjectRoot 'logs'
$LogPath = Join-Path $LogDirectory (
    'daily_stock_refresh_{0}_{1}.log' -f $Scope, (Get-Date -Format 'yyyyMMdd_HHmmss')
)
$TranscriptStarted = $false
$Mutex = $null
$HasMutex = $false

function Write-Step {
    param([string]$Message)

    Write-Host "`n[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message" -ForegroundColor Cyan
}

function Format-Command {
    param([string[]]$Arguments)

    $displayArguments = $Arguments | ForEach-Object {
        if ($_ -match '\s') {
            '"{0}"' -f $_
        }
        else {
            $_
        }
    }
    return 'python ' + ($displayArguments -join ' ')
}

function Invoke-PythonStep {
    param(
        [string]$Name,
        [string[]]$Arguments
    )

    Write-Step $Name
    Write-Host (Format-Command $Arguments)
    if ($DryRun) {
        return
    }

    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed. Python exit code: $LASTEXITCODE"
    }
}

function Test-TdxTradingDay {
    param([string]$DateValue)

    $calendarOutput = & python -m stock_data.daily_helper `
        is-trading-day --date $DateValue
    $calendarOutput | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        throw 'Trading calendar check failed.'
    }
    return $calendarOutput -contains 'TRADE_DAY=True'
}

function Get-ScopeCodes {
    param([string]$ScopeName)

    $codeText = & python -m stock_data.daily_helper `
        scope --db $DatabasePath --scope $ScopeName
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to read stock scope from SQLite.'
    }
    return @($codeText.Trim() -split ',' | Where-Object { $_ })
}

try {
    $parsedDate = [datetime]::ParseExact(
        $DataDate,
        'yyyy-MM-dd',
        [System.Globalization.CultureInfo]::InvariantCulture
    )
    if ($parsedDate.Date -gt (Get-Date).Date) {
        throw "DataDate cannot be later than today: $DataDate"
    }

    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw 'Python was not found. Install Python or add it to PATH.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $TdxPluginPath 'tqcenter.py'))) {
        throw "TDX plugin tqcenter.py was not found: $TdxPluginPath"
    }
    if (-not (Test-Path -LiteralPath $DatabasePath)) {
        throw "SQLite database was not found: $DatabasePath"
    }
    if (-not $DryRun -and -not (Get-Process -Name 'TdxW' -ErrorAction SilentlyContinue)) {
        throw 'TdxW.exe is not running. Start and sign in to TDX first.'
    }

    try {
        $otherTasks = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
            Where-Object {
                $_.CommandLine -like '*stock_data.main*' -or
                $_.CommandLine -like '*security_pool.main*'
            }
    }
    catch {
        Write-Warning 'Cannot inspect Python process command lines; relying on the refresh mutex.'
        $otherTasks = @()
    }
    if ($otherTasks) {
        $processIds = ($otherTasks.ProcessId -join ', ')
        throw "Another stock data task is running (PID: $processIds)."
    }

    $Mutex = [System.Threading.Mutex]::new(
        $false,
        'Global\InvestDailyStockRefresh'
    )
    $HasMutex = $Mutex.WaitOne(0)
    if (-not $HasMutex) {
        throw 'Another daily stock refresh is already running.'
    }

    $env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot;$TdxPluginPath"
    $env:PYTHONIOENCODING = 'utf-8'

    if (-not $DryRun -and -not $Force) {
        if (-not (Test-TdxTradingDay $DataDate)) {
            Write-Host "$DataDate is not an A-share trading day. Nothing to do."
            exit 0
        }
    }

    if (-not $DryRun) {
        New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
        Start-Transcript -Path $LogPath -Append | Out-Null
        $TranscriptStarted = $true
    }

    Write-Host 'Daily stock refresh parameters:' -ForegroundColor Green
    Write-Host "  Data date:         $DataDate"
    Write-Host "  Scope:             $Scope"
    Write-Host "  Database:          $DatabasePath"
    Write-Host "  Stock chunk size:  $ChunkSize"
    Write-Host "  Data domains:      $Domains"
    Write-Host "  Skip pool refresh: $SkipSecurityPool"
    Write-Host "  Dry run:           $DryRun"

    Push-Location $ProjectRoot
    try {
        if (-not $SkipSecurityPool) {
            Invoke-PythonStep '1/3 Refresh security pool and daily snapshot' @(
                '-m', 'invest', 'market', 'pool', 'daily-update',
                '--date', $DataDate,
                '--db', $DatabasePath
            )
        }
        else {
            Write-Step '1/3 Security pool refresh skipped'
        }

        $stockCodes = Get-ScopeCodes $Scope
        if ($stockCodes.Count -eq 0) {
            throw "No stocks found for scope: $Scope"
        }
        if ($Scope -eq 'Csi500' -and $stockCodes.Count -ne 500) {
            throw "Csi500 scope must contain 500 stocks, found $($stockCodes.Count)."
        }

        $chunkCount = [math]::Ceiling($stockCodes.Count / $ChunkSize)
        Write-Step "2/3 Refresh $($stockCodes.Count) stocks in $chunkCount chunks"
        for ($offset = 0; $offset -lt $stockCodes.Count; $offset += $ChunkSize) {
            $end = [math]::Min($offset + $ChunkSize - 1, $stockCodes.Count - 1)
            $chunk = @($stockCodes[$offset..$end])
            $chunkNumber = [math]::Floor($offset / $ChunkSize) + 1
            Invoke-PythonStep "Stock chunk $chunkNumber/$chunkCount" @(
                '-m', 'invest', 'market', 'stock', 'daily-update',
                '--date', $DataDate,
                '--stocks', ($chunk -join ','),
                '--domains', $Domains,
                '--db', $DatabasePath
            )
        }

        Invoke-PythonStep '3/3 Check today data and sync completeness' @(
            '-m', 'invest', 'market', 'stock', 'check-data',
            '--db', $DatabasePath,
            '--date', $DataDate,
            '--check-profile', 'daily'
        )
    }
    finally {
        Pop-Location
    }

    if ($DryRun) {
        Write-Host "`nDry run passed. No data was fetched or written." -ForegroundColor Green
    }
    else {
        Write-Host "`nDaily stock refresh completed." -ForegroundColor Green
        Write-Host "Database: $DatabasePath"
        Write-Host "Log:      $LogPath"
    }
}
catch {
    Write-Error $_
    exit 1
}
finally {
    if ($TranscriptStarted) {
        Stop-Transcript | Out-Null
    }
    if ($HasMutex -and $null -ne $Mutex) {
        $Mutex.ReleaseMutex()
    }
    if ($null -ne $Mutex) {
        $Mutex.Dispose()
    }
}
