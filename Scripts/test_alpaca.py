"""
Quick test to verify Alpaca connection and data feed.
Run with: python test_alpaca.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
import alpaca_trade_api as tradeapi

print("=" * 50)
print("  ALPACA CONNECTION TEST")
print("=" * 50)
print(f"  URL: {ALPACA_BASE_URL}")
print(f"  Key: {ALPACA_API_KEY[:8]}...")
print()

try:
    api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)

    # Test 1 - Account
    print("Testing account connection...")
    acct = api.get_account()
    print(f"  [OK] Account active: ${float(acct.equity):,.2f} equity")
    print(f"  [OK] Buying power:   ${float(acct.buying_power):,.2f}")
    print()

    # Test 2 - Market data
    print("Testing market data feed (AAPL)...")
    bars = api.get_bars("AAPL", "5Min", limit=5).df
    if bars.empty:
        print("  [WARN] No bars returned - market may be closed or data feed issue")
    else:
        print(f"  [OK] Got {len(bars)} bars")
        print(f"  Latest close: ${bars['close'].iloc[-1]:.2f}")
    print()

    # Test 3 - Multiple symbols
    print("Testing watchlist symbols...")
    symbols = ["AAPL", "MSFT", "NVDA", "TSLA"]
    for sym in symbols:
        try:
            b = api.get_bars(sym, "5Min", limit=3).df
            status = f"[OK] {len(b)} bars, close=${b['close'].iloc[-1]:.2f}" if not b.empty else "[WARN] No data"
        except Exception as e:
            status = f"[ERROR] {str(e)[:40]}"
        print(f"  {sym}: {status}")

    print()
    print("=" * 50)
    print("  Connection test complete!")
    print("=" * 50)

except Exception as e:
    print(f"  [ERROR] Connection failed: {e}")
    print()
    print("  Check your ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
