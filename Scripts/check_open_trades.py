"""
check_open_trades.py - Quick diagnostic to see whats actually open in DB
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3

conn = sqlite3.connect("data/trading_bot.db")
conn.row_factory = sqlite3.Row

open_trades = conn.execute("""
    SELECT trade_id, symbol, asset_class, direction, 
           entry_price, status, broker, strategy_name
    FROM trades 
    WHERE status = 'open'
    ORDER BY asset_class, symbol
""").fetchall()

print(f"\nTotal open trades in DB: {len(open_trades)}\n")

stocks = [t for t in open_trades if t['asset_class'] == 'stock']
crypto = [t for t in open_trades if t['asset_class'] == 'crypto']
other  = [t for t in open_trades if t['asset_class'] not in ('stock','crypto')]

print(f"Stocks: {len(stocks)}  Crypto: {len(crypto)}  Other: {len(other)}\n")

for t in open_trades:
    print(f"  {t['asset_class']:6} | {t['symbol']:10} | {t['direction']:5} | "
          f"${float(t['entry_price']):.4f} | {t['broker']:8} | {t['strategy_name']}")

conn.close()
