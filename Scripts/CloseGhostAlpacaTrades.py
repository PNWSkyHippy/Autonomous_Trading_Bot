import sqlite3
from datetime import datetime
conn = sqlite3.connect('data/trading_bot.db')
cur = conn.cursor()
ghosts = ['798cadff-7b8', 'a1b17e85-4cd', '4ab17b3a-0d1', 'ac67f7fe-356']
for tid in ghosts:
    cur.execute('''UPDATE trades SET status=?, exit_time=?, exit_price=entry_price, pnl=0, pnl_pct=0, exit_reason=? WHERE trade_id LIKE ?''', ('closed', datetime.now().isoformat(), 'ghost_cleanup', tid + '%'))
    print('Closed', tid)
conn.commit()
conn.close()
print('Done')
