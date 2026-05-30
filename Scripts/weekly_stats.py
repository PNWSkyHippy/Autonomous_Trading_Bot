import sqlite3
import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)



from datetime import date, timedelta
from collections import defaultdict

conn = sqlite3.connect('data/trading_bot.db')

end_date   = date.today()
start_date = end_date - timedelta(days=7)

rows = conn.execute("""
    SELECT symbol, exit_reason, pnl, strategy_name, asset_class, 
           DATE(exit_time), broker
    FROM trades 
    WHERE status='closed' 
    AND DATE(exit_time) >= ?
    AND DATE(exit_time) <= ?
    ORDER BY exit_time DESC
""", (start_date.isoformat(), end_date.isoformat())).fetchall()

def print_section(title, trades, indent="  "):
    if not trades:
        print(f"{indent}{title}: no trades")
        return
    wins  = [r for r in trades if r[2] > 0]
    total = len(trades)
    wr    = len(wins) / total * 100 if total else 0
    pnl   = sum(r[2] for r in trades)
    print(f"{indent}{title}: {total} trades | {wr:.1f}% WR | ${pnl:+.2f}")
    by_strat = defaultdict(lambda: {'w':0,'l':0,'pnl':0})
    for r in trades:
        s = r[3] or 'unknown'
        if r[2] > 0: by_strat[s]['w'] += 1
        else: by_strat[s]['l'] += 1
        by_strat[s]['pnl'] += r[2]
    for s, v in sorted(by_strat.items(), key=lambda x: -x[1]['pnl']):
        t = v['w'] + v['l']
        print(f"{indent}  {s:25} {t:3} trades | {v['w']/t*100:5.1f}% WR | ${v['pnl']:+.2f}")

print(f"Period: {start_date} to {end_date}")
print(f"Total trades: {len(rows)} | Total P&L: ${sum(r[2] for r in rows):+.2f}")
print()

# By day
print("=== BY DAY ===")
by_day = defaultdict(list)
for r in rows:
    by_day[r[5]].append(r)
for d in sorted(by_day.keys(), reverse=True):
    trades = by_day[d]
    wins   = sum(1 for r in trades if r[2] > 0)
    pnl    = sum(r[2] for r in trades)
    wr     = wins/len(trades)*100 if trades else 0
    print(f"{d}: {len(trades):3} trades | {wr:5.1f}% WR | ${pnl:+.2f}")
print()

# By asset class
stocks = [r for r in rows if r[4] == 'stock']
crypto = [r for r in rows if r[4] != 'stock']
print("=== BY ASSET CLASS ===")
print_section("STOCKS", stocks)
print()
print_section("CRYPTO", crypto)
print()

# By broker
print("=== BY BROKER ===")
by_broker = defaultdict(list)
for r in rows:
    by_broker[r[6] or 'unknown'].append(r)
for broker, trades in sorted(by_broker.items(), key=lambda x: -sum(r[2] for r in x[1])):
    wins  = sum(1 for r in trades if r[2] > 0)
    total = len(trades)
    wr    = wins/total*100 if total else 0
    pnl   = sum(r[2] for r in trades)
    asset = 'crypto' if any(r[4] != 'stock' for r in trades) else 'stock'
    print(f"  {broker:12} {total:3} trades | {wr:5.1f}% WR | ${pnl:+.2f}")

conn.close()
