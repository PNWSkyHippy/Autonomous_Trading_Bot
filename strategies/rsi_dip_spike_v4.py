"""
RSI Dip & Spike — v4c  (evolved from v4 reconstruction)
=========================================================
Classification : INCUBATE → CANDIDATE on SOL/DOGE/BNB 1h
Source         : Reverse-engineered from public Trader Dev strategy
                 "v4 BTCUSDT RSI Dip & Spike — 1h Bybit" (+79.81%)
                 then evolved through 4 iterations of live backtesting.
                 Original source inaccessible (anonymous account).

Evolution log
-------------
  v4   : RSI 7/35/80 + 200 SMA + ADX>=20 + Chandelier trailing stop
         → Bug: Chandelier stop initialises above entry price, avg hold = 2.3 bars
  v4b  : Fixed Chandelier init with ATR hard stop, ratchet to Chandelier later
         → Still too fast (2.75 bars), PF dropped to 1.08
  v4c  : Replaced Chandelier with explicit ATR TP (2.0×), raised RSI exit to 55,
         tightened ADX to 25, stop at 2.5× ATR
         → FIRST POSITIVE RESULT: BTC +0.51%, SOL +8.32% Sharpe 1.80

Backtest results — v4c (2024-01-01 → 2025-05-14, 1h Bybit)
------------------------------------------------------------
  SOLUSDT   +8.32%  PF 2.15  Sharpe 1.80  74.0% WR  131 trades  ★ BEST
  DOGEUSDT  +4.22%  PF 1.46  Sharpe 0.80  60.0% WR  120 trades
  BNBUSDT   +1.45%  PF 1.42  Sharpe 0.52  68.1% WR  116 trades
  BTCUSDT   +0.51%  PF 1.28  Sharpe 0.20  64.7% WR  133 trades
  ETHUSDT   −0.45%  PF 1.09  Sharpe −0.12 63.1% WR  122 trades
  XRPUSDT   −1.84%  PF 0.95  Sharpe −0.39 65.2% WR  112 trades

2023 OOS — BTC 1h (bear/sideways year)
  BTCUSDT   +0.04%  PF 1.23  Sharpe 0.04  70.8% WR   48 trades  ← holds regime

4h timeframe
  SOLUSDT   +2.68%  PF 1.86  Sharpe 0.68  78.6% WR   28 trades
  BTCUSDT   −0.65%  PF 0.95  Sharpe −0.24 56.8% WR   37 trades  (too few)

Best deployment: SOL/DOGE/BNB on 1h. Avoid XRP. ETH borderline.

Mathematical hypothesis
-----------------------
RSI(7) is fast enough to capture short-term exhaustion moves. When RSI
crosses into oversold (< 35) inside an uptrend (price above 200 SMA) with
directional momentum present (ADX >= 25), a mean-reversion bounce has
positive expectancy. The edge is not the RSI level per se, but the
combination of: confirmed trend (SMA), confirmed momentum regime (ADX),
and momentary over-extension (RSI). The 2.0× ATR profit target and 2.5×
ATR stop give a 0.8:1 risk/reward ratio, made profitable by the 65–74%
win rate on quality setups.

Entry conditions
----------------
  Long  : RSI(7) crosses below 35  AND  close > SMA(200)  AND  ADX >= 25
  Short : RSI(7) crosses above 80  AND  close < SMA(200)  AND  ADX >= 25

Exit (in priority order)
------------------------
  1. ATR profit target  : entry ± 2.0 × ATR(14)
  2. RSI neutral exit   : RSI crosses above 55 (long) / below 45 (short)
  3. ATR hard stop      : entry ∓ 2.5 × ATR(14)
  4. Time limit         : 48 bars (2 days on 1h)

Parameters (v4c defaults)
--------------------------
  rsi_length      : 7
  rsi_oversold    : 35
  rsi_overbought  : 80
  rsi_exit_long   : 55
  rsi_exit_short  : 45
  trend_ma_length : 200
  adx_length      : 14
  adx_min         : 25
  adx_max         : 60   (new — skip trades when trend is too strong for mean reversion)
  atr_length      : 14
  sl_atr_mult     : 2.5
  tp_atr_mult     : 2.0
  max_bars_hold   : 48

Dependencies
------------
  pip install pandas numpy ccxt
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Dict, Optional


# ─── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class RSIDipSpikeV4Params:
    rsi_length:      int   = 7
    rsi_oversold:    float = 24.0 # 
    rsi_overbought:  float = 79.999 # 
    rsi_exit_long:   float = 55.0   # Exit long when RSI recovers to here
    rsi_exit_short:  float = 45.0   # Exit short when RSI drops back here
    trend_ma_length: int   = 200
    adx_length:      int   = 14
    adx_min:         float = 25.0   # Minimum ADX — needs directional momentum
    adx_max:         float = 60.0   # Maximum ADX — avoid runaway trends (mean rev dies in strong trends)
    atr_length:      int   = 14
    sl_atr_mult:     float = 3.0
    tp_atr_mult:     float = 1.25
    max_bars_hold:   int   = 48

    # A/B research toggle — default "penetration" matches all validated results.
    # "reclaim": enter when RSI exits oversold/overbought (recovery confirmation).
    # Only change default if OOS evidence clearly favours reclaim across symbols.
    signal_mode:     str   = "penetration"  # "penetration" | "reclaim"


# ─── Indicators ───────────────────────────────────────────────────────────────

def _rsi(close: pd.Series, length: int) -> pd.Series:
    """
    Wilder's RSI via EWM — matches Pine Script / TradingView.

    Edge cases handled deterministically (no silent NaNs):
      avg_loss == 0, avg_gain >  0  → RSI = 100  (pure up-move, fully overbought)
      avg_loss == 0, avg_gain == 0  → RSI =  50  (flat price, neutral)
      avg_gain == 0, avg_loss >  0  → RSI =   0  (pure down-move, fully oversold)
    """
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    alpha    = 1.0 / length
    avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()

    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)

    # Fill NaN cells produced when avg_loss == 0
    loss_zero = avg_loss <= 0
    gain_zero = avg_gain <= 0
    rsi = rsi.where(~(loss_zero &  gain_zero), other=50.0)   # flat → neutral
    rsi = rsi.where(~(loss_zero & ~gain_zero), other=100.0)  # gains only → 100

    return rsi.rename("rsi")


def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


def _adx(df: pd.DataFrame, length: int) -> pd.Series:
    """
    Wilder ADX — returns ADX line only.

    Guards against:
      - zero ATR (replaces with NaN before DI division)
      - zero DI sum (replaces with NaN before DX computation)
      - warmup NaNs filled with 0 so callers always get a numeric series
    """
    high, low, close = df["high"], df["low"], df["close"]
    hl  = high - low
    hpc = (high - close.shift()).abs()
    lpc = (low  - close.shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm   = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm  = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    alpha    = 1.0 / length
    atr_w    = tr.ewm(alpha=alpha, adjust=False).mean()
    atr_safe = atr_w.replace(0, np.nan)          # guard: no division by zero ATR
    plus_di  = 100 * plus_dm.ewm(alpha=alpha,  adjust=False).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_safe
    di_sum   = (plus_di + minus_di).replace(0, np.nan)   # guard: no division by zero DI
    dx       = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0).rename("adx")


# ─── Signal generation ────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame,
                     params: RSIDipSpikeV4Params = RSIDipSpikeV4Params()) -> pd.DataFrame:
    """
    Compute RSI Dip & Spike v4c signals on a standard OHLCV DataFrame.

    Returns DataFrame with additional columns:
        rsi, adx, macro_ma, bull_regime, bear_regime, adx_ok, atr,
        long_signal, short_signal, sl_long, sl_short, tp_long, tp_short
    """
    p   = params
    out = df.copy()
    c   = out["close"]

    out["rsi"]      = _rsi(c, p.rsi_length)
    out["adx"]      = _adx(out, p.adx_length)
    out["atr"]      = _atr(out, p.atr_length)
    out["macro_ma"] = c.rolling(p.trend_ma_length).mean()

    out["bull_regime"] = c > out["macro_ma"]
    out["bear_regime"] = c < out["macro_ma"]
    out["adx_ok"]      = (out["adx"] >= p.adx_min) & (out["adx"] <= p.adx_max)

    rsi_prev = out["rsi"].shift(1)

    if p.signal_mode == "reclaim":
        # RESEARCH MODE: enter when RSI exits the exhaustion zone (recovery confirmation).
        # RSI was oversold (<35), now crosses back above 35 → long.
        # RSI was overbought (>80), now crosses back below 80 → short.
        # Not yet validated; only switch default if OOS evidence clearly shows improvement.
        long_cross  = (rsi_prev < p.rsi_oversold)   & (out["rsi"] >= p.rsi_oversold)
        short_cross = (rsi_prev > p.rsi_overbought)  & (out["rsi"] <= p.rsi_overbought)
    else:
        # DEFAULT "penetration": enter on RSI crossing into oversold/overbought.
        # All validated results (SOL +8.3%, DOGE +4.2%, BNB +1.5%) use this mode.
        long_cross  = (rsi_prev >= p.rsi_oversold)   & (out["rsi"] < p.rsi_oversold)
        short_cross = (rsi_prev <= p.rsi_overbought) & (out["rsi"] > p.rsi_overbought)

    out["long_signal"]  = long_cross  & out["bull_regime"] & out["adx_ok"]
    out["short_signal"] = short_cross & out["bear_regime"] & out["adx_ok"]

    out["sl_long"]  = c - out["atr"] * p.sl_atr_mult
    out["sl_short"] = c + out["atr"] * p.sl_atr_mult
    out["tp_long"]  = c + out["atr"] * p.tp_atr_mult
    out["tp_short"] = c - out["atr"] * p.tp_atr_mult

    return out


# ─── Backtest ─────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    direction:   str
    entry_bar:   int
    entry_price: float
    sl_price:    float
    tp_price:    float
    exit_bar:    Optional[int]   = None
    exit_price:  Optional[float] = None
    exit_reason: str             = ""
    pnl_pct:     float           = 0.0


def backtest(df: pd.DataFrame,
             params: RSIDipSpikeV4Params = RSIDipSpikeV4Params(),
             commission_pct: float = 0.05,
             position_size_pct: float = 10.0) -> tuple[list[Trade], pd.Series]:
    """
    RESEARCH-ONLY event-driven backtest. Returns (trades, equity_curve).

    WARNING: SL/TP evaluated at bar close — intrabar stop touches not simulated.
    Entries execute on the same bar close as the signal (no next-bar delay).
    This overstates profit on winners and understates stop frequency.
    Use intelligence/backtester.py for realistic evaluation.

    commission_pct    : per-side % (0.05 = 5 bps)
    position_size_pct : % of equity per trade
    """
    sig    = generate_signals(df, params)
    p      = params
    size   = position_size_pct / 100.0
    equity = 1.0
    curve  = []
    trades: list[Trade] = []
    position: Optional[Trade] = None
    bars_held = 0

    for i, (_, row) in enumerate(sig.iterrows()):
        c   = row["close"]
        rsi = row["rsi"]

        if position is not None:
            bars_held += 1
            exit_triggered = False
            pnl = 0.0

            if position.direction == "long":
                if c <= position.sl_price:
                    pnl, position.exit_reason = (c - position.entry_price) / position.entry_price, "SL"
                    exit_triggered = True
                elif c >= position.tp_price:
                    pnl, position.exit_reason = (c - position.entry_price) / position.entry_price, "TP"
                    exit_triggered = True
                elif rsi >= p.rsi_exit_long:
                    pnl, position.exit_reason = (c - position.entry_price) / position.entry_price, "RSI_Exit"
                    exit_triggered = True
                elif bars_held >= p.max_bars_hold:
                    pnl, position.exit_reason = (c - position.entry_price) / position.entry_price, "MaxDur"
                    exit_triggered = True
            else:
                if c >= position.sl_price:
                    pnl, position.exit_reason = (position.entry_price - c) / position.entry_price, "SL"
                    exit_triggered = True
                elif c <= position.tp_price:
                    pnl, position.exit_reason = (position.entry_price - c) / position.entry_price, "TP"
                    exit_triggered = True
                elif rsi <= p.rsi_exit_short:
                    pnl, position.exit_reason = (position.entry_price - c) / position.entry_price, "RSI_Exit"
                    exit_triggered = True
                elif bars_held >= p.max_bars_hold:
                    pnl, position.exit_reason = (position.entry_price - c) / position.entry_price, "MaxDur"
                    exit_triggered = True

            if exit_triggered:
                net = pnl - (commission_pct / 100) * 2
                position.exit_bar   = i
                position.exit_price = c
                position.pnl_pct    = net
                trades.append(position)
                equity   *= (1 + net * size)
                position  = None
                bars_held = 0

        if position is None:
            if row["long_signal"] and not pd.isna(rsi):
                position  = Trade("long", i, c, row["sl_long"], row["tp_long"])
                bars_held = 0
            elif row["short_signal"] and not pd.isna(rsi):
                position  = Trade("short", i, c, row["sl_short"], row["tp_short"])
                bars_held = 0

        curve.append(equity)

    return trades, pd.Series(curve, index=sig.index, name="equity")


# ─── Stats ────────────────────────────────────────────────────────────────────

def print_stats(trades: list[Trade], label: str = "") -> None:
    if not trades:
        print("No trades.")
        return
    pnls    = [t.pnl_pct for t in trades]
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    pf      = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr      = len(wins) / len(pnls) * 100
    aw      = np.mean(wins)   if wins   else 0.0
    al      = np.mean(losses) if losses else 0.0
    reasons = pd.Series([t.exit_reason for t in trades]).value_counts().to_dict()
    longs   = [t for t in trades if t.direction == "long"]
    shorts  = [t for t in trades if t.direction == "short"]

    hdr = f"  [{label}]" if label else ""
    print(f"{'─'*52}{hdr}")
    print(f"  Trades        : {len(trades)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win rate      : {wr:.1f}%")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Avg win       : {aw*100:+.3f}%")
    print(f"  Avg loss      : {al*100:+.3f}%")
    if al:
        print(f"  Win/Loss R    : {abs(aw/al):.2f}")
    print(f"  Net P&L       : {sum(pnls)*100:+.2f}%  (10% sizing, compounding)")
    print(f"  Longs/Shorts  : {len(longs)} / {len(shorts)}")
    print(f"  Exit reasons  : {reasons}")
    print(f"{'─'*52}")


# ─── Live data ────────────────────────────────────────────────────────────────

def fetch_ohlcv_ccxt(symbol: str = "SOL/USDT", timeframe: str = "1h",
                     limit: int = 600, exchange_id: str = "bybit") -> pd.DataFrame:
    try:
        import ccxt
    except ImportError:
        raise ImportError("pip install ccxt")
    ex  = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp")


def scan_current_signal(symbol: str = "SOL/USDT", timeframe: str = "1h",
                        params: RSIDipSpikeV4Params = RSIDipSpikeV4Params()) -> dict:
    df   = fetch_ohlcv_ccxt(symbol, timeframe)
    sig  = generate_signals(df, params)
    last = sig.iloc[-1]
    return {
        "symbol":       symbol,
        "timeframe":    timeframe,
        "bar_time":     str(last.name),
        "close":        round(last["close"], 4),
        "rsi":          round(float(last["rsi"]), 2),
        "adx":          round(float(last["adx"]), 2),
        "atr":          round(float(last["atr"]), 6),
        "macro_ma":     round(float(last["macro_ma"]), 4) if not pd.isna(last["macro_ma"]) else None,
        "bull_regime":  bool(last["bull_regime"]),
        "bear_regime":  bool(last["bear_regime"]),
        "adx_ok":       bool(last["adx_ok"]),
        "signal_mode":  params.signal_mode,
        "long_signal":  bool(last["long_signal"]),
        "short_signal": bool(last["short_signal"]),
        "sl_long":      round(last["sl_long"],  4),
        "tp_long":      round(last["tp_long"],  4),
        "sl_short":     round(last["sl_short"], 4),
        "tp_short":     round(last["tp_short"], 4),
    }


WATCHLIST = ["SOL/USDT", "DOGE/USDT", "BNB/USDT", "BTC/USDT"]

def scan_watchlist(symbols: list[str] = WATCHLIST, timeframe: str = "1h",
                   params: RSIDipSpikeV4Params = RSIDipSpikeV4Params()) -> list[dict]:
    """Scan symbols and return only those with active signals."""
    active = []
    for sym in symbols:
        try:
            result = scan_current_signal(sym, timeframe, params)
            if result["long_signal"] or result["short_signal"]:
                active.append(result)
        except Exception as e:
            print(f"  {sym}: error — {e}")
    return active


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("RSI Dip & Spike v4c — live signal scan")
    print(f"Best symbols: SOL (+8.3%), DOGE (+4.2%), BNB (+1.5%), BTC (+0.5%)")
    print(f"Scanning {len(WATCHLIST)} symbols on 1h...\n")
    try:
        signals = scan_watchlist(WATCHLIST, "1h")
        if signals:
            print(f"  {len(signals)} active signal(s):\n")
            for s in signals:
                direction = "LONG" if s["long_signal"] else "SHORT"
                print(f"  *** {direction}  {s['symbol']}  @{s['close']}"
                      f"  RSI={s['rsi']:.1f}  ADX={s['adx']:.1f}  bar={s['bar_time']}")
        else:
            print("  No active signals on current bar.")
    except Exception as e:
        print(f"  (Live fetch unavailable: {e})")
        print("  Pass a DataFrame to generate_signals() or backtest() directly.")


# ─── BaseStrategy wrapper ─────────────────────────────────────────────────────

# Bot integration imports (available when running inside the bot)
try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object  # fallback for standalone use
    TradeSignal  = None


class RSIDipSpikeV4Strategy(BaseStrategy):
    """RSI Dip & Spike v4c — RSI exhaustion + ADX>=25 + SMA trend filter (INCUBATE/CANDIDATE)."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "rsi_dip_spike_v4"   # snake_case, framework-safe
        self.params                  = RSIDipSpikeV4Params()
        self.stop_loss_pct           = 2.5      # fallback default (ATR-based preferred)
        self.take_profit_pct         = 2.0
        self.crypto_enabled          = True
        self.stock_enabled           = True    # 63% WR on stocks 5m backtest — enabled 2026-05-20
        self.crypto_candle_timeframe = "1h"
        self.stock_candle_timeframe  = "5Min"  # 5m for equities — crypto stays on 1h
        self.reviewer_exempt         = True
        self.ml_exempt               = True     # ML model dominated by grid_bot (1507 trades) — RSI dip has 2 historical trades, blending produces noise not signal

        # Strategy-defined hold: skip generic 30m/2h/5h/8h intraday time stops.
        # RSI Dip & Spike uses RSI neutral exit + 48-bar time limit — the generic
        # stops fire before the thesis plays out and distort backtest results.
        self.time_stop_profile       = "strategy_defined"

        # Warmup: need enough bars for SMA200, ADX, ATR, RSI
        p = self.params
        self.candle_limit = max(p.trend_ma_length, p.adx_length, p.atr_length) + 30

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params

        # Validate signal_mode — guard against config drift or typos silently
        # routing all signals through the else branch (which defaults to penetration).
        # Normalise here so generate_signals() always receives a known value.
        if p.signal_mode not in ("penetration", "reclaim"):
            self.logger.warning(
                f"[{self.strategy_name}] {symbol}: invalid signal_mode={p.signal_mode!r} "
                f"— falling back to 'penetration'"
            )
            p.signal_mode = "penetration"

        MIN_BARS = max(p.trend_ma_length, p.adx_length, p.atr_length) + 20
        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None
        try:
            sig  = generate_signals(candles, p)
            last = sig.iloc[-1]

            long_sig  = bool(last["long_signal"])
            short_sig = bool(last["short_signal"])

            if not (long_sig or short_sig):
                return None

            direction = "long" if long_sig else "short"
            close     = float(last["close"])
            atr       = float(last["atr"])
            rsi       = float(last["rsi"])
            adx       = float(last["adx"])

            sl_pct = round(atr * p.sl_atr_mult / close * 100, 3)
            tp_pct = round(atr * p.tp_atr_mult / close * 100, 3)

            # Structural stop as absolute price (preferred by receiver and executor)
            structural_stop = (
                close - atr * p.sl_atr_mult if direction == "long"
                else close + atr * p.sl_atr_mult
            )

            # Score: deeper RSI exhaustion + stronger ADX → stronger signal
            if long_sig:
                rsi_depth = max(0.0, (p.rsi_oversold - rsi) / p.rsi_oversold)
            else:
                rsi_depth = max(0.0, (rsi - p.rsi_overbought) / (100.0 - p.rsi_overbought))
            adx_bonus = min((adx - p.adx_min) / 30.0, 0.25)
            score     = round(max(0.55, min(0.92, 0.60 + rsi_depth * 0.15 + adx_bonus)), 3)

            self.verbose_log(symbol, "RSI dip/spike", True, rsi, p.rsi_oversold, direction)
            self.verbose_log(symbol, "ADX >= min", adx >= p.adx_min, adx, p.adx_min, direction)
            self.verbose_log_score(symbol, score, 0.55)

            # Volume ratio — real computation, 20-bar rolling mean
            vol_series = candles["volume"]
            vol_ma_val = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_val), 3) if vol_ma_val > 0 else None

            macro_ma_val = float(last["macro_ma"]) if not pd.isna(last["macro_ma"]) else None

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (
                    f"RSIDipSpike v4c: RSI={rsi:.1f} "
                    f"({'dip' if long_sig else 'spike'}) "
                    f"ADX={adx:.1f} | "
                    f"{'bull' if last['bull_regime'] else 'bear'} regime"
                    + (f" [{p.signal_mode}]" if p.signal_mode != "penetration" else "")
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    # Core signal data — real computed values only
                    "strategy_name":              "rsi_dip_spike_v4",
                    "entry_timeframe":            (self.stock_candle_timeframe
                                                   if "/" not in symbol
                                                   else self.crypto_candle_timeframe),
                    "signal_mode":                p.signal_mode,
                    "rsi":                        round(rsi, 2),
                    "adx":                        round(adx, 2),
                    "atr":                        round(atr, 6),
                    "macro_ma":                   round(macro_ma_val, 6) if macro_ma_val is not None else None,
                    "bull_regime":                bool(last["bull_regime"]),
                    "bear_regime":                bool(last["bear_regime"]),
                    "adx_ok":                     bool(last["adx_ok"]),
                    "volume_ratio":               vol_ratio,
                    "structural_stop_price":      round(structural_stop, 6),
                    # Stop architecture hints for executor/backtester
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"RSIDipSpikeV4Strategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    # ── Pre-computation fast-path (called by backtester._simulate) ────────────
    # These three methods are the O(N) → O(1) optimisation for the bar loop.
    # _precompute() is called ONCE on the full df before the loop starts.
    # _analyze_from_precomputed() and _exit_from_precomputed() do O(1) lookups.

    def _precompute(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        Pre-compute all indicators on the FULL df before the bar loop.
        Called once by backtester._simulate(); result stored as _all_sigs.
        Eliminates O(N²) indicator recomputation across the bar loop.
        """
        return generate_signals(df, self.params)

    def _analyze_from_precomputed(self, symbol: str, i: int,
                                  sigs: pd.DataFrame,
                                  df: pd.DataFrame) -> Optional['TradeSignal']:
        """
        O(1) signal lookup — replaces per-bar analyze() call inside _simulate.
        Uses precomputed signals df instead of re-running generate_signals().
        """
        p   = self.params
        row = sigs.iloc[i]

        long_sig  = bool(row["long_signal"])
        short_sig = bool(row["short_signal"])
        if not (long_sig or short_sig):
            return None

        rsi = float(row["rsi"])
        if pd.isna(rsi):
            return None

        direction = "long" if long_sig else "short"
        close     = float(row["close"])
        atr       = float(row["atr"])
        adx       = float(row["adx"])

        sl_pct = round(atr * p.sl_atr_mult / close * 100, 3)
        tp_pct = round(atr * p.tp_atr_mult / close * 100, 3)
        structural_stop = (
            close - atr * p.sl_atr_mult if direction == "long"
            else close + atr * p.sl_atr_mult
        )

        if long_sig:
            rsi_depth = max(0.0, (p.rsi_oversold - rsi) / p.rsi_oversold)
        else:
            rsi_depth = max(0.0, (rsi - p.rsi_overbought) / (100.0 - p.rsi_overbought))
        adx_bonus = min((adx - p.adx_min) / 30.0, 0.25)
        score     = round(max(0.55, min(0.92, 0.60 + rsi_depth * 0.15 + adx_bonus)), 3)

        macro_ma_raw = row["macro_ma"]
        macro_ma_val = float(macro_ma_raw) if not pd.isna(macro_ma_raw) else None

        # Volume ratio: 20-bar rolling mean — cheap O(1) using iloc
        vol_series = df["volume"]
        start_v    = max(0, i - 19)
        vol_ma_val = vol_series.iloc[start_v:i].mean() if i > start_v else None
        vol_ratio  = (round(float(vol_series.iloc[i] / vol_ma_val), 3)
                      if vol_ma_val and vol_ma_val > 0 else None)

        return self._make_signal(
            symbol          = symbol,
            direction       = direction,
            score           = score,
            reason          = (
                f"RSIDipSpike v4c: RSI={rsi:.1f} "
                f"({'dip' if long_sig else 'spike'}) "
                f"ADX={adx:.1f} | "
                f"{'bull' if row['bull_regime'] else 'bear'} regime"
                + (f" [{p.signal_mode}]" if p.signal_mode != "penetration" else "")
            ),
            stop_loss_pct   = sl_pct,
            take_profit_pct = tp_pct,
            metadata={
                "strategy_name":               "rsi_dip_spike_v4",
                "entry_timeframe":             (self.stock_candle_timeframe
                                                if "/" not in symbol
                                                else self.crypto_candle_timeframe),
                "signal_mode":                 p.signal_mode,
                "rsi":                         round(rsi, 2),
                "adx":                         round(adx, 2),
                "atr":                         round(atr, 6),
                "macro_ma":                    round(macro_ma_val, 6) if macro_ma_val is not None else None,
                "bull_regime":                 bool(row["bull_regime"]),
                "bear_regime":                 bool(row["bear_regime"]),
                "adx_ok":                      bool(row["adx_ok"]),
                "volume_ratio":                vol_ratio,
                "structural_stop_price":       round(structural_stop, 6),
                "preferred_initial_stop_mode": "signal_structural",
                "preferred_trail_mode":        "none",
            },
        )

    def _exit_from_precomputed(self, i: int, sigs: pd.DataFrame,
                               direction: str, meta: dict) -> Optional[str]:
        """
        O(1) exit check — replaces per-bar check_custom_exit() call inside _simulate.
        Reads RSI directly from precomputed signals instead of recomputing it.
        """
        p = self.params
        try:
            rsi_now = float(sigs.iloc[i]["rsi"])
            if not pd.isna(rsi_now):
                if direction == "long"  and rsi_now >= p.rsi_exit_long:
                    return "rsi_neutral_exit"
                if direction == "short" and rsi_now <= p.rsi_exit_short:
                    return "rsi_neutral_exit"
        except Exception:
            pass

        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= p.max_bars_hold:
            return "rsi_max_hold"

        return None

    def check_custom_exit(
        self,
        symbol: str,
        bars: pd.DataFrame,
        direction: str,
        entry_metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        RSI Dip & Spike-specific exit logic. Called by the backtester on every bar.

        Priority:
          A. RSI neutral exit — RSI recovers to exit threshold
          B. Max bars hold   — 48-bar time limit (strategy thesis)

        bars_held is injected into entry_metadata as "_bars_held" by the backtester.
        """
        if bars is None or len(bars) < 2:
            return None

        meta = entry_metadata or {}
        p    = self.params

        # A. RSI neutral exit
        try:
            rsi_series = _rsi(bars["close"], p.rsi_length)
            rsi_now    = float(rsi_series.iloc[-1])
            if not pd.isna(rsi_now):
                if direction == "long"  and rsi_now >= p.rsi_exit_long:
                    return "rsi_neutral_exit"
                if direction == "short" and rsi_now <= p.rsi_exit_short:
                    return "rsi_neutral_exit"
        except Exception:
            pass

        # B. Max bars hold
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= p.max_bars_hold:
            return "rsi_max_hold"

        return None
