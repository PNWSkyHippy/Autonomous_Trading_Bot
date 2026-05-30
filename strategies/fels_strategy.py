"""
FELS — Failed Extension Liquidity Sweep
=========================================
Classification : Reject v1 (Trader Dev result: PF 1.11, exits broken)
Timeframe      : 1h

Mathematical hypothesis
-----------------------
When price breaks a recent N-bar high/low (triggering stop hunts and
momentum chasers) but FAILS to close beyond that level by more than
one ATR unit, the breakout has failed. Trapped momentum traders are
forced to unwind, creating a rapid snapback in the opposite direction.

The mathematical edge:
  - Breakout level = highest high (or lowest low) of prior N bars
  - "Failed" = price pierces the level intrabar but CLOSES within 1 ATR of it
  - The close being back inside confirms the rejection
  - ATR-normalization filters out weak breaches on low-volatility days

Entry: opposite direction of the failed breakout (fading the liquidity sweep)
Exit:  price reaches midpoint of the sweep range, or max_bars_hold
Stop:  beyond the wick extreme (the sweep high/low)

Regime filter: ADX < adx_max (failed breakouts in strong trends often become
continuation — only fade in ranging/weak-trend environments)

NOTE: Parameters are UNTUNED first-pass defaults. Hypothesis validation only.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


@dataclass
class FELSParams:
    # Breakout detection
    lookback:          int   = 20   # N-bar high/low for breakout level
    atr_close_thresh:  float = 0.8  # Close must be within this many ATR of level
                                    # (lower = stricter failure requirement)

    # ATR
    atr_length:        int   = 14

    # Stop: beyond the wick
    sl_atr_buffer:     float = 0.3  # Extra buffer beyond wick: stop = wick ± buffer*ATR

    # Target: partial reversion
    tp_revert_pct:     float = 0.5  # Fraction of sweep range to target (0.5 = halfway back)

    # Regime
    adx_period:        int   = 14
    adx_max:           float = 28.0 # Fading breakouts only in ranging/weak-trend

    # Volume confirmation — higher vol on breakout bar = stronger trap
    vol_spike_min:     float = 1.2  # Volume on breakout bar must be >= this × rolling avg

    # Duration
    max_bars_hold:     int   = 16


def _compute_adx(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up   = high.diff()
    down = -low.diff()
    pdm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    ndm  = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr   = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr  = tr.ewm(span=period, adjust=False).mean()
    pdi  = 100 * pdm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    ndi  = 100 * ndm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    dx   = (100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan))
    return dx.ewm(span=period, adjust=False).mean()


class FELSStrategy(BaseStrategy):
    """Failed Extension Liquidity Sweep — fade failed breakouts at N-bar extremes."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "fels_strategy"
        self.params                  = FELSParams()
        self.stop_loss_pct           = 1.5
        self.take_profit_pct         = 1.5
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.reviewer_exempt         = True
        self.ml_exempt               = True
        self.time_stop_profile       = "strategy_defined"
        self.enabled                 = False  # WATCHLIST — needs backtest validation before live

        p = self.params
        self.candle_limit = p.lookback + p.atr_length + 50

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        MIN_BARS = p.lookback + p.atr_length + 20
        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None

        try:
            df = candles.copy()
            c  = df["close"]

            # ── ATR ───────────────────────────────────────────────────────────
            hl   = df["high"] - df["low"]
            hpc  = (df["high"] - c.shift()).abs()
            lpc  = (df["low"]  - c.shift()).abs()
            tr   = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
            atr  = tr.ewm(span=p.atr_length, adjust=False).mean()

            # ── ADX ───────────────────────────────────────────────────────────
            adx = _compute_adx(df, p.adx_period)

            # ── Volume spike ──────────────────────────────────────────────────
            vol_ma = df["volume"].rolling(p.lookback).mean()

            # ── N-bar high/low (prior bars ONLY — no lookahead) ───────────────
            # iloc[-2] is the last CLOSED bar; compare against prior N-1 bars
            # so the signal fires on the close of the breakout bar
            prior_high = df["high"].shift(1).rolling(p.lookback).max()
            prior_low  = df["low"].shift(1).rolling(p.lookback).min()

            # ── Failed high breakout → SHORT signal ───────────────────────────
            # Condition: high exceeds N-bar high BUT close is within atr_thresh of it
            swept_high = df["high"] > prior_high
            close_near_level_high = (df["high"] - c) <= atr * p.atr_close_thresh
            vol_spike  = df["volume"] >= vol_ma * p.vol_spike_min
            short_signal = swept_high & close_near_level_high & vol_spike

            # ── Failed low breakout → LONG signal ─────────────────────────────
            swept_low  = df["low"] < prior_low
            close_near_level_low = (c - df["low"]) <= atr * p.atr_close_thresh
            long_signal  = swept_low & close_near_level_low & vol_spike

            last = df.index[-1]
            adx_  = float(adx.loc[last])
            atr_  = float(atr.loc[last])
            cls   = float(c.loc[last])
            high_ = float(df["high"].loc[last])
            low_  = float(df["low"].loc[last])
            ph_   = float(prior_high.loc[last]) if not pd.isna(prior_high.loc[last]) else cls
            pl_   = float(prior_low.loc[last])  if not pd.isna(prior_low.loc[last])  else cls

            is_short = bool(short_signal.loc[last])
            is_long  = bool(long_signal.loc[last])

            self.verbose_log(symbol, "adx_below_max",      adx_ <= p.adx_max, adx_, p.adx_max)
            self.verbose_log(symbol, "failed_high_break",  is_short, high_, f">{ph_:.4f} close near")
            self.verbose_log(symbol, "failed_low_break",   is_long,  low_,  f"<{pl_:.4f} close near")

            if adx_ > p.adx_max:
                return None
            if not (is_short or is_long):
                return None

            direction = "short" if is_short else "long"

            # Stop: beyond the wick with small buffer
            if direction == "short":
                stop_price = high_ + atr_ * p.sl_atr_buffer
                sl_pct     = round((stop_price - cls) / cls * 100, 3)
                # TP: midpoint back into range
                sweep_range = high_ - prior_high.loc[last] if not pd.isna(prior_high.loc[last]) else atr_
                tp_price    = cls - sweep_range * p.tp_revert_pct - atr_ * 0.5
                tp_pct      = round((cls - tp_price) / cls * 100, 3)
            else:
                stop_price = low_ - atr_ * p.sl_atr_buffer
                sl_pct     = round((cls - stop_price) / cls * 100, 3)
                sweep_range = prior_low.loc[last] - low_ if not pd.isna(prior_low.loc[last]) else atr_
                tp_price    = cls + sweep_range * p.tp_revert_pct + atr_ * 0.5
                tp_pct      = round((tp_price - cls) / cls * 100, 3)

            # Guard against degenerate values
            sl_pct = max(sl_pct, 0.3)
            tp_pct = max(tp_pct, 0.3)

            wick_size = (high_ - cls if direction == "short" else cls - low_) / atr_
            wick_score = min(1.0, wick_size / 2.0)
            score = round(max(0.50, min(0.88,
                0.55 + 0.20 * wick_score + 0.10 * (1.0 - adx_ / p.adx_max)
            )), 3)

            self.verbose_log_score(symbol, score, 0.50)

            vol_series = candles["volume"]
            vol_ma     = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma), 3) if vol_ma > 0 else None

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (
                    f"FELS: failed_{'high' if direction=='short' else 'low'}_break "
                    f"wick={wick_size:.2f}ATR adx={adx_:.1f}"
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":               "fels_strategy",
                    "entry_timeframe":              self.crypto_candle_timeframe,
                    "_entry_bar_time":              str(candles.index[-1]),
                    "wick_atr_size":                round(wick_size, 3),
                    "adx":                          round(adx_, 2),
                    "atr":                          round(atr_, 6),
                    "volume_ratio":                 vol_ratio,
                    "structural_stop_price":        round(stop_price, 6),
                    "preferred_initial_stop_mode":  "signal_structural",
                    "preferred_trail_mode":         "none",
                },
            )

        except Exception as e:
            self.logger.error(f"FELSStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        """Exit at max hold."""
        meta = entry_metadata or {}
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= self.params.max_bars_hold:
            return "fels_max_hold"
        return None
