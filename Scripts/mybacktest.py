import yfinance as yf
import pandas as pd

# 1. Load 12 months of historical data
data = yf.download("BTC-USD", period="1y", interval="1d")

# 2. Indicators & Signal (Trend + Momentum)
data['EMA200'] = data['Close'].ewm(span=200, adjust=False).mean()
# Assuming calculate_rsi function exists
data['RSI'] = calculate_rsi(data['Close'], period=14) 
data['Signal'] = 0
data.loc[(data['Close'] > data['EMA200']) & (data['RSI'] < 35), 'Signal'] = 1

# 3. Calculate Strategy vs Market Returns
data['Market_Ret'] = data['Close'].pct_change()
data['Strategy_Ret'] = data['Signal'].shift(1) * data['Market_Ret']

# 4. Results
print(f"Market Return: {(data['Market_Ret'] + 1).prod() - 1:.2%}")
print(f"AI Strategy Return: {(data['Strategy_Ret'] + 1).prod() - 1:.2%}")
