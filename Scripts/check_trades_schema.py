from data.database import Database

db = Database()
with db._conn() as conn:
    cols = conn.execute("PRAGMA table_info(trades)").fetchall()
    for col in cols:
        print(tuple(col))
