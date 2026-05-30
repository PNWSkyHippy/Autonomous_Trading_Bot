"""
=============================================================
  WEEKLY TRADE LOG EXPORTER
  Generates a CSV trade log for a given week.

  Schedule:
    Runs automatically every Sunday at midnight (00:00 PT)
    covering the previous Sunday through Saturday.

  Output:
    reports/MM-DD-YYYY_MM-DD-YYYY_tradelog.csv
    where dates are the start and end of the week covered.

  Columns:
    Symbol, Entry Date, Entry Time, Entry Price,
    Exit Date, Exit Time, Exit Price, Shares, P&L

  All times stored in DB are UTC. Output converts to
  Pacific Time to match your local machine.

  Standalone usage:
    python reporting/weekly_tradelog.py                    # last week
    python reporting/weekly_tradelog.py 2026-04-10 2026-04-11  # custom range
    python reporting/weekly_tradelog.py 2026-04-12 2026-04-18  # custom range
    python reporting/weekly_tradelog.py 2026-04-19 2026-04-25  # custom range
=============================================================
"""

import csv
import logging
import os
import sys
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Tuple

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)

logger = logging.getLogger(__name__)


def _to_pacific(utc_str: str) -> Optional[datetime]:
    """Convert UTC ISO string to Pacific Time datetime."""
    if not utc_str:
        return None
    try:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return None


def get_week_range(reference_date: date = None) -> Tuple[date, date]:
    """
    Get the Sunday-Saturday week range for the week that just ended.
    If called at midnight Sunday, covers the previous Sun-Sat.
    Returns (start_date, end_date) both inclusive.
    """
    if reference_date is None:
        reference_date = date.today()

    days_since_saturday = (reference_date.weekday() + 2) % 7
    if days_since_saturday == 0:
        end_date = reference_date - timedelta(days=1)
    else:
        end_date = reference_date - timedelta(days=days_since_saturday)

    start_date = end_date - timedelta(days=6)
    return start_date, end_date


def generate_weekly_tradelog(
    start_date: date = None,
    end_date: date   = None,
    output_dir: str  = "reports"
) -> Optional[str]:
    """
    Generate weekly trade log CSV.

    Args:
        start_date: Start of period (inclusive). Defaults to last Sunday.
        end_date:   End of period (inclusive). Defaults to last Saturday.
        output_dir: Directory to save the CSV. Created if doesn't exist.

    Returns:
        Path to the generated CSV file, or None if no trades found.
    """
    if start_date is None or end_date is None:
        start_date, end_date = get_week_range()

    logger.info(
        f"Generating weekly trade log: "
        f"{start_date.strftime('%m-%d-%Y')} to {end_date.strftime('%m-%d-%Y')}"
    )

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    import sqlite3
    import config

    with sqlite3.connect(config.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT
                symbol,
                entry_time,
                entry_price,
                exit_time,
                exit_price,
                quantity,
                pnl,
                direction,
                strategy_name,
                broker,
                exit_reason
            FROM trades
            WHERE status = 'closed'
              AND exit_time IS NOT NULL
              AND exit_price IS NOT NULL
              AND date(entry_time) >= ?
              AND date(entry_time) <= ?
            ORDER BY entry_time ASC
        """, (
            start_date.isoformat(),
            end_date.isoformat()
        )).fetchall()

    trades = [dict(r) for r in rows]

    if not trades:
        logger.info(
            f"No closed trades found for "
            f"{start_date.strftime('%m-%d-%Y')} to {end_date.strftime('%m-%d-%Y')}"
        )
        return None

    os.makedirs(output_dir, exist_ok=True)
    filename = (
        f"{start_date.strftime('%m-%d-%Y')}_"
        f"{end_date.strftime('%m-%d-%Y')}_tradelog.csv"
    )
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        writer.writerow([
            "Symbol",
            "Entry Date",
            "Entry Time",
            "Entry Price",
            "Exit Date",
            "Exit Time",
            "Exit Price",
            "Shares",
            "P&L",
            "Direction",
            "Strategy",
            "Broker",
            "Exit Reason",
            "Hold Duration",
        ])

        total_pnl   = 0.0
        trade_count = 0
        win_count   = 0

        for t in trades:
            entry_pt = _to_pacific(t["entry_time"])
            exit_pt  = _to_pacific(t["exit_time"])

            # Date and time output WITHOUT timezone suffix
            # Keeping it clean so pandas can parse without warnings
            entry_date_str = entry_pt.strftime("%m/%d/%Y") if entry_pt else ""
            entry_time_str = entry_pt.strftime("%H:%M:%S") if entry_pt else ""
            exit_date_str  = exit_pt.strftime("%m/%d/%Y")  if exit_pt  else ""
            exit_time_str  = exit_pt.strftime("%H:%M:%S")  if exit_pt  else ""

            if entry_pt and exit_pt:
                delta      = exit_pt - entry_pt
                total_secs = int(delta.total_seconds())
                hours      = total_secs // 3600
                minutes    = (total_secs % 3600) // 60
                hold_str   = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
            else:
                hold_str = ""

            pnl = float(t["pnl"] or 0)
            total_pnl   += pnl
            trade_count += 1
            if pnl > 0:
                win_count += 1

            writer.writerow([
                t["symbol"],
                entry_date_str,
                entry_time_str,
                f"{float(t['entry_price']):.6f}",
                exit_date_str,
                exit_time_str,
                f"{float(t['exit_price']):.6f}",
                f"{float(t['quantity']):.4f}",
                f"{pnl:+.4f}",
                t.get("direction", "").upper(),
                t.get("strategy_name", "original"),
                t.get("broker", ""),
                t.get("exit_reason", "").replace("_", " ").title(),
                hold_str,
            ])

        # Summary footer
        writer.writerow([])
        writer.writerow(["--- SUMMARY ---"])
        writer.writerow(["Total Trades",   trade_count])
        writer.writerow(["Winning Trades",  win_count])
        writer.writerow(["Losing Trades",   trade_count - win_count])
        win_rate = (win_count / trade_count * 100) if trade_count else 0
        writer.writerow(["Win Rate",        f"{win_rate:.1f}%"])
        writer.writerow(["Total P&L",       f"${total_pnl:+.4f}"])
        writer.writerow(["Period",
            f"{start_date.strftime('%m/%d/%Y')} - {end_date.strftime('%m/%d/%Y')}"
        ])
        # Generated line uses plain time, no timezone suffix
        writer.writerow(["Generated",
            datetime.now().strftime("%m/%d/%Y %H:%M:%S")
        ])

    logger.info(
        f"Weekly trade log saved: {filepath} "
        f"({trade_count} trades, P&L ${total_pnl:+.2f})"
    )
    return filepath


def run_weekly_export():
    """Called by scheduler every Sunday at midnight."""
    try:
        filepath = generate_weekly_tradelog()
        if filepath:
            logger.info(f"[WEEKLY LOG] Exported: {filepath}")
        else:
            logger.info("[WEEKLY LOG] No trades this week \u2014 skipped.")
    except Exception as e:
        logger.error(f"[WEEKLY LOG] Export failed: {e}", exc_info=True)


# ------------------------------------------------------------------
#  STANDALONE USAGE
# ------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s [%(levelname)s] %(message)s",
        datefmt = "%H:%M:%S"
    )

    if len(sys.argv) == 3:
        try:
            start = date.fromisoformat(sys.argv[1])
            end   = date.fromisoformat(sys.argv[2])
            print(f"Generating trade log for {start} to {end}...")
            result = generate_weekly_tradelog(start, end)
        except ValueError as e:
            print(f"Date format error: {e}")
            print("Usage: python weekly_tradelog.py YYYY-MM-DD YYYY-MM-DD")
            sys.exit(1)
    else:
        start, end = get_week_range()
        print(f"Generating trade log for last week: {start} to {end}...")
        result = generate_weekly_tradelog(start, end)

    if result:
        print(f"Saved: {result}")
    else:
        print("No trades found for the specified period.")
