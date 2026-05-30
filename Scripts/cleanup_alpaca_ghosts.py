"""
cleanup_alpaca_ghosts.py
────────────────────────
One-time script to close the 6 orphaned Alpaca positions left over from Apr 23.
Symbols: PSQ, MSFT, GEM, NIO, NFLX, SOFI

Run ONCE at or after 9:30 AM ET Monday April 27:
    python cleanup_alpaca_ghosts.py

The script will:
  1. Connect to your Alpaca paper account
  2. Cancel any open orders on each symbol first (bracket order ghost fix)
  3. Submit a market close order for any position that still exists
  4. Print a summary of what was closed / skipped

Safe to re-run — it checks actual positions before attempting close.
"""

import os
import time
import sys
from dotenv import load_dotenv

load_dotenv()

try:
    import alpaca_trade_api as tradeapi
except ImportError:
    print("ERROR: alpaca_trade_api not installed.")
    print("Run: pip install alpaca-trade-api")
    sys.exit(1)


# ── Config ───────────────────────────────────────────────────────────────────
GHOST_SYMBOLS = ["PSQ", "MSFT", "GEM", "NIO", "NFLX", "SOFI"]

API_KEY    = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

if not API_KEY or not API_SECRET:
    print("ERROR: ALPACA_API_KEY or ALPACA_SECRET_KEY not found in .env")
    sys.exit(1)


# ── Connect ──────────────────────────────────────────────────────────────────
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")

try:
    account = api.get_account()
    print(f"Connected to Alpaca | Account: {account.id} | Status: {account.status}")
    print(f"Equity: ${float(account.equity):,.2f}\n")
except Exception as e:
    print(f"ERROR connecting to Alpaca: {e}")
    sys.exit(1)


# ── Get current open positions ────────────────────────────────────────────────
try:
    open_positions = {p.symbol: p for p in api.list_positions()}
    print(f"Open positions on account: {list(open_positions.keys()) or 'none'}\n")
except Exception as e:
    print(f"ERROR fetching positions: {e}")
    sys.exit(1)


# ── Process each ghost symbol ─────────────────────────────────────────────────
results = []

for symbol in GHOST_SYMBOLS:
    print(f"── {symbol} ──────────────────────────────")

    # Step 1: Cancel open orders on this symbol
    try:
        orders = api.list_orders(status="open", symbols=[symbol])
        if orders:
            print(f"  Cancelling {len(orders)} open order(s)...")
            for order in orders:
                api.cancel_order(order.id)
                print(f"  Cancelled order {order.id} ({order.order_type} {order.side})")
            time.sleep(0.5)             # brief pause after cancel
        else:
            print(f"  No open orders found.")
    except Exception as e:
        print(f"  WARNING: Error cancelling orders for {symbol}: {e}")

    # Step 2: Check if position exists
    if symbol not in open_positions:
        print(f"  No open position — skipping.")
        results.append((symbol, "SKIPPED", "no position"))
        continue

    pos = open_positions[symbol]
    qty  = abs(int(float(pos.qty)))
    side = pos.side                     # "long" or "short"
    unrealized_pl = float(pos.unrealized_pl)

    print(f"  Position: {side.upper()} {qty} shares | Unrealized P&L: ${unrealized_pl:+.2f}")

    # Step 3: Submit market close
    try:
        close_side = "sell" if side == "long" else "buy"
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side=close_side,
            type="market",
            time_in_force="day"
        )
        print(f"  ✓ Close order submitted | Order ID: {order.id}")
        results.append((symbol, "CLOSED", f"{close_side} {qty} @ market | P&L {unrealized_pl:+.2f}"))
    except Exception as e:
        print(f"  ✗ ERROR submitting close order: {e}")
        results.append((symbol, "ERROR", str(e)))

    time.sleep(0.3)                     # rate limit courtesy pause


# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*50)
print("CLEANUP SUMMARY")
print("="*50)
for symbol, status, detail in results:
    print(f"  {symbol:6s}  {status:8s}  {detail}")

closed_count  = sum(1 for _, s, _ in results if s == "CLOSED")
skipped_count = sum(1 for _, s, _ in results if s == "SKIPPED")
error_count   = sum(1 for _, s, _ in results if s == "ERROR")

print(f"\nClosed: {closed_count} | Skipped: {skipped_count} | Errors: {error_count}")

if error_count > 0:
    print("\nFor any ERRORs above, close those positions manually in the Alpaca dashboard.")

print("\nDone. Safe to delete this script after use.")
