"""
=============================================================
  BOT RESET UTILITY
  Wipes all trading history and resets capital to STARTING_CAPITAL.
  Use this to start fresh with clean, valid numbers.

  What it resets:
    - All trades (open and closed)
    - Capital history
    - Daily session state (P&L, consecutive losses, halts)
    - Daily summaries
    - Strategy results
    - Settlement queue
    - Tax ledger
    - Chat actions

  What it KEEPS:
    - Bot state / settings (strategy enabled/disabled flags)
    - Watchlists
    - Fund events (deposit/withdrawal history)
    - Withdrawal history

  Run from trading_bot_v2 root:
    python scripts/reset_bot.py

  Add --confirm to skip the confirmation prompt:
    python scripts/reset_bot.py --confirm
=============================================================
"""

import os
import sys
import sqlite3
import argparse
from datetime import date, datetime

# Add root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def reset_bot(confirm: bool = False):
    db_path = config.DB_PATH
    starting = config.STARTING_CAPITAL

    print(f"\n{'='*55}")
    print(f"  BOT RESET UTILITY")
    print(f"{'='*55}")
    print(f"  Database : {db_path}")
    print(f"  Reset to : ${starting:,.2f} starting capital")
    print(f"  Date     : {date.today().isoformat()}")
    print(f"{'='*55}")
    print()
    print("  This will PERMANENTLY DELETE:")
    print("    - All trade history (open and closed)")
    print("    - All capital snapshots")
    print("    - All daily P&L summaries")
    print("    - All session state (losses, halts, trades today)")
    print("    - All strategy performance results")
    print("    - Settlement queue")
    print("    - Tax ledger")
    print("    - Chat action log")
    print()
    print("  This will KEEP:")
    print("    - Strategy enabled/disabled settings")
    print("    - Watchlists")
    print("    - Fund deposit/withdrawal history")
    print()

    if not confirm:
        answer = input("  Type YES to confirm reset: ").strip()
        if answer != "YES":
            print("  Reset cancelled.")
            return

    print()
    print("  Resetting...")

    conn = sqlite3.connect(db_path)
    try:
        # Wipe trading history
        conn.execute("DELETE FROM trades")
        print("  [OK] trades cleared")

        conn.execute("DELETE FROM capital")
        print("  [OK] capital history cleared")

        conn.execute("DELETE FROM daily_summaries")
        print("  [OK] daily summaries cleared")

        conn.execute("DELETE FROM session_state")
        print("  [OK] session state cleared")

        conn.execute("DELETE FROM strategy_results")
        print("  [OK] strategy results cleared")

        conn.execute("DELETE FROM tax_ledger")
        print("  [OK] tax ledger cleared")

        # Clear settlement queue if it exists
        try:
            conn.execute("DELETE FROM settlement_queue")
            print("  [OK] settlement queue cleared")
        except Exception:
            pass

        # Clear chat actions if it exists
        try:
            conn.execute("DELETE FROM chat_actions")
            print("  [OK] chat actions cleared")
        except Exception:
            pass

        # Seed fresh capital snapshot at starting capital
        conn.execute("""
            INSERT INTO capital
                (timestamp, total_capital, available_cash,
                 invested_value, daily_pnl, total_pnl, note)
            VALUES (?, ?, ?, 0, 0, 0, ?)
        """, (
            datetime.now().isoformat(),
            starting, starting,
            f"Reset to starting capital ${starting:,.2f}"
        ))
        print(f"  [OK] capital seeded at ${starting:,.2f}")

        # Seed fresh session state for today
        today = date.today().isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO session_state
                (session_date, consecutive_losses, trades_today,
                 pnl_today, trading_halted, halt_reason)
            VALUES (?, 0, 0, 0.0, 0, NULL)
        """, (today,))
        print(f"  [OK] session state seeded for {today}")

        # Reset stale broker balance snapshots in bot_state
        for broker_key in ("broker_balance_kraken", "broker_balance_ibkr"):
            import json
            try:
                broker = broker_key.replace("broker_balance_", "")
                val = json.dumps({
                    "broker": broker,
                    "balance": starting if broker == "kraken" else 0.0,
                    "invested": 0.0,
                    "available": starting if broker == "kraken" else 0.0,
                    "last_updated": datetime.now().isoformat(),
                })
                conn.execute(
                    "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                    (broker_key, val)
                )
                print(f"  [OK] {broker_key} reset")
            except Exception as e:
                print(f"  [WARN] Could not reset {broker_key}: {e}")

        conn.commit()

        # Delete ML model files so scorer retrains on fresh data
        for pkl in ("data/ml_model.pkl", "data/ml_scaler.pkl"):
            full = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), pkl)
            if os.path.exists(full):
                os.remove(full)
                print(f"  [OK] deleted {pkl}")
            else:
                print(f"  [--] {pkl} not found (already clean)")

        print()
        print(f"  {'='*51}")
        print(f"  RESET COMPLETE")
        print(f"  Starting capital: ${starting:,.2f}")
        print(f"  All counters at zero")
        print(f"  Trading active: YES")
        print(f"  {'='*51}")
        print()
        print("  Restart the bot and dashboard to apply changes.")
        print()

    except Exception as e:
        conn.rollback()
        print(f"  ERROR during reset: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset bot to clean state")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Skip confirmation prompt"
    )
    args = parser.parse_args()
    reset_bot(confirm=args.confirm)
