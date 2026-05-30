"""
RCR Mean Reversion
==================
Classification : INCUBATE
Source         : Trader Dev public library — "RCR v1" (LINKUSDT 1h)
                 Forked and analysed 2026-05-24

Core Concept — Return Correlation Reversion
-------------------------------------------
The key insight is to detect WHEN the market is in mean-reverting mode
before entering, not just when price is extended.

The regime detector is the 50-bar autocorrelation of 1-bar returns:
  acRet = correlation(ret[t], ret[t-1], 50)

  acRet > 0  → trending (returns persist in direction)
  acRet < 0  → mean-reverting (returns reverse direction)
  acRet < -0.05 → confirmed mean-reverting regime

This is statistically principled: if recent 1-bar returns are negatively
autocorrelated, the market is actively reverting. We then enter when price
is also over-extended from its SMA by > zThresh standard deviations.

Entry Logic
-----------
  Regime  : 50-bar autocorrelation of returns < ac_thresh (e.g. -0.05)
  Long    : Z-score < -z_thresh  (price below mean by >z_thresh std devs)
  Short   : Z-score >  z_thresh  (price above mean by >z_thresh std devs)

Exit Logic (THREE independent exits — whichever fires first)
-----------
  1. SMA cross : price crosses back through SMA20 (mean reversion achieved)
  2. Hard SL   : entry ± stop_atr_k × ATR(14)
  3. Time stop : position held > time_stop bars with no resolution

Why this beats simple RSI mean reversion
-----------------------------------------
RSI enters whenever RSI is oversold, regardless of market structure.
RCR only enters when the market is structurally mean-reverting (negative
autocorrelation). In trending markets, even "oversold" RSI can go lower.
The autocorrelation filter eliminates those losing cases.

Original backtest (Trader Dev): PF=2.38, WR=76.7%, DD=1.46%, Sharpe=3.42
Tested on LINKUSDT 1h, 180 days (133 trades)

Parameters
----------
  sma_length  = 20    SMA for mean and Z-score calculation
  ac_window   = 50    autocorrelation lookback
  ac_thresh   = -0.05 autocorrelation must be below this (mean-reverting)
  z_thresh    = 1.5   Z-score threshold for entry (standard deviations)
  stop_atr_k  = 1.0   ATR multiple for hard stop
  atr_length  = 14
  time_stop   = 10    max bars to hold without SMA exit
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional


# ── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class RCRMeanReversionParams:
    sma_length:  int   = 20
    ac_window:   int   = 50     # autocorrelation lookback
    ac_thresh:   float = -0.05  # acRet must be BELOW this to qualify
    z_thresh:    float = 1.5    # Z-score entry threshold
    stop_atr_k:  float = 1.0    # hard stop ATR multiple
    atr_length:  int   = 14
    time_stop:   int   = 10     # max bars held before forced exit


# ── Indicators ────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


def _rolling_autocorr(series: pd.Series, window: int, lag: int = 1) -> pd.Series:
    """
    Rolling autocorrelation of a series with given lag.
    Uses pandas rolling correlation between series[t] and series[t-lag].
    Equivalent to Pine's ta.correlation(ret, ret[1], window).
    """
    lagged = series.shift(lag)
    return series.rolling(window).corr(lagged)


# ── Signal Generation ─────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame,
                     params: RCRMeanReversionParams = RCRMeanReversionParams()) -> pd.DataFrame:
    p   = params
    out = df.copy()
    c   = out["close"]

    # 1-bar returns
    out["ret1"]   = c.pct_change()

    # Autocorrelation of returns (regime detector)
    out["ac_ret"] = _rolling_autocorr(out["ret1"], p.ac_window, lag=1)

    # Mean-reverting regime: autocorrelation is negative enough
    out["mean_reverting"] = out["ac_ret"] < p.ac_thresh

    # SMA and Z-score
    out["sma"]    = c.rolling(p.sma_length).mean()
    out["std"]    = c.rolling(p.sma_length).std()
    out["z"]      = (c - out["sma"]) / out["std"].replace(0, np.nan)

    # ATR for stops
    out["atr"]    = _atr(out, p.atr_length)

    # Entry signals (edge-triggered — only on the bar they first fire)
    long_cond   = out["mean_reverting"] & (out["z"] < -p.z_thresh)
    short_cond  = out["mean_reverting"] & (out["z"] >  p.z_thresh)

    out["long_signal"]  = long_cond  & ~long_cond.shift(1, fill_value=False)
    out["short_signal"] = short_cond & ~short_cond.shift(1, fill_value=False)

    return out


# ── Bot Integration ───────────────────────────────────────────────────────────

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


class RCRMeanReversionStrategy(BaseStrategy):
    """
    Return Correlation Reversion. Detects mean-reverting market regimes via
    rolling autocorrelation of returns, then enters on Z-score extremes.
    Three exits: SMA cross (mean achieved), ATR hard stop, or time stop.
    Works long and short. Best on liquid crypto 1h; also viable stocks 1h.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name           = "rcr_mean_reversion"
        self.params                  = RCRMeanReversionParams()
        self.stop_loss_pct           = 2.5   # approx 1×ATR on 1h crypto
        self.take_profit_pct         = 5.0   # approximate; actual exit is SMA cross
        self.crypto_enabled          = True
        self.stock_enabled           = True
        self.crypto_candle_timeframe = "1h"  # validated on LINKUSDT 1h 180d
        self.time_stop_profile       = "strategy_defined"
        self.reviewer_exempt         = True
        self.candle_limit            = 150   # need ac_window(50) + sma_length(20) + buffer

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        min_bars = p.ac_window + p.sma_length + 10
        if not self._check_enough_candles(symbol, candles, min_bars):
            return None
        try:
            sig  = generate_signals(candles, p)
            last = sig.iloc[-1]

            long_sig  = bool(last["long_signal"])
            short_sig = bool(last["short_signal"])
            if not long_sig and not short_sig:
                return None

            close  = float(last["close"])
            atr    = float(last["atr"])
            z      = float(last["z"])
            ac_ret = float(last["ac_ret"])
            sma    = float(last["sma"])

            direction = "long" if long_sig else "short"

            # SL from ATR
            sl_dist = atr * p.stop_atr_k
            sl_pct  = round(max(sl_dist / close * 100, 1.0), 3)

            # TP = distance to SMA (mean reversion target)
            tp_dist = abs(close - sma)
            tp_pct  = round(max(tp_dist / close * 100, 1.0), 3)

            # Score: deeper autocorrelation + stronger Z = more confident
            ac_score = min(1.0, max(0.0, (p.ac_thresh - ac_ret) / 0.15))
            z_score  = min(1.0, (abs(z) - p.z_thresh) / 1.5)
            score    = round(max(0.66, min(0.88,
                           0.66 + ac_score * 0.15 + z_score * 0.10)), 3)

            sl_price = (close - atr * p.stop_atr_k if direction == "long"
                        else close + atr * p.stop_atr_k)

            vol_series = candles["volume"]
            vol_ma_val = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_val), 3) if vol_ma_val > 0 else None

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (f"RCR: Z={z:.2f} acRet={ac_ret:.3f} "
                                   f"SMA={sma:.4f}"),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":               "rcr_mean_reversion",
                    "z_score":                     round(z, 3),
                    "ac_ret":                      round(ac_ret, 4),
                    "sma":                         round(sma, 6),
                    "atr":                         round(atr, 6),
                    "time_stop":                   p.time_stop,
                    "volume_ratio":                vol_ratio,
                    "structural_stop_price":       round(sl_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"RCRMeanReversionStrategy error on {symbol}: {e}",
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
        if pd.isna(row.get("atr")) or pd.isna(row.get("z")):
            return None

        close  = float(row["close"])
        atr    = float(row["atr"])
        z      = float(row["z"])
        ac_ret = float(row["ac_ret"])
        sma    = float(row["sma"])

        direction = "long" if long_sig else "short"
        sl_dist   = atr * p.stop_atr_k
        sl_pct    = round(max(sl_dist / close * 100, 1.0), 3)
        tp_dist   = abs(close - sma)
        tp_pct    = round(max(tp_dist / close * 100, 1.0), 3)
        ac_score  = min(1.0, max(0.0, (p.ac_thresh - ac_ret) / 0.15))
        z_sc      = min(1.0, (abs(z) - p.z_thresh) / 1.5)
        score     = round(max(0.66, min(0.88,
                        0.66 + ac_score * 0.15 + z_sc * 0.10)), 3)

        sl_price   = (close - atr * p.stop_atr_k if direction == "long"
                      else close + atr * p.stop_atr_k)
        vol_series = df["volume"]
        start_v    = max(0, i - 19)
        vol_ma_val = vol_series.iloc[start_v:i].mean() if i > start_v else None
        vol_ratio  = (round(float(vol_series.iloc[i] / vol_ma_val), 3)
                      if vol_ma_val and vol_ma_val > 0 else None)

        return self._make_signal(
            symbol          = symbol,
            direction       = direction,
            score           = score,
            reason          = f"RCR: Z={z:.2f} acRet={ac_ret:.3f}",
            stop_loss_pct   = sl_pct,
            take_profit_pct = tp_pct,
            metadata={
                "strategy_name":               "rcr_mean_reversion",
                "z_score":                     round(z, 3),
                "ac_ret":                      round(ac_ret, 4),
                "sma":                         round(sma, 6),
                "atr":                         round(atr, 6),
                "time_stop":                   p.time_stop,
                "volume_ratio":                vol_ratio,
                "structural_stop_price":       round(sl_price, 6),
                "preferred_initial_stop_mode": "signal_structural",
                "preferred_trail_mode":        "none",
            },
        )

    def _exit_from_precomputed(self, i: int, sigs: pd.DataFrame,
                               direction: str, meta: dict) -> Optional[str]:
        p         = self.params
        bars_held = int(meta.get("_bars_held", 0))

        # A. SMA cross exit (primary: mean reversion achieved)
        try:
            if i >= 1:
                cur_close  = float(sigs.iloc[i]["close"])
                cur_sma    = float(sigs.iloc[i]["sma"])
                prev_close = float(sigs.iloc[i - 1]["close"])
                prev_sma   = float(sigs.iloc[i - 1]["sma"])
                if not any(pd.isna(v) for v in [cur_sma, prev_sma]):
                    if direction == "long" and prev_close <= prev_sma and cur_close > cur_sma:
                        return "sma_cross_exit"
                    if direction == "short" and prev_close >= prev_sma and cur_close < cur_sma:
                        return "sma_cross_exit"
        except Exception:
            pass

        # B. Time stop (safety net)
        if bars_held >= p.time_stop:
            return "time_stop"
        return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        if bars is None or len(bars) < 2:
            return None
        p         = self.params
        meta      = entry_metadata or {}
        bars_held = int(meta.get("_bars_held", 0))

        # A. Time stop (safety net — check first, cheap)
        if bars_held >= p.time_stop:
            return "time_stop"

        # B. SMA cross exit — compute only the SMA (O(sma_length), not O(N) like generate_signals)
        try:
            close_series = bars["close"]
            sma_series   = close_series.rolling(p.sma_length).mean()
            cur_close    = float(close_series.iloc[-1])
            cur_sma      = float(sma_series.iloc[-1])
            prev_close   = float(close_series.iloc[-2])
            prev_sma     = float(sma_series.iloc[-2])
            if not any(pd.isna(v) for v in [cur_sma, prev_sma]):
                if direction == "long" and prev_close <= prev_sma and cur_close > cur_sma:
                    return "sma_cross_exit"
                if direction == "short" and prev_close >= prev_sma and cur_close < cur_sma:
                    return "sma_cross_exit"
        except Exception:
            pass

        return None
