from data.database import db
from datetime import date
import sqlite3, config

today = date.today().isoformat()
print(f"Checking trades for: {today}")

all_today = db.get_trades_for_date(today)
print(f"Total trades today in DB: {len(all_today)}")
for t in all_today:
    print(f"  {t['symbol']:10s} {t['broker']:8s} {t['status']:6s} "
          f"entry={t['entry_time']} exit={t.get('exit_time','')} pnl={t['pnl']}")

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row
recent = conn.execute("""
    SELECT symbol, broker, status, entry_time, exit_time, pnl, asset_class
    FROM trades WHERE status='closed'
    ORDER BY exit_time DESC LIMIT 20
""").fetchall()
print(f"\nMost recent 20 closed trades:")
for r in recent:
    print(f"  {r['symbol']:10s} {r['broker']:8s} {r['asset_class']:6s} "
          f"exit={r['exit_time']} pnl={r['pnl']}")
conn.close()
