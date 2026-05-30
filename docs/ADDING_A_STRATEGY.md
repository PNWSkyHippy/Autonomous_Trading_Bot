# Adding a New Strategy — Complete Integration Checklist

> **Use this doc every time you add a strategy.** Missing even one touchpoint
> will silently break backtesting, live exits, or the chat interface.

---

## Naming Conventions

| What | Convention | Example |
|---|---|---|
| File | `strategies/<snake_name>_strategy.py` | `vdmr_strategy.py` |
| Class | `<PascalName>Strategy` | `VDMRStrategy` |
| `strategy_name` attr | same snake name as the file stem | `"vdmr_strategy"` |
| Backtester key | same as `strategy_name` | `"vdmr_strategy"` |

The file stem, the class attr `self.strategy_name`, and the backtester map key
**must all match exactly** — the position monitor uses `trade["strategy_name"]`
to route exits.

---

## Step 1 — Create the Strategy File

`strategies/<snake_name>_strategy.py`

Minimum skeleton:

```python
from dataclasses import dataclass
from typing import Optional
import pandas as pd

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


@dataclass
class MyParams:
    atr_len:  int   = 14
    sl_mult:  float = 2.0
    tp_mult:  float = 2.0
    max_bars: int   = 24   # time stop


class MyStrategy(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.strategy_name           = "my_strategy"   # ← must match file stem
        self.params                  = MyParams()
        self.stop_loss_pct           = 2.0
        self.take_profit_pct         = 2.0
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.candle_limit            = 250
        # These flags skip ML scoring and the reviewer queue
        self.reviewer_exempt         = True
        self.ml_exempt               = True
        # Tells the backtester to use this strategy's own time stop logic
        self.time_stop_profile       = "strategy_defined"

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        MIN_BARS = 230
        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None
        try:
            # ... your signal logic ...
            return self._make_signal(
                symbol          = symbol,
                direction       = "long",   # "long" or "short"
                score           = 0.65,
                reason          = "My strategy: <why>",
                stop_loss_pct   = 2.0,
                take_profit_pct = 2.0,
                metadata={
                    "strategy_name":               "my_strategy",
                    "entry_timeframe":             "1h",
                    "structural_stop_price":       0.0,   # fill in real value
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"MyStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str,
                          entry_metadata: Optional[dict] = None) -> Optional[str]:
        """Time stop — only needed if time_stop_profile = 'strategy_defined'."""
        meta      = entry_metadata or {}
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= self.params.max_bars:
            return "my_strategy_time_stop"
        return None
```

---

## Step 2 — Register in strategy_engine.py

File: `strategies/strategy_engine.py`

**A. Add import** (keep alphabetical order):
```python
from strategies.my_strategy import MyStrategy
```

**B. Add to the strategies list** in `_build_strategies()`:
```python
MyStrategy(),      # Strategy N — one-line description (INCUBATE / LIVE)
```

The engine deduplicates by symbol (highest score wins), so order doesn't matter
for correctness, but it matters for the comment numbering you maintain.

---

## Step 3 — Register in the Backtester

File: `intelligence/backtester.py`

Find `_load_strategy()` (~line 738) and add your entry to `strategy_map`:

```python
strategy_map = {
    ...
    "my_strategy":  ("strategies.my_strategy",  "MyStrategy"),
}
```

Without this entry `run_strategy("my_strategy", ...)` returns `None` and every
backtest call silently does nothing.

---

## Step 4 — Position Monitor: Time Stop Exemption

File: `core/position_monitor.py`

### 4A — Hard-coded TIME_STOP_EXEMPT set (~line 491)

If your strategy has `time_stop_profile = "strategy_defined"` and its own
time stop is **longer than 8 hours**, you MUST add it here or the generic 8-hour
stop will override your strategy's exit timing:

```python
TIME_STOP_EXEMPT = {"ecb_strategy", "grid_bot", "dca_accumulator", "my_strategy"}
```

**When to add:**
- Strategy max_bars × candle_timeframe > 8 hours → **add to exempt set**
- Strategy max_bars × candle_timeframe ≤ 8 hours → **no change needed**
  (generic stop fires after strategy stop — harmless)

Current strategies and their effective time stops:

| Strategy | max_bars | TF | Effective limit | Needs exempt? |
|---|---|---|---|---|
| `mr_02_vef` | 24 | 1h | 24h | **YES** |
| `mr_03_fbs` | 36 | 1h | 36h | **YES** |
| `mr_04_fvg` | 48 | 1h | 48h | **YES** |
| `rsi_dip_spike_v4` | 48 | 1h | 48h | YES (hardcoded separately) |
| `ecb_strategy` | 24 | 1h | 24h | YES (already in set) |

> **⚠️ Known gap:** `mr_02_vef`, `mr_03_fbs`, and `mr_04_fvg` are NOT yet in
> the TIME_STOP_EXEMPT set. They will be cut by the 8-hour generic stop.
> Add them when you promote them from INCUBATE to LIVE.

### 4B — Custom exit delegation (~line 1265)

The position monitor does NOT auto-call `check_custom_exit()` for every strategy.
It only calls it for two hardcoded strategies (`adaptive_regime`, `rsi_dip_spike_v4`).

If your strategy needs its own **live** exit logic beyond TP/SL/time-stop
(e.g. RSI-based exit, indicator cross), add an explicit delegation block:

```python
# ── 4d. MY STRATEGY EXIT ─────────────────────────────────────
if strategy == "my_strategy":
    my_exit = self._check_my_strategy_exit(trade, current_price, age_seconds)
    if my_exit:
        self._db.set_state(f"momentum_ext_{trade_id}", 0)
        self._attempt_close(trade, current_price, my_exit)
        return
```

And add a helper method `_check_my_strategy_exit()` similar to
`_check_rsi_dip_spike_exit()` (~line 888).

**For mean-reversion time-stop-only strategies** (MR-02/03/04 style), you do
NOT need a custom exit block — the backtester's generic loop calls
`check_custom_exit()` correctly, and the live side relies on TP/SL + the 8-hour
generic stop (or TIME_STOP_EXEMPT + live timer if you add the exempt entry).

---

## Step 5 — config.py

File: `config.py`

**Usually no changes needed.** Config holds API keys and global risk params.

Only touch config.py if your strategy needs a **global tunable** that other
components need to read (e.g. a universe-wide max position size). For
strategy-specific params, use a `@dataclass` params class inside the strategy
file (see Step 1 skeleton — `MyParams`).

---

## Step 6 — Chat Interface (already auto-populated)

File: `intelligence/chat_interface.py`

The chat interface reads the live list from `strategy_engine.list_strategies()`,
so **no manual edits needed** once you complete Step 2. The `list_strategies`
command and Haiku's strategy queries will show your strategy automatically.

---

## Step 7 — Verify end-to-end

```bash
# 1. Import smoke test
cd C:\Users\Linda\trading_bot_v2
python -c "from strategies.my_strategy import MyStrategy; s = MyStrategy(); print(s.strategy_name)"

# 2. Backtester smoke test (will fail gracefully if no data, but must not error on import)
python intelligence/backtester.py --symbol BTCUSDT --strategy my_strategy --days 30

# 3. Strategy engine smoke test
python -c "from strategies.strategy_engine import StrategyEngine; e = StrategyEngine(); print([s['strategy_name'] for s in e.list_strategies()])"
```

---

## Quick Reference — All Touchpoints

| Step | File | What to do | Required? |
|---|---|---|---|
| 1 | `strategies/<name>_strategy.py` | Create strategy class | ✅ Always |
| 2A | `strategies/strategy_engine.py` | Add import | ✅ Always |
| 2B | `strategies/strategy_engine.py` | Add to strategies list | ✅ Always |
| 3 | `intelligence/backtester.py` | Add to `_load_strategy()` map | ✅ Always |
| 4A | `core/position_monitor.py` | Add to `TIME_STOP_EXEMPT` | ✅ If time stop > 8h |
| 4B | `core/position_monitor.py` | Add `_check_X_exit()` block | Only if custom live exit logic |
| 5 | `config.py` | Add global param | Only if truly global |
| 6 | `intelligence/chat_interface.py` | Nothing — auto-populated | — |

---

## Common Mistakes

1. **strategy_name doesn't match the backtester key** — backtester silently
   returns `None` and runs zero trades.

2. **Forgot `intelligence/backtester.py` `_load_strategy()` entry** — same result
   as #1.

3. **time_stop_profile = "strategy_defined" but not in TIME_STOP_EXEMPT** — the
   8-hour generic stop fires in live trading before your strategy's intended exit.

4. **`stock_enabled = True` accidentally** — strategy fires on Alpaca stock
   universe with crypto-tuned parameters.

5. **candle_limit too small** — `_check_enough_candles()` will silently skip
   symbols unless the limit covers your longest indicator lookback + buffer.
   Formula: `max(SMA_period, BB_period, lookback) + warmup + 20`.

6. **Missing `try/except` in `analyze()`** — one bad candle crashes the entire
   scan cycle for all symbols.
