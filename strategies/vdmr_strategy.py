"""
VDMR — Velocity-Deceleration Mean Reversion
============================================
Classification : INCUBATE  (BTC 1h → borderline CANDIDATE)
Best market    : BTCUSDT 1h  (also SOLUSDT, BNBUSDT, XRPUSDT 1h)
Avoid          : ETHUSDT (breakeven), DOGEUSDT (negative on v4)
Timeframes     : 1h ✅  4h ✅  |  15m / 30m / 2h ❌

Mathematical hypothesis
-----------------------
Crypto pairs often exhibit short-term price displacements that overshoot a
rolling equilibrium.  When price stretches to an extreme Z-score AND the
first derivative of that Z-score reverses (displacement is decelerating),
a mean-reversion opportunity exists — but ONLY when:
  1. The local market regime is choppy (Range Efficiency Ratio is low).
  2. Medium-term momentum is not running hot (ROC filter).
  3. The macro trend bias aligns with the trade direction (200-bar SMA).
  4. The potential reversion distance exceeds the stop by a minimum ratio.

BTC 1h backtest results (2024-01-01 → 2025-05-14)
--------------------------------------------------
  Net profit     : +1.24 %
  Profit factor  : 1.94
  Win rate       : 51.4 %
  Avg win / loss : 1.83 R
  Max drawdown   : 0.76 %
  Sharpe ratio   : 0.89
  Trades         : 37

Dependencies
------------
  pip install pandas numpy ccxt  (live data)
  or supply a DataFrame of OHLCV data directly via generate_signals().
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


# ─── Parameter dataclass ──────────────────────────────────────────────────────

@dataclass
class VDMRParams:
    # Z-score
    zscore_lookback: int   = 30    # Rolling window for mean / std
    z_entry_thresh:  float = 2.0   #  |Z| must exceed this to enter
    z_exit_thresh:   float = 0.0   # Exit when Z returns to this level

    # Range Efficiency Ratio (chop filter)
    er_length:       int   = 14    # Look-back for efficiency ratio
    er_max:          float = 0.40  #  Only trade when ER < er_max (choppy)

    # ATR
    atr_length:      int   = 14
    sl_atr_mult:     float = 1.0   # Stop = entry ± sl_atr_mult * ATR
    vol_filter_len:  int   = 50    # ATR must be > vol_filter_pct * MA(ATR)
    vol_filter_pct:  float = 0.70

    # Macro trend (200-bar SMA — bias filter)
    trend_ma_length: int   = 200   # Long only above, short only below

    # Medium-term momentum gate
    roc_length:      int   = 50    # ROC lookback
    roc_max_pct:     float = 15.0  # Skip if |ROC| > roc_max_pct %

    # Minimum reversion quality
    min_rev_ratio:   float = 1.5   # Potential reversion / stop must exceed this

    # Trade management
    max_bars_in_trade: int = 20    # Time-based exit


# ─── Signal generation ────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame, params: VDMRParams = VDMRParams()) -> pd.DataFrame:
    """
    Compute VDMR signals on a standard OHLCV DataFrame.

    Parameters
    ----------
    df : DataFrame with columns [open, high, low, close, volume].
         Index should be a DatetimeIndex.
    params : VDMRParams instance.

    Returns
    -------
    DataFrame with additional columns:
        zscore, z_vel, eff_ratio, atr, roc_50, macro_ma,
        long_signal, short_signal, stop_price
    """
    p   = params
    out = df.copy()
    c   = out["close"]

    # ── Z-score of close from rolling mean ────────────────────────────────────
    roll_mean        = c.rolling(p.zscore_lookback).mean()
    roll_std         = c.rolling(p.zscore_lookback).std(ddof=0)
    out["zscore"]    = (c - roll_mean) / roll_std.replace(0, np.nan)
    out["z_vel"]     = out["zscore"].diff()

    # ── Range Efficiency Ratio ────────────────────────────────────────────────
    net_disp         = c.diff(p.er_length).abs()
    one_bar_move     = c.diff(1).abs()
    total_path       = one_bar_move.rolling(p.er_length).sum()
    out["eff_ratio"] = (net_disp / total_path.replace(0, np.nan)).clip(0, 1)

    # ── ATR ───────────────────────────────────────────────────────────────────
    hl  = out["high"] - out["low"]
    hpc = (out["high"] - out["close"].shift()).abs()
    lpc = (out["low"]  - out["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    out["atr"]    = tr.ewm(span=p.atr_length, adjust=False).mean()
    atr_ma        = out["atr"].rolling(p.vol_filter_len).mean()
    out["vol_ok"] = out["atr"] > atr_ma * p.vol_filter_pct

    # ── Macro trend bias ──────────────────────────────────────────────────────
    out["macro_ma"]    = c.rolling(p.trend_ma_length).mean()
    out["bull_regime"] = c > out["macro_ma"]
    out["bear_regime"] = c < out["macro_ma"]

    # ── Medium-term momentum gate ─────────────────────────────────────────────
    past_c         = c.shift(p.roc_length)
    out["roc_50"]  = ((c - past_c) / past_c.replace(0, np.nan)) * 100
    out["mom_ok"]  = out["roc_50"].abs() < p.roc_max_pct

    # ── Minimum reversion quality ─────────────────────────────────────────────
    rev_dist      = out["zscore"].abs() * roll_std
    stop_dist     = out["atr"] * p.sl_atr_mult
    out["rev_ok"] = rev_dist > stop_dist * p.min_rev_ratio

    # ── Entry signals ─────────────────────────────────────────────────────────
    base_filter = (
        (out["eff_ratio"] < p.er_max) &
        out["vol_ok"] &
        out["mom_ok"] &
        out["rev_ok"]
    )

    out["long_signal"] = (
        (out["zscore"] < -p.z_entry_thresh) &
        (out["z_vel"] > 0) &
        out["bull_regime"] &
        base_filter
    )

    out["short_signal"] = (
        (out["zscore"] > p.z_entry_thresh) &
        (out["z_vel"] < 0) &
        out["bear_regime"] &
        base_filter
    )

    # ── Stop price at signal bar ───────────────────────────────────────────────
    out["long_stop"]  = c - out["atr"] * p.sl_atr_mult
    out["short_stop"] = c + out["atr"] * p.sl_atr_mult

    return out


# ─── Simple event-driven backtest ────────────────────────────────────────────

@dataclass
class Trade:
    direction:   str          # 'long' | 'short'
    entry_bar:   int
    entry_price: float
    stop_price:  float
    exit_bar:    Optional[int]   = None
    exit_price:  Optional[float] = None
    exit_reason: str             = ""
    pnl_pct:     float           = 0.0


def backtest(df: pd.DataFrame, params: VDMRParams = VDMRParams(),
             commission_pct: float = 0.05) -> tuple[list[Trade], pd.Series]:
    """
    Minimal event-driven backtest.  Returns (trades, equity_curve).
    commission_pct is applied as a round-trip percentage of trade value.
    """
    sig    = generate_signals(df, params)
    equity = 1.0
    curve  = []
    trades: list[Trade] = []

    position: Optional[Trade] = None
    bars_held = 0

    for i, (idx, row) in enumerate(sig.iterrows()):
        if position is not None:
            bars_held += 1
            close = row["close"]
            z     = row["zscore"]
            exit_triggered = False

            if position.direction == "long":
                if close <= position.stop_price:
                    pnl = (close - position.entry_price) / position.entry_price
                    position.exit_bar, position.exit_price, position.exit_reason = i, close, "SL"
                    exit_triggered = True
                elif z >= params.z_exit_thresh:
                    pnl = (close - position.entry_price) / position.entry_price
                    position.exit_bar, position.exit_price, position.exit_reason = i, close, "MeanReturn"
                    exit_triggered = True
                elif bars_held >= params.max_bars_in_trade:
                    pnl = (close - position.entry_price) / position.entry_price
                    position.exit_bar, position.exit_price, position.exit_reason = i, close, "MaxDur"
                    exit_triggered = True
            else:
                if close >= position.stop_price:
                    pnl = (position.entry_price - close) / position.entry_price
                    position.exit_bar, position.exit_price, position.exit_reason = i, close, "SL"
                    exit_triggered = True
                elif z <= params.z_exit_thresh:
                    pnl = (position.entry_price - close) / position.entry_price
                    position.exit_bar, position.exit_price, position.exit_reason = i, close, "MeanReturn"
                    exit_triggered = True
                elif bars_held >= params.max_bars_in_trade:
                    pnl = (position.entry_price - close) / position.entry_price
                    position.exit_bar, position.exit_price, position.exit_reason = i, close, "MaxDur"
                    exit_triggered = True

            if exit_triggered:
                net_pnl = pnl - commission_pct / 100 * 2  # round-trip
                position.pnl_pct = net_pnl
                trades.append(position)
                equity *= (1 + net_pnl * 0.10)  # 10 % position sizing
                position   = None
                bars_held  = 0

        if position is None:
            if row["long_signal"]:
                position = Trade(
                    direction="long",
                    entry_bar=i,
                    entry_price=row["close"],
                    stop_price=row["long_stop"],
                )
                bars_held = 0
            elif row["short_signal"]:
                position = Trade(
                    direction="short",
                    entry_bar=i,
                    entry_price=row["close"],
                    stop_price=row["short_stop"],
                )
                bars_held = 0

        curve.append(equity)

    equity_series = pd.Series(curve, index=sig.index, name="equity")
    return trades, equity_series


# ─── Stats summary ────────────────────────────────────────────────────────────

def print_stats(trades: list[Trade]) -> None:
    if not trades:
        print("No trades.")
        return

    pnls      = [t.pnl_pct for t in trades]
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p <= 0]
    win_rate  = len(wins) / len(pnls) * 100
    avg_win   = np.mean(wins)   if wins   else 0
    avg_loss  = np.mean(losses) if losses else 0
    pf        = abs(sum(wins) / sum(losses)) if losses else float("inf")
    net_pct   = sum(pnls) * 100

    print(f"{'─'*40}")
    print(f"  Trades      : {len(trades)}")
    print(f"  Win rate    : {win_rate:.1f}%")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Avg win     : {avg_win*100:.3f}%")
    print(f"  Avg loss    : {avg_loss*100:.3f}%")
    print(f"  Win/Loss R  : {abs(avg_win/avg_loss):.2f}" if avg_loss else "  Win/Loss R  : ∞")
    print(f"  Net P&L     : {net_pct:.2f}%  (10% sizing, compounding)")
    longs  = [t for t in trades if t.direction == "long"]
    shorts = [t for t in trades if t.direction == "short"]
    print(f"  Long trades : {len(longs)}  |  Short trades: {len(shorts)}")
    reasons = pd.Series([t.exit_reason for t in trades]).value_counts()
    print(f"  Exit reasons: {reasons.to_dict()}")
    print(f"{'─'*40}")


# ─── Live signal scanner (requires ccxt) ─────────────────────────────────────

def fetch_ohlcv_ccxt(symbol: str = "BTC/USDT", timeframe: str = "1h",
                     limit: int = 500, exchange_id: str = "bybit") -> pd.DataFrame:
    """Fetch recent OHLCV data via ccxt."""
    try:
        import ccxt
    except ImportError:
        raise ImportError("pip install ccxt")

    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp")


def scan_current_signal(symbol: str = "BTC/USDT", timeframe: str = "1h",
                        params: VDMRParams = VDMRParams()) -> dict:
    """Return the current bar's signal state for live monitoring."""
    df  = fetch_ohlcv_ccxt(symbol, timeframe)
    sig = generate_signals(df, params)
    last = sig.iloc[-1]
    return {
        "symbol":       symbol,
        "timeframe":    timeframe,
        "bar_time":     str(last.name),
        "close":        round(last["close"], 4),
        "zscore":       round(last["zscore"], 3),
        "z_vel":        round(last["z_vel"],  4),
        "eff_ratio":    round(last["eff_ratio"], 3),
        "roc_50":       round(last["roc_50"],   2),
        "long_signal":  bool(last["long_signal"]),
        "short_signal": bool(last["short_signal"]),
        "long_stop":    round(last["long_stop"],  2),
        "short_stop":   round(last["short_stop"], 2),
    }


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("VDMR Strategy — live signal check")
    print("Fetching BTCUSDT 1h from Bybit…")
    try:
        result = scan_current_signal("BTC/USDT", "1h")
        for k, v in result.items():
            print(f"  {k:<16}: {v}")
        if result["long_signal"]:
            print("\n  *** LONG SIGNAL ACTIVE ***")
        elif result["short_signal"]:
            print("\n  *** SHORT SIGNAL ACTIVE ***")
        else:
            print("\n  No active signal on current bar.")
    except Exception as e:
        print(f"  (Live fetch unavailable: {e})")
        print("  Supply a DataFrame to generate_signals() or backtest() directly.")


# ─── BaseStrategy wrapper ─────────────────────────────────────────────────────

# Bot integration imports (available when running inside the bot)
try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object  # fallback for standalone use
    TradeSignal  = None


# ETH: breakeven in backtest. DOGE: negative on v4. Both excluded.
VDMR_BLACKLIST = {
    "ETH/USD", "ETHUSDT", "ETH/USDT", "ETHBUSD", "ETH-USD",
    "DOGE/USD", "DOGEUSDT", "DOGE/USDT", "DOGE-USD",
}


class VDMRStrategy(BaseStrategy):
    """Velocity-Deceleration Mean Reversion — Z-score exhaustion + chop filter (INCUBATE)."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "vdmr_strategy"
        self.params                  = VDMRParams()
        self.stop_loss_pct           = 1.0      # fallback default
        self.take_profit_pct         = 2.0
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.candle_limit            = 300
        self.reviewer_exempt         = True
        self.time_stop_profile       = "strategy_defined"

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        if symbol.upper() in VDMR_BLACKLIST:
            self.verbose_log_skip(symbol, "In VDMR blacklist (ETH: breakeven, DOGE: negative v4)")
            return None
        if not self._check_enough_candles(symbol, candles, 220):
            return None
        try:
            sig  = generate_signals(candles, self.params)
            last = sig.iloc[-1]

            long_sig  = bool(last["long_signal"])
            short_sig = bool(last["short_signal"])

            if not (long_sig or short_sig):
                return None

            direction = "long" if long_sig else "short"
            close     = float(last["close"])
            atr       = float(last["atr"])
            zscore    = float(last["zscore"])
            z_vel     = float(last["z_vel"])
            eff_ratio = float(last["eff_ratio"])

            sl_pct = round(atr * self.params.sl_atr_mult / close * 100, 3)
            # TP is mean reversion to 0 — approximate as zscore-distance in price
            roll_std   = candles["close"].rolling(self.params.zscore_lookback).std(ddof=0).iloc[-1]
            rev_dist   = abs(zscore) * float(roll_std)
            tp_pct     = round(min(rev_dist / close * 100, 8.0), 3)  # cap at 8%

            # Score: deeper Z, more deceleration = stronger mean reversion case
            z_depth = min(abs(zscore) / (self.params.z_entry_thresh * 2), 1.0)
            er_score = max(0.0, 1.0 - eff_ratio / self.params.er_max)
            score    = round(max(0.55, min(0.90, 0.5 + z_depth * 0.3 + er_score * 0.15)), 3)

            self.verbose_log(symbol, "|zscore| > thresh", True,
                             abs(zscore), self.params.z_entry_thresh, direction)
            self.verbose_log(symbol, "z_vel deceleration",
                             (z_vel > 0 if long_sig else z_vel < 0),
                             z_vel, 0.0, direction)
            self.verbose_log_score(symbol, score, 0.55)

            vol_series = candles["volume"]
            vol_ma     = vol_series.rolling(50).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma), 3) if vol_ma > 0 else 1.0

            sl_price = (close - atr * self.params.sl_atr_mult if direction == "long"
                        else close + atr * self.params.sl_atr_mult)

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (f"VDMR: Z={zscore:.2f} vel={z_vel:.3f} "
                                   f"ER={eff_ratio:.2f} | "
                                   f"ROC={last['roc_50']:.1f}% "
                                   f"| {'bull' if last['bull_regime'] else 'bear'} regime"),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":               "vdmr_strategy",
                    "entry_timeframe":             "1h",
                    "zscore":                      round(zscore, 3),
                    "z_vel":                       round(z_vel, 4),
                    "eff_ratio":                   round(eff_ratio, 3),
                    "roc_50":                      round(float(last["roc_50"]), 2),
                    "bull_regime":                 bool(last["bull_regime"]),
                    "bear_regime":                 bool(last["bear_regime"]),
                    "rev_ok":                      bool(last["rev_ok"]),
                    "atr":                         round(atr, 6),
                    "volume_ratio":                vol_ratio,
                    "structural_stop_price":       round(sl_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"VDMRStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        if bars is None or len(bars) < 2:
            return None
        p    = self.params
        meta = entry_metadata or {}

        # A. Z-score mean reversion exit (primary thesis exit)
        try:
            c         = bars["close"]
            roll_mean = c.rolling(p.zscore_lookback).mean()
            roll_std  = c.rolling(p.zscore_lookback).std(ddof=0).replace(0, np.nan)
            z_now     = float(((c - roll_mean) / roll_std).iloc[-1])
            if not pd.isna(z_now):
                if direction == "long"  and z_now >= p.z_exit_thresh:
                    return "vdmr_mean_return"
                if direction == "short" and z_now <= p.z_exit_thresh:
                    return "vdmr_mean_return"
        except Exception:
            pass

        # B. Time stop (safety net)
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= p.max_bars_in_trade:
            return "vdmr_time_stop"

        return None
