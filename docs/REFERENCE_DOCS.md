# Reference Documents Index
Last updated: April 23, 2026

This file lists every reference and guide document in the repo.
Read these when you need to understand how something works,
add a new strategy, or troubleshoot a component.

---

## 📖 Docs folder, Docs (Read First)

| File | Purpose |
|---|---|
| `README.md` | Installation guide, first-time setup, requirements |
| `CHAT_COMMANDS.md` | Every dashboard chat command with examples |
| `REFERENCE_DOCS.md` | This file — index of all reference documents |
| `PROJECT_SUMMARY.md` | Full architecture overview, all components explained |
| `CLAUDE.md` | Claude Code context — for AI assistant sessions |
| `.env.template` | Template for your API keys — copy to `.env` and fill in |
| `config.py` | All configurable settings — position sizes, stop loss %, watchlists |
| `requirements.txt` | Python package dependencies |
| `FileMap.txt` | Quick file structure reference |
| `CommandNotes.txt` | PowerShell commands for common tasks |

---

## 📊 Strategies (`strategies/`)

| File | Purpose |
|---|---|
| `strategies/INTEGRATION_GUIDE.py` | **How to add a new strategy** — read this before writing any new strategy |
| `strategies/base_strategy.py` | Abstract base class all strategies inherit from — read to understand structure |
| `strategies/strategy_engine.py` | Orchestrates all 11 strategies, auto-disable logic, performance tracking |

### The 11 Strategies
| File | Strategy | Status | Notes |
|---|---|---|---|
| `rsi_momentum.py` | RSI Momentum | Active | RSI reversal from extremes |
| `bollinger_breakout.py` | Bollinger Breakout | Active | OBV triple filter added |
| `ema_crossover.py` | EMA Crossover | Disabled | Fast/slow EMA cross |
| `mean_reversion.py` | Mean Reversion | Active | Fades extreme deviations |
| `scalp_master.py` | Scalp Master | Active | Tight stops, quick profits |
| `swing_trader.py` | Swing Trader | Disabled | Wide stops, big targets |
| `grid_bot.py` | Grid Bot | Disabled | Ranging markets only |
| `dca_accumulator.py` | DCA Accumulator | Active | Blue chip dip buying |
| `vwap_momentum.py` | VWAP Momentum | Active | VWAP + EMA 5/13/34 |
| `hammer_reversal.py` | Hammer Reversal | Active | SOL/USD only, 5m |
| `orb_breakout.py` | ORB Breakout | Active | GO whitelist stocks, 5m, VWAP exit |

---

## 🏦 Brokers & Data (`core/`, `data/`)

| File | Purpose |
|---|---|
| `data/BROKER_INTEGRATION_GUIDE.py` | **How to add a new broker** — step by step |
| `core/trade_executor.py` | Routes orders to correct broker, bracket order handling |
| `core/broker_manager.py` | Multi-broker management, balance tracking |
| `core/ibkr_executor.py` | Interactive Brokers integration (needs TWS running) |
| `core/kraken_executor.py` | Kraken crypto trading via CCXT |

---

## 🛡️ Risk & Position Management

| File | Purpose |
|---|---|
| `core/risk_manager.py` | ALL risk rules — position sizing, stop loss, daily limits |
| `core/position_monitor.py` | Watches open trades — trailing stops, stale check, VWAP exit |
| `core/settlement_tracker.py` | T+1 settlement tracking for IBKR cash accounts |

---

## 🤖 Intelligence & AI

| File | Purpose |
|---|---|
| `intelligence/chat_interface.py` | Haiku chat bot — pre-parse commands, morning scan |
| `intelligence/condition_detector.py` | Market condition classifier (trending/ranging/volatile) |
| `intelligence/ml_scorer.py` | Random Forest ML model, learns from trade history |
| `intelligence/backtester.py` | Historical backtesting engine |

---

## 🛠️ Utility Scripts (Root)

| File | Purpose | When to use |
|---|---|---|
| `UnHaltBot.py` | Clears trading halt state | Bot halted and won't resume |
| `flush_trade.py` | Force-close a stuck open trade in DB | Ghost position in dashboard |
| `flush_closed_trade.py` | Remove a wrongly-closed trade | Bad DB entry |
| `flush_session.py` | Reset today's session counters | P&L/trade count stuck |
| `resume_trading.py` | Resume trading via command line | Haiku chat not working |
| `dashboard_strategy_fix.py` | Enable/disable strategies via CLI | Haiku fumbled the command |
| `TailLog.ps1` | Live log tail in PowerShell | Watching trades in real time |
| `start_bot.bat` | Windows batch file to start bot | Double-click to launch |

---

## 📊 Watchlists (`watchlists/`)

| File | Purpose |
|---|---|
| `watchlists/crypto.txt` | Crypto pairs scanned 24/7 |
| `watchlists/stocks.txt` | Stock symbols scanned Mon-Fri 10-3:45 ET (if separate from config.py) |

---

## ⬇️ Reading Order for New Installers

1. `README.md` — install Python, clone repo, set up .env
2. `.env.template` — understand what API keys are needed
3. `config.py` — review all settings, adjust position sizes, watchlists
4. `PROJECT_SUMMARY.md` — understand the full architecture
5. `CHAT_COMMANDS.md` — learn how to talk to the bot
6. `strategies/INTEGRATION_GUIDE.py` — when ready to add strategies
7. `data/BROKER_INTEGRATION_GUIDE.py` — when ready to add brokers
