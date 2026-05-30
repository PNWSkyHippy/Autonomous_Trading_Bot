import sqlite3
c = sqlite3.connect('data/trading_bot.db')

sql = """SELECT label,value,detail FROM
        (SELECT 'Total Capital' AS label,
                '$'||printf('%.2f',total_capital) AS value,
                'Cash: $'||printf('%.2f',available_cash)||'   Invested: $'||printf('%.2f',invested_value) AS detail
         FROM capital ORDER BY timestamp DESC LIMIT 1)
        UNION ALL SELECT label,value,detail FROM
        (SELECT 'Daily P&L (tracker)' AS label,
                (CASE WHEN daily_pnl>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',daily_pnl) AS value,
                'Cumulative: '||(CASE WHEN total_pnl>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',total_pnl) AS detail
         FROM capital ORDER BY timestamp DESC LIMIT 1)
        UNION ALL
        SELECT 'In Open Trades',
               '$'||printf('%.2f',COALESCE(SUM(position_value),0)),
               CAST(COUNT(*) AS TEXT)||' open positions'
        FROM trades WHERE status='open'
        UNION ALL
        SELECT 'In Settlement',
               '$'||printf('%.2f',COALESCE(SUM(amount),0)),
               CAST(COUNT(*) AS TEXT)||' unsettled items'
        FROM settlement_queue WHERE settled=0
        UNION ALL
        SELECT 'Today Closed P&L',
               (CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0)),
               CAST(COUNT(*) AS TEXT)||' trades  '||CAST(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS TEXT)||'W / '||CAST(SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS TEXT)||'L'
        FROM trades WHERE status='closed' AND DATE(exit_time)=DATE('now','localtime')
        UNION ALL
        SELECT 'Yesterday P&L',
               (CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0)),
               CAST(COUNT(*) AS TEXT)||' trades  '||CAST(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS TEXT)||'W / '||CAST(SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS TEXT)||'L'
        FROM trades WHERE status='closed' AND DATE(exit_time)=DATE('now','-1 day','localtime')
        UNION ALL
        SELECT 'This Week P&L',
               (CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0)),
               CAST(COUNT(*) AS TEXT)||' trades'
        FROM trades WHERE status='closed' AND DATE(exit_time)>=DATE('now','-7 days','localtime')
        UNION ALL
        SELECT 'This Month P&L',
               (CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0)),
               CAST(COUNT(*) AS TEXT)||' trades'
        FROM trades WHERE status='closed'
          AND strftime('%Y-%m',exit_time)=strftime('%Y-%m','now','localtime')"""

try:
    rows = c.execute(sql).fetchall()
    print(f"OK — {len(rows)} rows returned")
    for r in rows:
        print(f"  {r[0]:<25} {r[1]:<18} {r[2]}")
except Exception as e:
    print(f"ERROR: {e}")

# Test Strategy P&L
sql2 = """SELECT strategy_name,COUNT(*) AS trades,
               SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(pnl),2) AS total_pnl,
               ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END)
                     /NULLIF(ABS(SUM(CASE WHEN pnl<=0 THEN pnl ELSE 0 END)),0),3) AS profit_factor
        FROM trades WHERE status='closed'
        GROUP BY strategy_name ORDER BY total_pnl DESC"""
try:
    rows2 = c.execute(sql2).fetchall()
    print(f"\nStrategy P&L — {len(rows2)} strategies")
    for r in rows2:
        print(f"  {r}")
except Exception as e:
    print(f"Strategy P&L ERROR: {e}")
