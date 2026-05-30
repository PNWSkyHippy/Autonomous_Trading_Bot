# Define parameters
$dbPath = "C:\users\linda\trading_bot_v2\data\trading_bot.db"
$sqlQuery = "SELECT
  r.timestamp,
  r.symbol,
  r.direction,
  r.strategy_name,
  r.decision,
  r.confidence,
  t.pnl,
  t.pnl_pct,
  t.exit_reason
FROM ai_signal_reviews r
LEFT JOIN trades t ON t.trade_id = r.trade_id
ORDER BY r.timestamp DESC;"

# Call Python and capture the output
$jsonOutput = python c:\users\linda\trading_bot_v2\Scripts\query_db.py --db $dbPath --sql $sqlQuery

# Convert the JSON output back into a PowerShell Object
$results = $jsonOutput | ConvertFrom-Json

# Display or use the results
$results | Format-Table -AutoSize

