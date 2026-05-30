from scanners.market_scanner import scanner

# Try the actual method the strategy engine calls
try:
    df = scanner.crypto_scanner.get_bars('BTC/USD', timeframe='5m', limit=120)
    print(f'get_bars rows: {len(df)}')
except Exception as e:
    print(f'get_bars error: {e}')

# Also check what pairs are actually working
try:
    df2 = scanner.crypto_scanner.get_ohlcv('ETH/USD', timeframe='5m', limit=120)
    print(f'ETH get_ohlcv rows: {len(df2) if df2 is not None else None}')
except Exception as e:
    print(f'ETH error: {e}')

# Check candles attribute name
print(f'CryptoScanner methods: {[m for m in dir(scanner.crypto_scanner) if not m.startswith("_")]}')