import sqlite3
c = sqlite3.connect('data/trading_bot.db')
tables = c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print("Tables:", [r[0] for r in tables])
for t in ['settlement_queue', 'capital', 'daily_summaries']:
    try:
        cols = c.execute(f"PRAGMA table_info({t})").fetchall()
        print(f"\n{t} columns:", [r[1] for r in cols])
    except Exception as e:
        print(f"{t}: {e}")
