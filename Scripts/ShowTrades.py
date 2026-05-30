"""Show all currently open trades with full details."""
import sqlite3
import config
import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)


conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT trade_id, symbol, direction, entry_price, stop_loss, take_profit,
           position_value, quantity, status, strategy_name, entry_time
    FROM trades
    WHERE status = 'open'
    ORDER BY entry_time ASC
""").fetchall()

print(f"Found {len(rows)} open trade(s):\n")
for r in rows:
    print(f"  trade_id:  {r['trade_id']}")
    print(f"  symbol:    {r['symbol']}")
    print(f"  direction: {r['direction']}")
    print(f"  entry:     ${r['entry_price']}")
    print(f"  stop_loss: ${r['stop_loss']}")
    print(f"  take_profit: ${r['take_profit']}")
    print(f"  value:     ${r['position_value']}")
    print(f"  strategy:  {r['strategy_name']}")
    print(f"  time:      {r['entry_time']}")
    print()

conn.close()