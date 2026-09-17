[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$EndDate,

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$StartDate = '2004-01-01',

    [ValidateSet('Stage1', 'Financial', 'FinancialAll', 'All')]
    [string]$Phase = 'Stage1',

    [string]$DatabasePath,

    [string]$TdxPluginPath = 'D:\software\tdx\PYPlugins\user',

    [ValidateRange(1, 20)]
    [int]$BarBatchSize = 3,

    [ValidateRange(1, 20)]
    [int]$MetricBatchSize = 1,

    [ValidateRange(1, 50)]
    [int]$StockChunkSize = 25,

    [ValidateRange(1, 5)]
    [int]$MaxChunkRetries = 3,

    [switch]$SkipSecurityPool,

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
    'all_a_stock_full_load_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss')
)
$PhaseToken = $Phase.ToLowerInvariant()
$CheckpointPath = Join-Path (
    Split-Path -Parent $DatabasePath
) ("full_load_${PhaseToken}_checkpoint_${StartDate}_${EndDate}.txt")
$LegacyCheckpointPath = Join-Path (
    Split-Path -Parent $DatabasePath
) ("full_load_checkpoint_$EndDate.txt")
$ReportPath = Join-Path $ProjectRoot (
    "doc\all_a_stock_${PhaseToken}_database_status_$EndDate.md"
)
$Domains = switch ($Phase) {
    'Stage1' { 'bar,capital,action,trade' }
    'Financial' { 'financial' }
    'FinancialAll' { 'financial' }
    default { 'bar,capital,action,trade,financial' }
}
$FinancialProfile = if ($Phase -eq 'Financial') { 'core' } else { 'all' }
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

function Get-AllAStockCodes {
    $codeText = & python -m invest market helper `
        scope --db $DatabasePath --scope AllA
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to read all A-share stock codes from SQLite.'
    }
    return @($codeText.Trim() -split ',' | Where-Object { $_ })
}

function Get-CompletedCodes {
    $completed = @{}
    if (Test-Path -LiteralPath $CheckpointPath) {
        Get-Content -LiteralPath $CheckpointPath | ForEach-Object {
            $code = $_.Trim()
            if ($code) {
                $completed[$code] = $true
            }
        }
    }
    return $completed
}

function Invoke-FullStockChunk {
    param(
        [int]$ChunkNumber,
        [int]$ChunkCount,
        [string[]]$StockCodes
    )

    $arguments = @(
        '-m', 'invest', 'market', 'stock', 'bulk-full-load',
        '--end-date', $EndDate,
        '--start-date', $StartDate,
        '--domains', $Domains,
        '--stocks', ($StockCodes -join ','),
        '--bar-batch-size', $BarBatchSize.ToString(),
        '--metric-batch-size', $MetricBatchSize.ToString(),
        '--financial-profile', $FinancialProfile,
        '--db', $DatabasePath
    )
    if ($DryRun) {
        Write-Host (Format-Command $arguments)
        return
    }

    for ($attempt = 1; $attempt -le $MaxChunkRetries; $attempt++) {
        Write-Step "Full stock chunk $ChunkNumber/$ChunkCount, attempt $attempt"
        Write-Host (Format-Command $arguments)
        $previousErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $output = & python @arguments 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorPreference
        }
        $output | ForEach-Object { Write-Host $_ }
        $outputText = $output -join "`n"
        if ($exitCode -eq 0 -and $outputText -match "'status': 'SUCCESS'") {
            $StockCodes | Add-Content -LiteralPath $CheckpointPath -Encoding ascii
            return
        }
        Write-Warning (
            "Chunk $ChunkNumber failed or was partial. Exit code: $exitCode."
        )
        if ($attempt -lt $MaxChunkRetries) {
            Start-Sleep -Seconds 10
        }
    }
    throw "Full stock chunk $ChunkNumber failed after $MaxChunkRetries attempts."
}

try {
    $parsedDate = [datetime]::ParseExact(
        $EndDate,
        'yyyy-MM-dd',
        [System.Globalization.CultureInfo]::InvariantCulture
    )
    if ($parsedDate.Date -gt (Get-Date).Date) {
        throw "EndDate cannot be later than today: $EndDate"
    }
    $parsedStartDate = [datetime]::ParseExact(
        $StartDate,
        'yyyy-MM-dd',
        [System.Globalization.CultureInfo]::InvariantCulture
    )
    if ($parsedStartDate.Date -gt $parsedDate.Date) {
        throw "StartDate cannot be later than EndDate: $StartDate"
    }

    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw 'Python was not found. Install Python or add it to PATH.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $TdxPluginPath 'tqcenter.py'))) {
        throw "TDX plugin tqcenter.py was not found: $TdxPluginPath"
    }
    if (-not (Get-Process -Name 'TdxW' -ErrorAction SilentlyContinue)) {
        throw 'TdxW.exe is not running. Start and sign in to TDX first.'
    }

    $otherTasks = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object {
            $_.CommandLine -like '*invest market stock*' -or
            $_.CommandLine -like '*invest market pool*'
        }
    if ($otherTasks) {
        $processIds = ($otherTasks.ProcessId -join ', ')
        throw "Another stock data task is running (PID: $processIds)."
    }

    $Mutex = [System.Threading.Mutex]::new(
        $false,
        'Global\InvestAllAStockFullLoad'
    )
    $HasMutex = $Mutex.WaitOne(0)
    if (-not $HasMutex) {
        throw 'Another full A-share loading script is already running.'
    }

    $env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot;$TdxPluginPath"
    $env:PYTHONIOENCODING = 'utf-8'

    if (-not $DryRun) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $DatabasePath) -Force |
            Out-Null
        New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
        Start-Transcript -Path $LogPath -Append | Out-Null
        $TranscriptStarted = $true
    }

    Write-Host 'Full A-share data load parameters:' -ForegroundColor Green
    Write-Host "  Phase:             $Phase"
    Write-Host "  Domains:           $Domains"
    Write-Host "  Financial profile: $FinancialProfile"
    Write-Host "  Start date:        $StartDate"
    Write-Host "  End date:          $EndDate"
    Write-Host "  Database:          $DatabasePath"
    Write-Host "  Bar batch size:    $BarBatchSize"
    Write-Host "  Metric batch size: $MetricBatchSize"
    Write-Host "  Stock chunk size:  $StockChunkSize"
    Write-Host "  Chunk retries:     $MaxChunkRetries"
    Write-Host "  Skip pool refresh: $SkipSecurityPool"
    Write-Host "  Checkpoint:        $CheckpointPath"
    Write-Host "  Final report:      $ReportPath"
    Write-Host "  Dry run:           $DryRun"

    Push-Location $ProjectRoot
    try {
        Invoke-PythonStep '1/5 Initialize security pool database' @(
            '-m', 'invest', 'market', 'pool', 'init-db',
            '--db', $DatabasePath
        )
        if (-not $SkipSecurityPool) {
            Invoke-PythonStep '2/5 Refresh full A-share security pool' @(
                '-m', 'invest', 'market', 'pool', 'full-load',
                '--date', $EndDate,
                '--db', $DatabasePath
            )
        }
        else {
            Write-Step '2/5 Security pool refresh skipped'
        }
        $stockCodes = Get-AllAStockCodes
        if ($stockCodes.Count -lt 5000) {
            throw "AllA scope is unexpectedly small: $($stockCodes.Count) stocks."
        }
        if (
            $Phase -eq 'Stage1' -and
            -not $DryRun -and
            -not (Test-Path -LiteralPath $CheckpointPath) -and
            (Test-Path -LiteralPath $LegacyCheckpointPath)
        ) {
            Copy-Item -LiteralPath $LegacyCheckpointPath -Destination $CheckpointPath
            Write-Host "Seeded Stage1 checkpoint from: $LegacyCheckpointPath"
        }
        $completedCodes = Get-CompletedCodes
        $remainingCodes = @(
            $stockCodes | Where-Object { -not $completedCodes.ContainsKey($_) }
        )
        $chunkCount = [math]::Ceiling($remainingCodes.Count / $StockChunkSize)
        Write-Step (
            "3/5 Load full data for $($stockCodes.Count) stocks; " +
            "$($completedCodes.Count) checkpointed, $($remainingCodes.Count) remaining"
        )
        if ($DryRun) {
            Write-Host "Would run $chunkCount stock chunks."
            if ($remainingCodes.Count -gt 0) {
                $previewEnd = [math]::Min(
                    $StockChunkSize - 1,
                    $remainingCodes.Count - 1
                )
                Invoke-FullStockChunk 1 $chunkCount @(
                    $remainingCodes[0..$previewEnd]
                )
            }
        }
        else {
            for (
                $offset = 0;
                $offset -lt $remainingCodes.Count;
                $offset += $StockChunkSize
            ) {
                $end = [math]::Min(
                    $offset + $StockChunkSize - 1,
                    $remainingCodes.Count - 1
                )
                $chunk = @($remainingCodes[$offset..$end])
                $chunkNumber = [math]::Floor($offset / $StockChunkSize) + 1
                Invoke-FullStockChunk $chunkNumber $chunkCount $chunk
            }
        }
        Invoke-PythonStep '4/5 Check database integrity and row counts' @(
            '-m', 'invest', 'market', 'stock', 'check-data',
            '--db', $DatabasePath,
            '--check-profile', 'full'
        )
        Invoke-PythonStep '5/5 Generate per-stock database report' @(
            '.\scripts\generate_database_status_report.py',
            '--db', $DatabasePath,
            '--output', $ReportPath
        )
    }
    finally {
        Pop-Location
    }

    if ($DryRun) {
        Write-Host "`nDry run passed. No data was fetched or written." -ForegroundColor Green
    }
    else {
        Write-Host "`nFull A-share data load completed." -ForegroundColor Green
        Write-Host "Database: $DatabasePath"
        Write-Host "Log:      $LogPath"
        Write-Host "Report:   $ReportPath"
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
