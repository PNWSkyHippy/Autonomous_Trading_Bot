import sqlite3
conn = sqlite3.connect('data/trading_bot.db')
cur = conn.cursor()
cur.execute('SELECT trade_id, symbol, entry_price, status FROM trades WHERE status=?', ('open',))
rows = cur.fetchall()
print('Open trades in database:')
for r in rows:
    print(r[0], r[1], r[2], r[3])
conn.close()
