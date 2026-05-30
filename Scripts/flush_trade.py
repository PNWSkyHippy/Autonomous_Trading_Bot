"""
Flush a bad/stuck trade from the database.
Usage: python flush_trade.py
"""
from data.database import db
import sqlite3
import config

# Show all open trades first
open_trades = db.get_open_trades()
#open_trades = db.get_open_trades()
print(f"\nOpen trades ({len(open_trades)}):")
print(f"{'#':<4} {'Symbol':<12} {'Dir':<6} {'Entry':>10} {'P&L':>10} {'Trade ID'}")
print("-" * 65)
for i, t in enumerate(open_trades):
    pnl = t.get("pnl", 0) or 0
    print(
        f"{i+1:<4} {t['symbol']:<12} {t['direction']:<6} "
        f"${t['entry_price']:>9.4f} ${pnl:>9.2f}  {t['trade_id']}"
    )

print()
choice = input("Enter # of trade to flush (or 'q' to quit): ").strip()
if choice.lower() == "q":
    print("Cancelled.")
    exit()

try:
    idx = int(choice) - 1
    trade = open_trades[idx]
except (ValueError, IndexError):
    print("Invalid selection.")
    exit()

print(f"\nSelected: {trade['symbol']} {trade['direction']} @ ${trade['entry_price']:.4f}")
confirm = input("Type YES to flush this trade (removes it with $0 P&L): ").strip()

if confirm != "YES":
    print("Cancelled.")
    exit()

# Force close the trade with $0 P&L
conn = sqlite3.connect(config.DB_PATH)
try:
    conn.execute("""
        UPDATE trades
        SET status='closed', exit_price=?, exit_time=datetime('now'),
            exit_reason='manual_flush', pnl=0, pnl_pct=0
        WHERE trade_id=?
    """, (trade["entry_price"], trade["trade_id"]))
    conn.commit()
    print(f"\nFlushed! {trade['symbol']} removed with $0 P&L.")
    print("Refresh the dashboard to confirm.")
finally:
    conn.close()
