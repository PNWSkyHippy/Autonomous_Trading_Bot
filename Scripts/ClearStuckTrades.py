"""
ClearStuckTrades.py
====================
Resets STUCK flags and close attempt counters for all open trades.
This allows the position monitor to retry closing them on the next cycle.

Run this FIRST before force-close scripts when trades get stuck.

Usage:
    python Scripts\ClearStuckTrades.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3

conn = sqlite3.connect('data/trading_bot.db')
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT trade_id, symbol FROM trades WHERE status='open'"
).fetchall()

if not rows:
    print("No open trades found — nothing to reset.")
    conn.close()
    exit(0)

cleared = 0
for row in rows:
    trade_id = row['trade_id']
    symbol   = row['symbol']

    # Clear stuck flag and attempt counter
    conn.execute(
        "DELETE FROM bot_state WHERE key IN (?, ?, ?)",
        (
            f"close_stuck_{trade_id}",
            f"close_attempts_{trade_id}",
            f"close_last_attempt_{trade_id}",
        )
    )
    print(f"Reset stuck flag: {symbol} {trade_id[:8]}")
    cleared += 1

conn.commit()
conn.close()
print(f"\nDone — reset {cleared} trade(s).")
print("Restart the bot — position monitor will retry closes within 30s.")
