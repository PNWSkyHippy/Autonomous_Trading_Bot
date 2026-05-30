"""
force_close_stuck_stocks.py
============================
Emergency cleanup for stock trades that are stuck open in the DB but have
no real positions in Alpaca (paper mode). Closes them all at entry price
with pnl=0 and clears all stuck/attempt flags.

Safe to run while bot is stopped. Restart bot after running.

Usage:
    python Scripts/force_close_stuck_stocks.py           # preview only
    python Scripts/force_close_stuck_stocks.py --commit  # actually close them
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import sys
from datetime import datetime, timezone

DRY_RUN = "--commit" not in sys.argv

conn = sqlite3.connect("data/trading_bot.db")
conn.row_factory = sqlite3.Row

# ── Find all open stock trades ────────────────────────────────────────────────
open_stocks = conn.execute("""
    SELECT trade_id, symbol, direction, entry_price, quantity,
           position_value, broker, strategy_name, entry_time
    FROM trades
    WHERE status = 'open'
      AND asset_class = 'stock'
    ORDER BY symbol
""").fetchall()

if not open_stocks:
    print("No open stock trades found in DB. Nothing to do.")
    conn.close()
    sys.exit(0)

# ── Preview ───────────────────────────────────────────────────────────────────
print(f"\nFound {len(open_stocks)} open stock trade(s) in DB:\n")
print(f"  {'Symbol':<8} {'Dir':<6} {'Entry $':>10} {'Qty':>10} {'Value':>10} {'Strategy':<20} {'Broker'}")
print("  " + "-" * 80)

total_value = 0.0
for t in open_stocks:
    val = float(t["position_value"] or 0)
    total_value += val
    print(
        f"  {t['symbol']:<8} {t['direction']:<6} "
        f"${float(t['entry_price']):>9.4f} "
        f"{float(t['quantity']):>10.4f} "
        f"${val:>9.2f} "
        f"{t['strategy_name'] or 'unknown':<20} "
        f"{t['broker']}"
    )

print(f"\n  Total simulated position value: ${total_value:,.2f}")
print(f"  All will be closed at entry price with pnl=$0 (paper simulation)\n")

if DRY_RUN:
    print("=" * 60)
    print("  DRY RUN — no changes made.")
    print("  Run with --commit to actually close these trades:")
    print("  python Scripts/force_close_stuck_stocks.py --commit")
    print("=" * 60)
    conn.close()
    sys.exit(0)

# ── Commit closes ─────────────────────────────────────────────────────────────
print("Closing all stuck stock trades at entry price (pnl=$0)...")
now  = datetime.now(timezone.utc).isoformat()
done = 0

for t in open_stocks:
    tid = t["trade_id"]

    # Close the trade at entry price, pnl=0
    conn.execute("""
        UPDATE trades
        SET status      = 'closed',
            exit_price  = entry_price,
            exit_time   = ?,
            exit_reason = 'force_close_stuck_stock',
            pnl         = 0.0,
            pnl_pct     = 0.0
        WHERE trade_id = ?
    """, (now, tid))

    # Clear all stuck/attempt flags from bot_state
    for key in [
        f"close_stuck_{tid}",
        f"close_attempts_{tid}",
        f"early_loss_checked_{tid}",
        f"momentum_ext_{tid}",
    ]:
        conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))

    print(f"  Closed: {t['symbol']:8} {t['direction']} @ ${float(t['entry_price']):.4f} (pnl=$0)")
    done += 1

conn.commit()
conn.close()

print(f"\nDone — force closed {done} stuck stock trade(s).")
print("Capital impact: $0 (all closed at entry price, paper simulation)")
print("\nNext steps:")
print("  1. Close any remaining open positions manually in Alpaca paper dashboard")
print("  2. Restart the bot — position monitor cache will clear on startup")
print("  3. Run python git_push.py to sync")
