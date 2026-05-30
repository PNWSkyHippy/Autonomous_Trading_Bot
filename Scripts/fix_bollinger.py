from data.database import Database

db = Database()
with db._conn() as conn:
    conn.execute("DELETE FROM strategy_results WHERE strategy_name = 'bollinger_breakout'")
    conn.commit()
    print("Stats reset")