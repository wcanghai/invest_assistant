[CmdletBinding()]
param(
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$EndDate = (Get-Date -Format 'yyyy-MM-dd'),

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$StartDate = '2004-01-01',

    [string]$DatabasePath,

    [string]$PythonPath,

    [string]$TdxPluginPath = 'D:\software\tdx\PYPlugins\user',

    [ValidateRange(1, 50)]
    [int]$StockChunkSize = 25,

    [ValidateRange(1, 10)]
    [int]$AttemptsPerRound = 3,

    [ValidateRange(5, 3600)]
    [int]$RetryDelaySeconds = 60,

    [switch]$RefreshSecurityPool,

    [switch]$RebuildCheckpoint,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$Utf8Encoding = New-Object System.Text.UTF8Encoding($false)
try {
    [Console]::InputEncoding = $Utf8Encoding
    [Console]::OutputEncoding = $Utf8Encoding
}
catch {
    # Some redirected hosts do not expose a mutable console encoding.
}
$OutputEncoding = $Utf8Encoding
$codePageTool = Join-Path $env:SystemRoot 'System32\chcp.com'
if (Test-Path -LiteralPath $codePageTool) {
    & $codePageTool 65001 | Out-Null
}
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    $DatabasePath = Join-Path $ProjectRoot 'data\databases\market.db'
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $ProjectRoot '.venv-report\Scripts\python.exe'
}
$DatabasePath = [System.IO.Path]::GetFullPath($DatabasePath)
$PythonPath = [System.IO.Path]::GetFullPath($PythonPath)
$CheckpointPath = Join-Path (
    Split-Path -Parent $DatabasePath
) ("full_load_financialall_checkpoint_${StartDate}_${EndDate}.txt")
$LogDirectory = Join-Path $ProjectRoot 'logs'
$LogPath = Join-Path $LogDirectory (
    'financialall_one_script_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss')
)
$Mutex = $null
$HasMutex = $false
$TranscriptStarted = $false
$RunStartedAt = Get-Date

function Write-Stage {
    param([string]$Message)

    Write-Host ''
    Write-Host (
        '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    ) -ForegroundColor Cyan
}

function Format-Duration {
    param([timespan]$Duration)

    if ($Duration.TotalDays -ge 1) {
        return '{0}d {1:00}:{2:00}:{3:00}' -f (
            [math]::Floor($Duration.TotalDays),
            $Duration.Hours,
            $Duration.Minutes,
            $Duration.Seconds
        )
    }
    return '{0:00}:{1:00}:{2:00}' -f (
        [math]::Floor($Duration.TotalHours),
        $Duration.Minutes,
        $Duration.Seconds
    )
}

function Invoke-PythonLive {
    param(
        [string[]]$Arguments,
        [switch]$Quiet
    )

    $captured = [System.Collections.Generic.List[string]]::new()
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonPath @Arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            $captured.Add($line)
            if (-not $Quiet) {
                Write-Host $line
            }
        }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = ($captured -join "`n")
    }
}

function Wait-ForTdx {
    while (-not (Get-Process -Name 'TdxW' -ErrorAction SilentlyContinue)) {
        Write-Warning (
            'TDX is not running. Start and sign in to TDX. Retrying in {0}s.' -f
            $RetryDelaySeconds
        )
        Start-Sleep -Seconds $RetryDelaySeconds
    }
}

function Get-AllAStockCodes {
    $result = Invoke-PythonLive -Quiet -Arguments @(
        '-m', 'invest', 'market', 'helper', 'scope',
        '--db', $DatabasePath,
        '--scope', 'AllA'
    )
    if ($result.ExitCode -ne 0) {
        throw 'Unable to read the AllA stock scope from SQLite.'
    }
    $lastLine = @($result.Output -split "`r?`n")[-1]
    return @($lastLine.Trim() -split ',' | Where-Object { $_ })
}

function Get-CompletedCodeMap {
    $result = @{}
    if (Test-Path -LiteralPath $CheckpointPath) {
        Get-Content -LiteralPath $CheckpointPath | ForEach-Object {
            $code = $_.Trim()
            if ($code) {
                $result[$code] = $true
            }
        }
    }
    return $result
}

function Initialize-Checkpoint {
    if ((Test-Path -LiteralPath $CheckpointPath) -and -not $RebuildCheckpoint) {
        Write-Host "Using existing checkpoint: $CheckpointPath"
        return
    }

    Write-Stage 'Building the financial checkpoint from existing SQLite data'
    $seedCode = @'
import json
import sqlite3
import sys
from pathlib import Path
from invest.providers.stock import FN_FIELDS

db_path, output_path, start_date = sys.argv[1:4]
connection = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
try:
    latest = connection.execute(
        "SELECT MAX(report_date) FROM stock_financial_report"
    ).fetchone()[0]
    completed = []
    if latest:
        candidates = set()
        rows = connection.execute(
            "SELECT stock_code, raw_values_json FROM stock_financial_report "
            "WHERE report_date=?",
            (latest,),
        )
        for stock_code, raw_values in rows:
            values = json.loads(raw_values or "{}")
            if all(field in values for field in FN_FIELDS):
                candidates.add(str(stock_code))

        incomplete = set()
        candidate_list = sorted(candidates)
        for offset in range(0, len(candidate_list), 500):
            batch = candidate_list[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            sql = (
                "SELECT stock_code, raw_values_json FROM stock_financial_report "
                f"WHERE report_date>=? AND stock_code IN ({placeholders})"
            )
            for stock_code, raw_values in connection.execute(
                sql, [start_date, *batch]
            ):
                values = json.loads(raw_values or "{}")
                if not all(field in values for field in FN_FIELDS):
                    incomplete.add(str(stock_code))
        completed = sorted(candidates - incomplete)
finally:
    connection.close()

output = Path(output_path)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("".join(f"{code}\n" for code in completed), encoding="ascii")
print(
    f"Checkpoint seeded: latest_report={latest}, fields={len(FN_FIELDS)}, "
    f"completed={len(completed)}, path={output.resolve()}"
)
'@
    $seedResult = Invoke-PythonLive @(
        '-c', $seedCode, $DatabasePath, $CheckpointPath, $StartDate
    )
    if ($seedResult.ExitCode -ne 0) {
        throw 'Failed to initialize the financial checkpoint.'
    }
}

function Invoke-FinancialChunk {
    param(
        [string[]]$Codes,
        [int]$Round,
        [int]$ChunkNumber,
        [int]$ChunkCount
    )

    $arguments = @(
        '-m', 'invest', 'market', 'stock', 'bulk-full-load',
        '--end-date', $EndDate,
        '--start-date', $StartDate,
        '--domains', 'financial',
        '--stocks', ($Codes -join ','),
        '--bar-batch-size', '3',
        '--metric-batch-size', '1',
        '--financial-profile', 'all',
        '--db', $DatabasePath
    )

    for ($attempt = 1; $attempt -le $AttemptsPerRound; $attempt++) {
        Wait-ForTdx
        Write-Stage (
            'Round {0}, chunk {1}/{2}, attempt {3}/{4}, stocks {5}' -f
            $Round,
            $ChunkNumber,
            $ChunkCount,
            $attempt,
            $AttemptsPerRound,
            $Codes.Count
        )
        Write-Host ('Stocks: {0}' -f ($Codes -join ',')) -ForegroundColor DarkGray
        $result = Invoke-PythonLive $arguments
        if ($result.ExitCode -eq 0 -and $result.Output -match "'status': 'SUCCESS'") {
            $Codes | Add-Content -LiteralPath $CheckpointPath -Encoding ascii
            return $true
        }

        Write-Warning (
            'Chunk failed or returned partial data (exit={0}).' -f $result.ExitCode
        )
        if ($attempt -lt $AttemptsPerRound) {
            Write-Host "Retrying this chunk in $RetryDelaySeconds seconds..."
            Start-Sleep -Seconds $RetryDelaySeconds
        }
    }
    return $false
}

function Write-OverallProgress {
    param(
        [int]$Completed,
        [int]$Total,
        [int]$SucceededThisRun,
        [datetime]$StartedAt
    )

    $elapsed = (Get-Date) - $StartedAt
    $percent = if ($Total -gt 0) {
        [math]::Round($Completed * 100.0 / $Total, 2)
    }
    else { 0 }
    $remaining = [math]::Max($Total - $Completed, 0)
    $etaText = 'calculating'
    if ($SucceededThisRun -gt 0) {
        $secondsPerStock = $elapsed.TotalSeconds / $SucceededThisRun
        $etaText = Format-Duration (
            [timespan]::FromSeconds($secondsPerStock * $remaining)
        )
    }
    $status = (
        'Overall {0}/{1} ({2}%), remaining {3}, elapsed {4}, ETA {5}' -f
        $Completed,
        $Total,
        $percent,
        $remaining,
        (Format-Duration $elapsed),
        $etaText
    )
    Write-Host $status -ForegroundColor Green
    Write-Progress -Activity 'Full A-share financial data' `
        -Status $status -PercentComplete ([math]::Min($percent, 100))
}

function Write-DatabaseSummary {
    $summaryCode = @'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    row = connection.execute(
        "SELECT COUNT(*), COUNT(DISTINCT stock_code), "
        "MIN(report_date), MAX(report_date) FROM stock_financial_report"
    ).fetchone()
finally:
    connection.close()
print(
    f"Financial rows={row[0]}, stocks={row[1]}, "
    f"report_range={row[2]}..{row[3]}"
)
'@
    $result = Invoke-PythonLive @('-c', $summaryCode, $DatabasePath)
    if ($result.ExitCode -ne 0) {
        Write-Warning 'Unable to print the final database summary.'
    }
}

try {
    $parsedStart = [datetime]::ParseExact(
        $StartDate,
        'yyyy-MM-dd',
        [System.Globalization.CultureInfo]::InvariantCulture
    )
    $parsedEnd = [datetime]::ParseExact(
        $EndDate,
        'yyyy-MM-dd',
        [System.Globalization.CultureInfo]::InvariantCulture
    )
    if ($parsedStart -gt $parsedEnd) {
        throw 'StartDate cannot be later than EndDate.'
    }
    if ($parsedEnd.Date -gt (Get-Date).Date) {
        throw 'EndDate cannot be later than today.'
    }
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        throw "Python was not found: $PythonPath"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $TdxPluginPath 'tqcenter.py'))) {
        throw "TDX plugin tqcenter.py was not found: $TdxPluginPath"
    }

    $Mutex = [System.Threading.Mutex]::new(
        $false,
        'Global\InvestFinancialAllUntilComplete'
    )
    $HasMutex = $Mutex.WaitOne(0)
    if (-not $HasMutex) {
        throw 'The full-financial task is already running.'
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $DatabasePath) -Force |
        Out-Null
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
    if (-not $DryRun) {
        Start-Transcript -Path $LogPath -Append | Out-Null
        $TranscriptStarted = $true
    }

    $env:PYTHONPATH = "$ProjectRoot\src;$ProjectRoot;$TdxPluginPath"
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    Write-Host 'One-script full financial collection' -ForegroundColor Green
    Write-Host "Start date:       $StartDate"
    Write-Host "End date:         $EndDate"
    Write-Host "Database:         $DatabasePath"
    Write-Host "Checkpoint:       $CheckpointPath"
    Write-Host "Stock chunk size: $StockChunkSize"
    Write-Host "Attempts/round:   $AttemptsPerRound"
    Write-Host "Retry delay:      $RetryDelaySeconds seconds"
    Write-Host "Log:              $LogPath"
    Write-Host "Dry run:          $DryRun"

    Push-Location $ProjectRoot
    try {
        Write-Stage '1/5 Initialize the SQLite schema'
        if (-not $DryRun) {
            $initResult = Invoke-PythonLive @(
                '-m', 'invest', 'market', 'pool', 'init-db', '--db', $DatabasePath
            )
            if ($initResult.ExitCode -ne 0) {
                throw 'Database initialization failed.'
            }
        }

        Write-Stage '2/5 Prepare the A-share universe'
        if ($RefreshSecurityPool -and -not $DryRun) {
            Wait-ForTdx
            $poolResult = Invoke-PythonLive @(
                '-m', 'invest', 'market', 'pool', 'full-load',
                '--date', $EndDate,
                '--db', $DatabasePath
            )
            if ($poolResult.ExitCode -ne 0) {
                throw 'Security pool refresh failed.'
            }
        }
        elseif (-not $RefreshSecurityPool) {
            Write-Host 'Using the existing AllA security pool.'
        }

        $stockCodes = Get-AllAStockCodes
        if ($stockCodes.Count -lt 5000) {
            throw "AllA scope is unexpectedly small: $($stockCodes.Count) stocks."
        }
        Write-Host "AllA securities: $($stockCodes.Count)" -ForegroundColor Green

        Write-Stage '3/5 Prepare the resumable checkpoint'
        if (-not $DryRun) {
            Initialize-Checkpoint
        }
        $completed = Get-CompletedCodeMap
        $validCompleted = @(
            $stockCodes | Where-Object { $completed.ContainsKey($_) }
        ).Count
        Write-Host (
            'Checkpointed {0}/{1}; remaining {2}.' -f
            $validCompleted,
            $stockCodes.Count,
            ($stockCodes.Count - $validCompleted)
        ) -ForegroundColor Green

        if ($DryRun) {
            Write-Stage '4/5 Dry-run acquisition preview'
            $remainingPreview = @(
                $stockCodes | Where-Object { -not $completed.ContainsKey($_) }
            )
            $previewCount = [math]::Min($StockChunkSize, $remainingPreview.Count)
            Write-Host "Would fetch $($remainingPreview.Count) stocks."
            if ($previewCount -gt 0) {
                Write-Host (
                    'First chunk: {0}' -f
                    ($remainingPreview[0..($previewCount - 1)] -join ',')
                )
            }
            Write-Stage '5/5 Dry run completed'
            return
        }

        Write-Stage '4/5 Fetch all 438-field financial data'
        $round = 0
        $succeededThisRun = 0
        while ($completed.Count -lt $stockCodes.Count) {
            $round += 1
            $remaining = @(
                $stockCodes | Where-Object { -not $completed.ContainsKey($_) }
            )
            if ($remaining.Count -eq 0) {
                break
            }
            $chunkCount = [math]::Ceiling($remaining.Count / $StockChunkSize)
            $roundSucceeded = 0
            Write-Stage (
                'Starting round {0}: {1} stocks in {2} chunks' -f
                $round, $remaining.Count, $chunkCount
            )

            for ($offset = 0; $offset -lt $remaining.Count; $offset += $StockChunkSize) {
                $end = [math]::Min(
                    $offset + $StockChunkSize - 1,
                    $remaining.Count - 1
                )
                $codes = @($remaining[$offset..$end])
                $chunkNumber = [math]::Floor($offset / $StockChunkSize) + 1
                $success = Invoke-FinancialChunk `
                    -Codes $codes `
                    -Round $round `
                    -ChunkNumber $chunkNumber `
                    -ChunkCount $chunkCount
                if ($success) {
                    foreach ($code in $codes) {
                        $completed[$code] = $true
                    }
                    $roundSucceeded += $codes.Count
                    $succeededThisRun += $codes.Count
                }
                else {
                    Write-Warning (
                        'Postponing chunk {0}/{1} to the next round.' -f
                        $chunkNumber, $chunkCount
                    )
                }
                Write-OverallProgress `
                    -Completed $completed.Count `
                    -Total $stockCodes.Count `
                    -SucceededThisRun $succeededThisRun `
                    -StartedAt $RunStartedAt
            }

            if ($roundSucceeded -eq 0) {
                Write-Warning (
                    'No chunk succeeded in round {0}. Waiting {1}s before retry.' -f
                    $round, $RetryDelaySeconds
                )
                Start-Sleep -Seconds $RetryDelaySeconds
            }
        }

        Write-Progress -Activity 'Full A-share financial data' -Completed
        Write-Stage '5/5 Verify completion and print database totals'
        $completed = Get-CompletedCodeMap
        $missing = @(
            $stockCodes | Where-Object { -not $completed.ContainsKey($_) }
        )
        if ($missing.Count -gt 0) {
            throw "Checkpoint verification failed: $($missing.Count) stocks missing."
        }
        Write-DatabaseSummary
        Write-Host ''
        Write-Host (
            'COMPLETED: {0}/{1} stocks, elapsed {2}.' -f
            $stockCodes.Count,
            $stockCodes.Count,
            (Format-Duration ((Get-Date) - $RunStartedAt))
        ) -ForegroundColor Green
        Write-Host "Database: $DatabasePath"
        Write-Host "Log:      $LogPath"
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
