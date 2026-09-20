[CmdletBinding()]
param(
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$EndDate = (Get-Date -Format 'yyyy-MM-dd'),

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$StartDate = '2004-01-01',

    [string]$DatabasePath,

    [ValidateRange(1, 20)]
    [int]$BatchSize = 10,

    [switch]$AuditOnly
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $ProjectRoot 'data\databases\market.db'
}
$DatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
$PythonPath = Join-Path $ProjectRoot '.venv-report\Scripts\python.exe'
$TdxPluginPath = 'D:\software\tdx\PYPlugins\user'
$LogDirectory = Join-Path $ProjectRoot 'logs'
$LogPath = Join-Path $LogDirectory (
    'financial_gap_repair_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss')
)
$Mutex = $null
$HasMutex = $false
$TranscriptStarted = $false

try {
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        throw "Python environment not found: $PythonPath"
    }
    if (-not (Test-Path -LiteralPath $DatabasePath)) {
        throw "Database not found: $DatabasePath"
    }
    if (-not $AuditOnly) {
        if (-not (Test-Path -LiteralPath (Join-Path $TdxPluginPath 'tqcenter.py'))) {
            throw "TDX plugin not found: $TdxPluginPath"
        }
        if (-not (Get-Process -Name 'TdxW' -ErrorAction SilentlyContinue)) {
            throw 'TDX is not running. Start and sign in to TDX first.'
        }
        $OtherTasks = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
            Where-Object {
                $_.CommandLine -like '*invest market stock*' -or
                $_.CommandLine -like '*repair_financial_gaps.py*'
        }
        if ($OtherTasks) {
            throw "Another stock data task is running. PID: $($OtherTasks.ProcessId -join ', ')"
        }
    }

    $Mutex = [System.Threading.Mutex]::new(
        $false,
        'Global\InvestFinancialGapRepair'
    )
    $HasMutex = $Mutex.WaitOne(0)
    if (-not $HasMutex) {
        throw 'The financial gap repair task is already running.'
    }

    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    Start-Transcript -Path $LogPath -Append | Out-Null
    $TranscriptStarted = $true

    $env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot;$TdxPluginPath"
    $env:PYTHONIOENCODING = 'utf-8'
    $Arguments = @(
        (Join-Path $ProjectRoot 'scripts\repair_financial_gaps.py'),
        '--db', $DatabasePath,
        '--start-date', $StartDate,
        '--end-date', $EndDate,
        '--metric-batch-size', $BatchSize.ToString()
    )
    if ($AuditOnly) {
        $Arguments += '--audit-only'
    }

    Write-Host "Financial gap repair started: $EndDate" -ForegroundColor Cyan
    Write-Host "Database: $DatabasePath"
    Write-Host "Log: $LogPath"
    if ($AuditOnly) {
        Write-Host 'Mode: audit only; no data will be fetched.'
    }
    else {
        Write-Host 'Mode: fetch 92 core fields for gap securities only.'
    }

    & $PythonPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Financial gap repair failed. Python exit code: $LASTEXITCODE"
    }
    Write-Host 'Financial gap repair completed.' -ForegroundColor Green
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
