# Autonomous Day Trading Bot v2

A fully autonomous AI-powered day trading bot with:
- **27 trading strategies** (stocks + crypto)
- **Streamlit dashboard** with live P&L, positions, backtester
- **Claude AI chat interface** (Haiku model) for natural language control
- **Multi-broker support**: Alpaca (stocks), Coinbase, Kraken, IBKR
- **IRS Form 8949** compatible tax records
- **Risk management**: position limits, trailing stops, daily loss ceiling

---

## ⚠️ Disclaimer

This bot trades real money. Paper trading mode is enabled by default.
**Test thoroughly in paper mode before switching to live trading.**
Past backtest performance does not guarantee future results.

---

## ⚙️ Requirements

- **Python 3.12** (required — tested on 3.12 only)
- **Windows 10/11** (developed on Windows, Linux/Mac may work)
- **Git** for version control
- **4GB+ RAM** recommended

---

## 🚀 Installation

### Step 1 — Clone the repo
```powershell
git clone https://github.com/PNWSkyHippy/Trading_Bot_V2.git
cd Trading_Bot_V2
```

### Step 2 — Create Python virtual environment
```powershell
python -m venv venv312
venv312\Scripts\activate
```

### Step 3 — Install dependencies
```powershell
pip install -r requirements.txt
```

### Step 4 — Set up API keys
Copy the template and fill in your keys:
```powershell
copy .env.template .env
notepad .env
```

See **API Keys** section below for where to get each key.

### Step 5 — Configure settings
Open `config.py` and review/adjust:
- `STARTING_CAPITAL` — your starting capital amount
- `MAX_POSITION_PCT` — max % of capital per trade (default 2%)
- `DEFAULT_STOP_LOSS_PCT` — stop loss % (default 1.5%)
- `DEFAULT_TAKE_PROFIT_PCT` — take profit % (default 3%)
- `STOCK_WATCHLIST` — stocks to scan
- `CRYPTO_WATCHLIST` — crypto pairs to scan

### Step 6 — Start the bot
```powershell
# Terminal 1: Start the trading engine
python bot_engine.py

# Terminal 2: Start the dashboard
python web_dashboard.py
```

Dashboard opens at the configured port (see web_dashboard.py).

---

## 🔑 API Keys

All keys go in your `.env` file. Never commit `.env` to git.

| Key | Where to get it | Required? |
|---|---|---|
| `ALPACA_API_KEY` | [alpaca.markets](https://alpaca.markets) → Paper Trading | Yes |
| `ALPACA_SECRET_KEY` | Same as above | Yes |
| `COINBASE_API_KEY` | [Coinbase Advanced Trade API](https://www.coinbase.com/settings/api) | Optional |
| `COINBASE_SECRET_KEY` | Same as above | Optional |
| `KRAKEN_API_KEY` | [Kraken API](https://www.kraken.com/u/security/api) | Optional |
| `KRAKEN_SECRET_KEY` | Same as above | Optional |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | For chat only |
| `EMAIL_ADDRESS` | Your Gmail address | For daily reports |
| `EMAIL_PASSWORD` | Gmail App Password (not regular password) | For daily reports |
| `EMAIL_RECIPIENT` | Where to send reports | For daily reports |

### Alpaca Setup
- Sign up at alpaca.markets
- Go to **Paper Trading** → Generate API keys
- Use paper URL: `https://paper-api.alpaca.markets`
- For live trading change to: `https://api.alpaca.markets`

### Gmail App Password
- Google Account → Security → 2-Step Verification → App Passwords
- Generate a password for "Mail"
- Use that 16-character password as `EMAIL_PASSWORD`

---

## 📦 Key Configurable Settings (`config.py`)

```python
# Capital
STARTING_CAPITAL = 100.0        # Your starting capital

# Position sizing
MAX_POSITION_PCT = 2.0          # Max % of capital per trade
DEFAULT_STOP_LOSS_PCT = 1.5     # Stop loss %
DEFAULT_TAKE_PROFIT_PCT = 3.0   # Take profit %
TRAILING_STOP_PCT = 1.0         # Trailing stop gap %

# Daily limits
MAX_DAILY_LOSS_PCT = 10.0       # Halt trading if down this % in a day
MAX_CONSECUTIVE_LOSSES = 10     # Halt after this many losses in a row
MAX_OPEN_POSITIONS = 5          # Max simultaneous trades

# Scan intervals
STOCK_SCAN_INTERVAL_SEC = 60    # Stock scan every 60 seconds
CRYPTO_SCAN_INTERVAL_SEC = 300  # Crypto scan every 5 minutes

# Broker mode
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"  # Change for live
```

---

## 💻 Dashboard

Open the dashboard URL after running `python web_dashboard.py`.

The current live dashboard is the HTML dashboard served by `web_dashboard.py` on
`http://localhost:8125`. It uses the files in `Html-Files/` for the browser UI
and JSON endpoints in `web_dashboard.py` for bot/database/watchlist/backtester
actions. For implementation details, see `WEB_DASHBOARD_TECHNICAL_REFERENCE.md`.

**Sections:**
- **Top metrics** — capital, daily P&L, open positions, win rate
- **Capital growth chart** — 30-day equity curve
- **Open positions** — live P&L, trailing stops, close button
- **Manual trade entry** — open a trade you spotted yourself
- **Today's trade log** — all closed trades with exit reason
- **Backtester** — test any strategy on historical data
- **Chat interface** — talk to the bot in plain English
- **Sidebar** — risk controls, bot restart, tax export, withdrawals

---

## 🤖 Chat Interface

Type commands in the dashboard chat box. See `CHAT_COMMANDS.md` for the full list.

Quick reference:
```
morning scan          → Top trading candidates for the day
enable orb_breakout   → Enable a strategy
disable grid_bot      → Disable a strategy
list strategies       → Show all 27 with ON/OFF status
pause trading         → Halt new trades
resume trading        → Resume trading
withdraw $500 for rent → Record a withdrawal
show my stats         → Full performance summary
```

---

## ⭐ Strategies

27 strategies run simultaneously. Each scores every symbol independently.
Highest score per symbol wins. See `strategies/INTEGRATION_GUIDE.py` to add your own.

| Strategy | Style | Default |
|---|---|---|
| RSI Momentum | Momentum | Disabled |
| Bollinger Breakout | Breakout + OBV | **Enabled** |
| EMA Crossover | Trend | Disabled |
| Mean Reversion | Mean reversion | **Enabled** |
| Scalp Master | Scalping | **Enabled** |
| Swing Trader | Swing | Disabled |
| Grid Bot | Range | Disabled |
| DCA Accumulator | Accumulation | **Enabled** |
| VWAP Momentum | VWAP trend | **Enabled** |
| Hammer Reversal | Reversal (SOL only) | **Enabled** |
| ORB Breakout | Opening range | **Enabled** |

---

## 📧 Daily Reports

At 5 PM ET the bot emails an HTML report with:
- P&L summary, win rate, trade log
- Strategy performance breakdown
- Claude AI activity log (what Haiku did)
- Capital growth chart

Set `EMAIL_ADDRESS`, `EMAIL_PASSWORD`, `EMAIL_RECIPIENT` in `.env` to enable.

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---|---|
| Bot halted, won't resume | Run `python UnHaltBot.py` |
| Ghost positions in dashboard | Use dashboard **Close Now** button |
| P&L % wrong after restart | Fixed in latest version — git pull |
| Haiku won't enable a strategy | Use `python dashboard_strategy_fix.py` |
| Strategy not firing | Check `bot_state` table in DB Browser for `strategy_X_enabled` |
| No trades executing | Check `logs/bot.log` for signal details |
| Dashboard won't load | Ensure `python web_dashboard.py` is running |

---

## 📖 Documentation

| File | Purpose |
|---|---|
| `REFERENCE_DOCS.md` | Index of all reference documents |
| `CHAT_COMMANDS.md` | All chat commands with examples |
| `WEB_DASHBOARD_TECHNICAL_REFERENCE.md` | Live HTML dashboard architecture, API endpoints, panel data flow |
| `PROJECT_SUMMARY.md` | Full architecture deep-dive |
| `config.py` | All settings with comments |
| `strategies/INTEGRATION_GUIDE.py` | How to add a new strategy |
| `data/BROKER_INTEGRATION_GUIDE.py` | How to add a new broker |

---

## 🔒 Security Notes

- `.env` is in `.gitignore` — your API keys are never committed
- Paper trading mode is default — no real money until you change the URL
- All withdrawals require explicit confirmation from the chat bot
- Halt commands require exact wording to prevent accidental triggers

---

## 📜 License

Personal use only. Not for redistribution without permission.
