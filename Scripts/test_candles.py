from scanners.market_scanner import scanner

df = scanner.crypto_scanner.get_ohlcv('BTC/USD', timeframe='5m', limit=120)
if df is not None:
    print(f'Rows: {len(df)}')
    print(df.tail(3))
else:
    print('None returned')