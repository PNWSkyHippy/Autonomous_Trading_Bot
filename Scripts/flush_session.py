"""
Flush a bad/stuck trade from the database.
Usage: python flush_trade.py
"""
from data.database import db
import sqlite3
import config
db.reset_session_state()
# Show all open trades first
#open_trades = db.get_all_closed_trades()
#open_trades = db.get_open_trades()
#print(f"\nOpen trades ({len(open_trades)}):")
#print(f"{'#':<4} {'Symbol':<12} {'Dir':<6} {'Entry':>10} {'P&L':>10} {'Trade ID'}")
#print("-" * 65)
#for i, t in enumerate(open_trades):
#    pnl = t.get("pnl", 0) or 0
#    print(
#        f"{i+1:<4} {t['symbol']:<12} {t['direction']:<6} "
#        f"${t['entry_price']:>9.4f} ${pnl:>9.2f}  {t['trade_id']}"
#    )
#
#print()
#choice = input("Enter # of trade to flush (or 'q' to quit): ").strip()
#if choice.lower() == "q":
print("Cancelled.")
exit()
