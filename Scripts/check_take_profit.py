import sqlite3
import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)

conn = sqlite3.connect("data/trading_bot.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT trade_id, symbol, entry_price, exit_price,
           exit_reason, pnl, pnl_pct, take_profit
    FROM trades
    WHERE symbol = 'TRUMP/USD'
    AND status = 'closed'
    AND DATE(entry_time) = '2026-04-25'
""""")

rows = cursor.fetchall()
for row in rows:
    print(row)

conn.close()
