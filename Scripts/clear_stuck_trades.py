import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import db
import sqlite3

conn = sqlite3.connect('data/trading_bot.db')
rows = conn.execute("SELECT trade_id, symbol FROM trades WHERE status='open'").fetchall()
conn.close()

count = 0
for trade_id, symbol in rows:
    db.set_state(f'close_stuck_{trade_id}', 0)
    db.set_state(f'close_attempts_{trade_id}', 0)
    print(f'Reset stuck flag: {symbol} {trade_id[:8]}')
    count += 1

print(f'\nDone — reset {count} trades.')
