"""
EMA Ribbon Breakout
===================
Classification : INCUBATE
Author         : Quant Engineering Session 2026-05-24

Core Concept
------------
An EMA Ribbon uses a stack of 5 EMAs (8/13/21/34/55 — Fibonacci-spaced).
When all 5 EMAs are perfectly ordered AND price closes above/below the
entire stack, a confirmed trend breakout has occurred. The ribbon alignment
is a multi-timeframe proxy: each EMA represents progressively longer trend.

The edge is in the CONFIRMATION: requiring all 5 aligned eliminates false
breakouts from choppy markets. ADX confirms the trend has momentum.

Entry Logic
-----------
  Long  : close > ema8 > ema13 > ema21 > ema34 > ema55
           AND ADX >= adx_min
           AND close crosses above ema8 on current bar (fresh break)

  Short : close < ema8 < ema13 < ema21 < ema34 < ema55
           AND ADX >= adx_min
           AND close crosses below ema8 on current bar

Exit Logic
----------
  Chandelier-style: rolling_high/low since entry minus/plus ATR × atr_mult
  Falls back to hard SL at sl_pct if chandelier not established.

Why Fibonacci EMA Spacing
-------------------------
8, 13, 21, 34, 55 are consecutive Fibonacci numbers. They create a natural
"fan" of momentum: the faster EMAs respond to short-term momentum, the
slower EMAs to long-term trend. When all 5 align perfectly, the market
structure is self-consistent across all those horizons simultaneously.

Parameters
----------
  ema1 = 8    fastest EMA
  ema2 = 13
  ema3 = 21
  ema4 = 34
  ema5 = 55   slowest EMA
  adx_min   = 22    minimum ADX for entry
  adx_length= 14
  atr_length= 14
  atr_mult  = 3.0   chandelier ATR multiple
  sl_pct    = 6.0   hard fallback stop %
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional


# ── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class EMARibbonBreakoutParams:
    ema1:       int   = 8
    ema2:       int   = 13
    ema3:       int   = 21
    ema4:       int   = 34
    ema5:       int   = 55
    adx_min:    float = 22.0   # trend must have momentum
    adx_length: int   = 14
    atr_length: int   = 14
    atr_mult:   float = 3.0    # chandelier trail
    sl_pct:     float = 6.0    # hard fallback SL %


# ── Indicators ────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


def _adx(df: pd.DataFrame, length: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    hl   = high - low
    hpc  = (high - close.shift()).abs()
    lpc  = (low  - close.shift()).abs()
    tr   = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    up   = high.diff()
    down = -low.diff()
    plus_dm  = pd.Series(np.where((up > down) & (up > 0),   up,   0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    alpha    = 1.0 / length
    atr_w    = tr.ewm(alpha=alpha, adjust=False).mean()
    safe_atr = atr_w.replace(0, np.nan)
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / safe_atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / safe_atr
    di_sum   = (plus_di + minus_di).replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0).rename("adx")


# ── Signal Generation ─────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame,
                     params: EMARibbonBreakoutParams = EMARibbonBreakoutParams()) -> pd.DataFrame:
    p   = params
    out = df.copy()
    c   = out["close"]

    out["e1"] = _ema(c, p.ema1)
    out["e2"] = _ema(c, p.ema2)
    out["e3"] = _ema(c, p.ema3)
    out["e4"] = _ema(c, p.ema4)
    out["e5"] = _ema(c, p.ema5)
    out["atr"] = _atr(out, p.atr_length)
    out["adx"] = _adx(out, p.adx_length)

    # Full ribbon alignment check
    ribbon_bull = (
        (out["e1"] > out["e2"]) &
        (out["e2"] > out["e3"]) &
        (out["e3"] > out["e4"]) &
        (out["e4"] > out["e5"])
    )
    ribbon_bear = (
        (out["e1"] < out["e2"]) &
        (out["e2"] < out["e3"]) &
        (out["e3"] < out["e4"]) &
        (out["e4"] < out["e5"])
    )

    # Price must be above/below entire ribbon
    above_ribbon = c > out["e1"]
    below_ribbon = c < out["e1"]

    # Fresh cross: wasn't above/below ribbon on prior bar
    fresh_bull = above_ribbon & ~above_ribbon.shift(1, fill_value=False)
    fresh_bear = below_ribbon & ~below_ribbon.shift(1, fill_value=False)

    trending = out["adx"] >= p.adx_min

    out["long_signal"]  = ribbon_bull & fresh_bull & trending
    out["short_signal"] = ribbon_bear & fresh_bear & trending
    out["ribbon_bull"]  = ribbon_bull
    out["ribbon_bear"]  = ribbon_bear

    return out


# ── Bot Integration ───────────────────────────────────────────────────────────

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


class EMARibbonBreakoutStrategy(BaseStrategy):
    """
    5-EMA Fibonacci ribbon (8/13/21/34/55). Enters when all EMAs align
    AND price makes a fresh break above/below the full stack AND ADX >= 22.
    Exits via chandelier trailing stop (rolling high/low - ATR×3).
    Works long and short. Good for trending crypto and tech stocks.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name           = "ema_ribbon_breakout"
        self.params                  = EMARibbonBreakoutParams()
        self.stop_loss_pct           = self.params.sl_pct
        self.take_profit_pct         = 25.0   # wide — chandelier exits first
        self.crypto_enabled          = True
        self.stock_enabled           = True
        self.crypto_candle_timeframe = "1d"
        self.time_stop_profile       = "strategy_defined"
        self.reviewer_exempt         = True
        self.candle_limit            = 200

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        if not self._check_enough_candles(symbol, candles, 100):
            return None
        try:
            sig  = generate_signals(candles, p)
            last = sig.iloc[-1]

            long_sig  = bool(last["long_signal"])
            short_sig = bool(last["short_signal"])
            if not long_sig and not short_sig:
                return None

            close = float(last["close"])
            atr   = float(last["atr"])
            adx   = float(last["adx"])
            e5    = float(last["e5"])

            direction = "long" if long_sig else "short"

            # SL from initial chandelier: entry ± ATR × atr_mult
            chan_dist = atr * p.atr_mult
            sl_pct    = round(max(chan_dist / close * 100, 2.0), 3)
            sl_price  = close - chan_dist if direction == "long" else close + chan_dist

            # Score: stronger ADX = more confident trend
            adx_norm  = min(1.0, max(0.0, (adx - p.adx_min) / 30.0))
            # Width of ribbon as a % of price = alignment quality
            ribbon_width = abs(float(last["e1"]) - e5) / close
            ribbon_score = min(1.0, ribbon_width / 0.03)
            score = round(max(0.66, min(0.88,
                        0.66 + adx_norm * 0.15 + ribbon_score * 0.10)), 3)

            vol_series = candles["volume"]
            vol_ma_val = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_val), 3) if vol_ma_val > 0 else None

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (f"EMArib: ADX={adx:.1f} "
                                   f"e1={float(last['e1']):.4f} e5={e5:.4f}"),
                stop_loss_pct   = sl_pct,
                take_profit_pct = self.take_profit_pct,
                metadata={
                    "strategy_name":               "ema_ribbon_breakout",
                    "adx":                         round(adx, 2),
                    "atr":                         round(atr, 6),
                    "entry_atr":                   round(atr, 6),
                    "e1":                          round(float(last["e1"]), 6),
                    "e5":                          round(e5, 6),
                    "volume_ratio":                vol_ratio,
                    "entry_timeframe":             "1d",
                    "structural_stop_price":       round(sl_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"EMARibbonBreakoutStrategy error on {symbol}: {e}",
                              exc_info=True)
            return None

    def _precompute(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        return generate_signals(df, self.params)

    def _analyze_from_precomputed(self, symbol: str, i: int,
                                  sigs: pd.DataFrame, df: pd.DataFrame) -> Optional[TradeSignal]:
        p   = self.params
        row = sigs.iloc[i]
        long_sig  = bool(row["long_signal"])
        short_sig = bool(row["short_signal"])
        if not long_sig and not short_sig:
            return None
        if pd.isna(row["atr"]):
            return None

        close = float(row["close"])
        atr   = float(row["atr"])
        adx   = float(row["adx"])
        e5    = float(row["e5"])

        direction    = "long" if long_sig else "short"
        chan_dist    = atr * p.atr_mult
        sl_pct       = round(max(chan_dist / close * 100, 2.0), 3)
        sl_price     = close - chan_dist if direction == "long" else close + chan_dist
        adx_norm     = min(1.0, max(0.0, (adx - p.adx_min) / 30.0))
        ribbon_width = abs(float(row["e1"]) - e5) / close
        ribbon_score = min(1.0, ribbon_width / 0.03)
        score        = round(max(0.66, min(0.88,
                           0.66 + adx_norm * 0.15 + ribbon_score * 0.10)), 3)

        vol_series = df["volume"]
        start_v    = max(0, i - 19)
        vol_ma_val = vol_series.iloc[start_v:i].mean() if i > start_v else None
        vol_ratio  = (round(float(vol_series.iloc[i] / vol_ma_val), 3)
                      if vol_ma_val and vol_ma_val > 0 else None)

        return self._make_signal(
            symbol          = symbol,
            direction       = direction,
            score           = score,
            reason          = f"EMArib: ADX={adx:.1f} e5={e5:.4f}",
            stop_loss_pct   = sl_pct,
            take_profit_pct = self.take_profit_pct,
            metadata={
                "strategy_name":               "ema_ribbon_breakout",
                "adx":                         round(adx, 2),
                "atr":                         round(atr, 6),
                "entry_atr":                   round(atr, 6),
                "e1":                          round(float(row["e1"]), 6),
                "e5":                          round(e5, 6),
                "volume_ratio":                vol_ratio,
                "entry_timeframe":             "1d",
                "structural_stop_price":       round(sl_price, 6),
                "preferred_initial_stop_mode": "signal_structural",
                "preferred_trail_mode":        "none",
            },
        )

    def _exit_from_precomputed(self, i: int, sigs: pd.DataFrame,
                               direction: str, meta: dict) -> Optional[str]:
        p         = self.params
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held < 1:
            return None

        entry_i    = max(0, i - bars_held)
        cur_atr    = float(sigs["atr"].iloc[i])
        cur_close  = float(sigs["close"].iloc[i])

        if direction == "long":
            track_high = float(sigs["high"].iloc[entry_i:i + 1].max())
            chandelier = track_high - cur_atr * p.atr_mult
            if cur_close <= chandelier:
                return "chandelier_stop"
        elif direction == "short":
            track_low  = float(sigs["low"].iloc[entry_i:i + 1].min())
            chandelier = track_low + cur_atr * p.atr_mult
            if cur_close >= chandelier:
                return "chandelier_stop"
        return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        if bars is None or len(bars) < 2:
            return None
        p         = self.params
        meta      = entry_metadata or {}
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held < 1:
            return None

        since     = bars.iloc[-bars_held:] if bars_held <= len(bars) else bars
        cur_atr   = float(meta.get("entry_atr", float(bars["close"].iloc[-1]) * 0.02))
        cur_close = float(bars["close"].iloc[-1])

        if direction == "long":
            track_high = float(since["high"].max())
            chandelier = track_high - cur_atr * p.atr_mult
            if cur_close <= chandelier:
                return "chandelier_stop"
        elif direction == "short":
            track_low  = float(since["low"].min())
            chandelier = track_low + cur_atr * p.atr_mult
            if cur_close >= chandelier:
                return "chandelier_stop"
        return None
