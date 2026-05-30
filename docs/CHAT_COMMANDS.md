# Trading Bot Chat Command Reference
Last updated: May 10, 2026

Type any of these in the **dashboard chat box** (🤖 Chat with Your Bot section).
Commands marked ⚡ are handled by the **pre-parse layer** — they execute instantly
without going through Haiku, so they always work reliably.

---

## 🛑 Trading Control

| What you type | What happens |
|---|---|
| `pause trading` | Halts all new trades immediately |
| `halt trading` | Same as pause |
| `stop all trades` | Same as pause |
| `resume trading` | Re-enables trading |

> ⚠️ These are HIGH RISK commands. Haiku requires explicit wording — vague phrases like
> "things are slow" or "resources used up" will NOT trigger a halt.

---

## 📊 Status & Stats

| What you type | What happens |
|---|---|
| `show my stats` | Full performance summary |
| `how am I doing` | Same — P&L, win rate, open positions |
| `dashboard summary` ⚡ | Capital, cash, open positions, closed trades, win rate, halt state |
| `dashboard status` ⚡ | Same as dashboard summary |
| `quick summary` ⚡ | Same as dashboard summary |
| `bot status` ⚡ | Trading state, capital, cash, daily P&L, trades, loss streak |
| `capital summary` ⚡ | Same as bot status |
| `capital breakdown` ⚡ | Per-broker balance breakdown |
| `risk status` ⚡ | Trading active/halted, daily P&L, trades, loss streak |
| `trading status` ⚡ | Same as risk status |
| `open positions` ⚡ | Lists current open positions with entry, current price, P&L |
| `show positions` ⚡ | Same as open positions |
| `daily performance` ⚡ | Today's closed trades, win/loss count, win rate, realized P&L |
| `recent trades` ⚡ | Last 10 closed trades today |
| `trade log` ⚡ | Same as recent trades |
| `strategy stats` ⚡ | Win rate, P&L, profit factor per strategy |
| `local commands` ⚡ | Shows the commands that bypass Haiku/API |
| `chat help` ⚡ | Same as local commands |

---

## 🔍 Morning Scan ⚡

| What you type | What happens |
|---|---|
| `morning scan` | Scans ORB whitelist, surfaces top 5 candidates |
| `what should I trade` | Same |
| `top setups` | Same |
| `best setups` | Same |
| `what to trade` | Same |
| `scan for me` | Same |

> 💡 Best used at 10:00 AM ET. Shows move %, volume ratio, direction for each candidate.
> Wait for 9:45 ET ORB window before entering. Exit when price crosses VWAP or EOD.

---

## ♟️ Strategy Management ⚡

All enable/disable commands bypass Haiku and execute instantly.

| What you type | What happens |
|---|---|
| `enable orb_breakout` | Enables ORB strategy |
| `disable orb_breakout` | Disables ORB strategy |
| `enable hammer_reversal` | Enables Hammer Reversal |
| `disable hammer_reversal` | Disables Hammer Reversal |
| `enable scalp_master` | Enables Scalp Master |
| `disable scalp_master` | Disables Scalp Master |
| `enable bollinger_breakout` | Enables Bollinger Breakout |
| `disable bollinger_breakout` | Disables Bollinger Breakout |
| `enable vwap_momentum` | Enables VWAP Momentum |
| `disable vwap_momentum` | Disables VWAP Momentum |
  `enable vwap_confirmed_orb` | Enables VWAP Confirmed |
| `disable vwap_confirmed_orb` | Disables VWAP Confirmed |
| `disable rsi_momentum` | Disables RSI Momentum |
| `enable rsi_momentum` | Enables RSI Momentum |
| `enable ema_crossover` | Enables EMA Crossover |
| `disable ema_crossover` | Disables EMA Crossover |
| `enable bollinger_squeeze` | Enables Bollinger Squeeze |
| `disable bollinger_squeeze` | Disables Bollinger Squeeze |
| `enable mean_reversion` | Enables Mean Reversion |
| `disable mean_reversion` | Disables Mean Reversion |
| `enable swing_trader` | Enables Swing Trader |
| `disable swing_trader` | Disables Swing Trader |
| `enable vdmr_strategy` | Enables VDMR |
| `disable vdmr_strategy` | Disables VDMR |
| `enable rsi_dip_spike_v4` | Enables RSI Dip Spike V4 |
| `disable rsi_dip_spike_v4` | Disables RSI Dip Spike V4 |
| `enable ema_crossover` | Enables EMA Crossover |
| `disable ema_crossover` | Disables EMA Crossover |
| `enable adaptive_regime` | Enables Adaptive Regime |
| `disable adaptive_regime` | Disables Adaptive Regime |
| `disable grid_bot` | Disables Grid Bot |
| `ensable grid_bot` | Enables Grid Bot |
| `enable ecb_strategy` | Enables ECB |
| `disable ecb_strategy` | Disables ECB |
| `list strategies` | Shows all 11 with ON/OFF status |
| `show strategies` | Same as list strategies |
| `enabled strategies` | Same as list strategies |
| `disabled strategies` | Same as list strategies |
| `strategy stats` | Win rate and P&L per strategy |

> ✅ Most reliable wording: `list strategies`. Short slang like `show strats`
> or typos like `show starts` are not guaranteed to match the local parser.

### Strategy names (for reference)
```
rsi_momentum        bollinger_breakout    ema_crossover
mean_reversion      scalp_master          swing_trader
grid_bot            dca_accumulator       vwap_momentum
hammer_reversal     orb_breakout          adaptive_regime
rsi_dip_spike_v4    ecb                   
bollinger_squeeze   vdmr                  vwap_confirmed_orb  
```

---

## 🏦 Broker Management

| What you type | What happens |
|---|---|
| `list brokers` | Shows all registered brokers |
| `capital breakdown` ⚡ | Per-broker balance |
| `broker breakdown` ⚡ | Same as capital breakdown |
| `broker balances` ⚡ | Same as capital breakdown |
| `capital by broker` ⚡ | Same as capital breakdown |
| `I deposited $500 into Kraken` | Records fund injection (informational only — you move money manually) |
| `I withdrew $200 from Alpaca` | Records fund withdrawal (informational only) |
| `enable kraken` | Enables Kraken broker |
| `disable kraken` | Disables Kraken broker |

---

## 💰 Financial

| What you type | What happens |
|---|---|
| `withdraw $500 for rent` | Deducts from capital tracking |
| `export my taxes for 2025` | Saves IRS Form 8949 CSV to exports/ folder |
| `lower my risk to 1.5% per trade` | Adjusts MAX_POSITION_PCT |

> ⚠️ Withdrawals require explicit confirmation — Haiku will ask before executing.

---

## ⚙️ Risk Settings

| What you type | What happens |
|---|---|
| `set risk to 1.5%` | Changes position size to 1.5% of capital per trade |
| `lower my risk` | Haiku will ask what % you want |
| `raise my risk` | Haiku will ask what % you want |

---

## 💬 Natural Language (Haiku handles these)

These don't have fixed syntax — just ask naturally:

- "Which strategy is performing best?"
- "Am I on track for my daily goal?"
- "What's my win rate this week?"
- "Close all my positions" *(Haiku will confirm first)*
- "Generate today's report"

---

## 🔧 Fallback — If Haiku Fumbles a Command

If the chat doesn't work, use PowerShell directly:

```powershell
cd C:\users\linda\trading_bot_v2
python -c "
from strategies.strategy_engine import strategy_engine
print(strategy_engine.enable_strategy('orb_breakout'))
print(strategy_engine.disable_strategy('grid_bot'))
print(strategy_engine.list_strategies())
"
```

Or use **DB Browser for SQLite**:
- Open `data/trading_bot.db`
- Browse Data → `bot_state` table
- Find `strategy_orb_breakout_enabled` → change value to `"true"` or `"false"`
- Write Changes

---

## 📋 Quick Reference Card

```
SCAN:      morning scan
PAUSE:     pause trading / resume trading
STRATEGY:  enable/disable <strategy_name>
LIST:      list strategies / strategy stats
BROKERS:   list brokers / capital breakdown
MONEY:     withdraw $X for <reason>
TAXES:     export my taxes for 2025
RISK:      set risk to 1.5%
STATUS:    show my stats / how am I doing
LOCAL:     local commands / dashboard summary / risk status
TRADES:    recent trades / trade log / daily performance
POSITIONS: open positions / show positions
```
