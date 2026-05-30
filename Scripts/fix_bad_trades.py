"""Find and optionally remove open trades with zero or null stop/take profit values."""
from data.database import db
import sqlite3
import config

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row

# Find bad trades
rows = conn.execute("""
    SELECT trade_id, symbol, direction, entry_price, stop_loss, take_profit, 
           position_value, status
    FROM trades
    WHERE status = 'open'
    AND (stop_loss IS NULL OR stop_loss = 0 OR take_profit IS NULL OR take_profit = 0)
""").fetchall()

if not rows:
    print("No bad trades found!")
else:
    print(f"Found {len(rows)} trade(s) with zero/null stop or take profit:\n")
    for r in rows:
        print(f"  trade_id: {r['trade_id']}")
        print(f"  symbol:   {r['symbol']}")
        print(f"  direction:{r['direction']}")
        print(f"  entry:    ${r['entry_price']}")
        print(f"  stop:     ${r['stop_loss']}")
        print(f"  tp:       ${r['take_profit']}")
        print(f"  value:    ${r['position_value']}")
        print()

    answer = input("Delete all these trades? (yes/no): ").strip().lower()
    if answer == "yes":
        for r in rows:
            conn.execute(
                "DELETE FROM trades WHERE trade_id = ?", (r['trade_id'],)
            )
        conn.commit()
        print(f"Deleted {len(rows)} trade(s).")
    else:
        print("No changes made.")

conn.close()
