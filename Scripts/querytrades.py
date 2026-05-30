import sqlite3
import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)


conn = sqlite3.connect('data/trading_bot.db')
rows = conn.execute("""
    SELECT symbol, entry_time, exit_time, exit_reason, pnl 
    FROM trades 
    WHERE status='closed' 
    ORDER BY exit_time DESC 
    LIMIT 20
""").fetchall()
for r in rows:
    print(f"{r[0]:12} | in: {str(r[1])[:16]} | out: {str(r[2])[:16]} | {str(r[3]):25} | ${r[4]:+.2f}")
conn.close()
 