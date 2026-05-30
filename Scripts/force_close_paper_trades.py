"""
force_close_paper_trades.py
Forces database closure of crypto positions that were opened as paper/Coinbase
simulations but are now stuck because Kraken has no record of them.
Run once to clean up, then restart the bot.
"""
import sqlite3
import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)


from datetime import datetime

conn = sqlite3.connect('data/trading_bot.db')
conn.row_factory = sqlite3.Row

# Get all open crypto trades
open_trades = conn.execute("""
    SELECT trade_id, symbol, direction, entry_price, quantity, 
           position_value, broker
    FROM trades 
    WHERE status='open' AND asset_class='crypto'
""").fetchall()

if not open_trades:
    print("No open crypto trades found.")
    conn.close()
    exit()

print(f"Found {len(open_trades)} open crypto trades:\n")
for t in open_trades:
    print(f"  {t['symbol']:12} | {t['direction']:5} | entry=${t['entry_price']:.4f} | broker={t['broker']}")

print(f"\nForce closing all {len(open_trades)} as paper simulation (pnl=0)...")
print("These were paper trades with no real exchange positions.\n")

now = datetime.now().isoformat()
count = 0

for t in open_trades:
    conn.execute("""
        UPDATE trades 
        SET status='closed', 
            exit_price=entry_price,
            exit_time=?,
            exit_reason='force_close_paper',
            pnl=0.0,
            pnl_pct=0.0
        WHERE trade_id=?
    """, (now, t['trade_id']))
    
    # Clear stuck flags
    conn.execute("DELETE FROM bot_state WHERE key=?", (f"close_stuck_{t['trade_id']}",))
    conn.execute("DELETE FROM bot_state WHERE key=?", (f"close_attempts_{t['trade_id']}",))
    
    print(f"  Closed: {t['symbol']} {t['direction']} @ ${t['entry_price']:.4f} (paper, pnl=$0)")
    count += 1

conn.commit()
conn.close()

print(f"\nDone — force closed {count} paper crypto positions.")
print("Restart the bot to clear the position monitor cache.")
