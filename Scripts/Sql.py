
SELECT
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
ORDER BY r.timestamp DESC;