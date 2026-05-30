# combine_sweep_results.ps1
# Reads all param sweep CSVs from reports\param_sweeps\
# Outputs reports\sweep_combined.xlsx with:
#   - Sheet 1 "Winners"  : best combo per strategy at a glance
#   - One sheet per strategy: all rows sorted score DESC, then profit factor DESC
#
# Usage:
#   .\Scripts\combine_sweep_results.ps1
#   .\Scripts\combine_sweep_results.ps1 -TopN 20   # keep top N rows per sheet

param(
    [string]$InputDir   = "reports\param_sweeps",
    [string]$OutputFile = "reports\sweep_combined.xlsx",
    [int]$TopN          = 0
)

Set-Location $PSScriptRoot\..
Import-Module ImportExcel -ErrorAction Stop

$csvFiles = Get-ChildItem -Path $InputDir -Filter "*.csv" | Sort-Object Name

if ($csvFiles.Count -eq 0) {
    Write-Host "No CSV files found in $InputDir" -ForegroundColor Yellow
    exit 1
}

$outputPath = Join-Path (Get-Location) $OutputFile

# Remove old file so we start fresh (ImportExcel appends by default)
if (Test-Path $outputPath) { Remove-Item $outputPath -Force }

$winnerRows = [System.Collections.Generic.List[PSCustomObject]]::new()

foreach ($file in $csvFiles) {

    # Derive strategy label (strip trailing _YYYYMMDD_HHMMSS)
    $baseName = $file.BaseName
    $label    = $baseName -replace '_\d{8}_\d{6}$', ''

    # Detect timeframe from matching pass1 log
    $tfHint = ""
    $allLogs = Get-ChildItem -Path $InputDir -Filter "${label}_*pass1.log" -ErrorAction SilentlyContinue
    if ($null -ne $allLogs -and $allLogs.Count -eq 1 -and $allLogs[0].Name -match '_(5m|1h|15m|1d)_pass') {
        $tfHint = "_$($Matches[1])"
    } elseif ($null -ne $allLogs -and $allLogs.Count -gt 1) {
        $ts      = $baseName -replace '^.*_(\d{8}_\d{6})$', '$1'
        $csvTime = [datetime]::ParseExact($ts, 'yyyyMMdd_HHmmss', $null)
        $best    = $allLogs | Sort-Object { [Math]::Abs(($csvTime - $_.LastWriteTime).TotalSeconds) } | Select-Object -First 1
        if ($best -and $best.Name -match '_(5m|1h|15m|1d)_pass') {
            $tfHint = "_$($Matches[1])"
        }
    }

    $stratLabel = "$label$tfHint"
    # Sheet names max 31 chars, no special chars
    $sheetName  = ($stratLabel -replace '[:\\/?*\[\]]', '_')[0..30] -join ''

    # Import CSV as objects
    $rows = Import-Csv $file.FullName

    if ($rows.Count -eq 0) {
        Write-Host "  SKIP $($file.Name) -- empty" -ForegroundColor DarkGray
        continue
    }

    # Sort: score DESC, avg_profit_factor DESC, avg_net_pnl DESC, consistency DESC
    $sorted = $rows | Sort-Object `
        @{ Expression = { $v = $_.score;             if ($v) { [double]$v } else { 0 } }; Descending = $true },
        @{ Expression = { $v = $_.avg_profit_factor; if ($v) { [double]$v } else { 0 } }; Descending = $true },
        @{ Expression = { $v = $_.avg_net_pnl;       if ($v) { [double]$v } else { 0 } }; Descending = $true },
        @{ Expression = { $v = $_.consistency;        if ($v) { [double]$v } else { 0 } }; Descending = $true }

    if ($TopN -gt 0 -and $sorted.Count -gt $TopN) {
        $sorted = $sorted[0..($TopN - 1)]
    }

    $bestScore = $sorted[0].score
    Write-Host "  + $stratLabel  best score=$bestScore  ($($sorted.Count) rows)  <- $($file.Name)"

    # Add strategy column and export to its own sheet
    $sorted | Select-Object @{N='strategy';E={$stratLabel}}, * |
        Export-Excel -Path $outputPath -WorksheetName $sheetName -AutoFilter -FreezeTopRow -BoldTopRow -AutoSize

    # Collect winner row for summary sheet
    # Fixed metric columns + all params collapsed into one readable "best_params" column
    $metricCols = @('score','avg_profit_factor','avg_win_rate','avg_net_pnl','consistency','symbols_tested','symbols_profitable')
    $best = $sorted[0]
    $paramCols = $best.PSObject.Properties.Name | Where-Object { $metricCols -notcontains $_ }

    $winRow = [ordered]@{ strategy = $stratLabel }
    foreach ($m in $metricCols) {
        if ($best.PSObject.Properties[$m]) { $winRow[$m] = $best.$m }
    }
    # Each param gets its own name+value column pair: param_1, val_1, param_2, val_2 ...
    $i = 1
    foreach ($p in $paramCols) {
        $winRow["param_$i"] = $p
        $winRow["val_$i"]   = $best.$p
        $i++
    }
    $winnerRows.Add([PSCustomObject]$winRow)
}

# Write Winners sheet — each strategy row uses its own param column names
# (columns that don't apply to a strategy will just be blank — clearly labeled)
if ($winnerRows.Count -gt 0) {
    $winnerRows | Export-Excel -Path $outputPath -WorksheetName "Winners" `
        -AutoFilter -FreezeTopRow -BoldTopRow -AutoSize `
        -MoveToStart
}

Write-Host ""
Write-Host "================================================================"
Write-Host "  Combined $($winnerRows.Count) sweep result files"
Write-Host "  Output : $outputPath"
Write-Host "  Sheet 1 'Winners' = best combo per strategy"
Write-Host "  Each strategy sheet = all rows, best score at top"
Write-Host "================================================================"
