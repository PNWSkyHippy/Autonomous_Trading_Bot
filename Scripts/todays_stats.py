import sqlite3

import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)

from datetime import date

conn = sqlite3.connect('data/trading_bot.db')
today = date.today().isoformat()

rows = conn.execute("""
    SELECT symbol, exit_reason, pnl, strategy_name
    FROM trades 
    WHERE status='closed' 
    AND DATE(exit_time) = ?
    ORDER BY exit_time DESC
""", (today,)).fetchall()

wins   = [r for r in rows if r[2] > 0]
losses = [r for r in rows if r[2] <= 0]
total  = len(rows)
wr     = len(wins) / total * 100 if total else 0
total_pnl = sum(r[2] for r in rows)

print(f"Today: {today}")
print(f"Trades: {total} | Wins: {len(wins)} | Losses: {len(losses)}")
print(f"Win Rate: {wr:.1f}%")
print(f"Total P&L: ${total_pnl:+.2f}")
print()

# By strategy
from collections import defaultdict
by_strat = defaultdict(lambda: {'w':0,'l':0,'pnl':0})
for r in rows:
    s = r[3] or 'unknown'
    if r[2] > 0:
        by_strat[s]['w'] += 1
    else:
        by_strat[s]['l'] += 1
    by_strat[s]['pnl'] += r[2]

print("By strategy:")
for s, v in sorted(by_strat.items(), key=lambda x: -x[1]['pnl']):
    t = v['w'] + v['l']
    wr = v['w']/t*100 if t else 0
    print(f"  {s:25} {t:3} trades | {wr:5.1f}% WR | ${v['pnl']:+.2f}")

conn.close()
