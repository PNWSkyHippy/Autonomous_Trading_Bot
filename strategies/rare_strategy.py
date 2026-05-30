"""
RARE — Rolling Autocorrelation Regime Entry
=============================================
Classification : Reject v1 (Trader Dev result: PF 1.12, exits broken)
Timeframe      : 1h

Mathematical hypothesis
-----------------------
Lag-1 autocorrelation of returns transitions between:
  positive → trend-following regime (momentum)
  negative → mean-reverting regime (fade the move)

Trading the SIGN CHANGE of rolling autocorrelation, aligned with a Z-score
extreme, targets the exact moment a market regime flips from trending to
mean-reverting with price displaced far from its mean.

Entry conditions:
  1. Rolling autocorr flips from positive to negative (event, not state)
  2. Z-score of close vs rolling mean is at extreme (|Z| >= z_thresh)
  3. Trade AGAINST the displacement (Z > z_thresh → short, Z < -z_thresh → long)
  4. ADX declining (directional momentum weakening confirms regime shift)

Exit: Z-score reverts to neutral band (|Z| < z_exit), or max_bars_hold
Stop: ATR-based

NOTE: The Trader Dev v1 result had broken exits (no Z-score reversion exit
implemented in Pine). This Python version implements correct exits from scratch.
Parameters are UNTUNED first-pass defaults — hypothesis validation only.
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
class RAREParams:
    # Autocorrelation
    autocorr_window:  int   = 20    # Rolling window for lag-1 autocorr of returns
    min_obs:          int   = 15    # Minimum valid observations in window

    # Z-score
    zscore_window:    int   = 50    # Rolling mean/std for Z-score
    z_entry:          float = 1.8   # |Z| must exceed this to enter
    z_exit:           float = 0.5   # Exit when |Z| falls below this (reversion done)

    # ATR
    atr_length:       int   = 14
    sl_atr_mult:      float = 2.0   # Wider stop — regime trades can be choppy early
    tp_atr_mult:      float = 2.5

    # Regime confirmation
    adx_period:       int   = 14
    adx_declining_bars: int = 3     # ADX must be declining for this many bars

    # Duration
    max_bars_hold:    int   = 24


class RAREStrategy(BaseStrategy):
    """Rolling Autocorrelation Regime Entry — fade extreme displacement at regime flip."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "rare_strategy"
        self.params                  = RAREParams()
        self.stop_loss_pct           = 2.0
        self.take_profit_pct         = 2.5
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.reviewer_exempt         = True
        self.ml_exempt               = True
        self.time_stop_profile       = "strategy_defined"
        self.enabled                 = False  # WATCHLIST — needs backtest validation before live

        p = self.params
        self.candle_limit = max(p.zscore_window, p.autocorr_window) + p.adx_period + 30

    def _compute_adx(self, df: pd.DataFrame) -> pd.Series:
        p    = self.params
        high, low, close = df["high"], df["low"], df["close"]
        up   = high.diff()
        down = -low.diff()
        pdm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
        ndm  = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
        tr   = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr  = tr.ewm(span=p.adx_period, adjust=False).mean()
        pdi  = 100 * pdm.ewm(span=p.adx_period, adjust=False).mean() / atr.replace(0, np.nan)
        ndi  = 100 * ndm.ewm(span=p.adx_period, adjust=False).mean() / atr.replace(0, np.nan)
        dx   = (100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan))
        return dx.ewm(span=p.adx_period, adjust=False).mean()

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        MIN_BARS = max(p.zscore_window, p.autocorr_window) + p.adx_period + 10
        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None

        try:
            df  = candles.copy()
            c   = df["close"]
            ret = c.pct_change()

            # ── Lag-1 autocorrelation ─────────────────────────────────────────
            def rolling_autocorr(series: pd.Series, window: int, lag: int = 1) -> pd.Series:
                # Vectorized rolling corr — O(N), not O(N²) like the lambda approach.
                lagged = series.shift(lag)
                return series.rolling(window).corr(lagged)

            autocorr = rolling_autocorr(ret, p.autocorr_window)

            # Event: autocorr crosses from positive to negative
            ac_prev = autocorr.shift(1)
            regime_flip = (ac_prev > 0) & (autocorr < 0)

            # ── Z-score ───────────────────────────────────────────────────────
            roll_mean = c.rolling(p.zscore_window).mean()
            roll_std  = c.rolling(p.zscore_window).std().replace(0, np.nan)
            zscore    = (c - roll_mean) / roll_std

            # ── ATR ───────────────────────────────────────────────────────────
            hl   = df["high"] - df["low"]
            hpc  = (df["high"] - c.shift()).abs()
            lpc  = (df["low"]  - c.shift()).abs()
            tr   = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
            atr  = tr.ewm(span=p.atr_length, adjust=False).mean()

            # ── ADX declining ─────────────────────────────────────────────────
            adx        = self._compute_adx(df)
            adx_prev_n = adx.shift(p.adx_declining_bars)
            adx_declining = adx < adx_prev_n

            last = df.index[-1]
            flip_ = bool(regime_flip.loc[last])
            z_    = float(zscore.loc[last]) if not pd.isna(zscore.loc[last]) else 0.0
            adxd_ = bool(adx_declining.loc[last])
            atr_  = float(atr.loc[last])
            cls   = float(c.loc[last])
            ac_   = float(autocorr.loc[last]) if not pd.isna(autocorr.loc[last]) else 0.0

            self.verbose_log(symbol, "regime_flip",    flip_,          ac_,  "autocorr crossed 0 from +")
            self.verbose_log(symbol, "z_extreme",      abs(z_) >= p.z_entry, z_, f"±{p.z_entry}")
            self.verbose_log(symbol, "adx_declining",  adxd_,          float(adx.loc[last]), f"declining {p.adx_declining_bars}b")

            if not flip_:
                return None
            if abs(z_) < p.z_entry:
                return None
            if not adxd_:
                return None

            direction = "short" if z_ > 0 else "long"

            sl_pct = round(atr_ * p.sl_atr_mult / cls * 100, 3)
            tp_pct = round(atr_ * p.tp_atr_mult / cls * 100, 3)
            structural_stop = (
                cls - atr_ * p.sl_atr_mult if direction == "long"
                else cls + atr_ * p.sl_atr_mult
            )

            z_extreme_score = min(1.0, (abs(z_) - p.z_entry) / p.z_entry)
            score = round(max(0.50, min(0.88,
                0.55 + 0.25 * z_extreme_score
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
                    f"RARE: autocorr_flip ac={ac_:.3f} z={z_:.2f} "
                    f"(thresh±{p.z_entry}) adx_declining={adxd_}"
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":               "rare_strategy",
                    "entry_timeframe":              self.crypto_candle_timeframe,
                    "_entry_bar_time":              str(candles.index[-1]),
                    "autocorr":                     round(ac_, 4),
                    "zscore":                       round(z_, 3),
                    "z_exit_thresh":                p.z_exit,
                    "atr":                          round(atr_, 6),
                    "volume_ratio":                 vol_ratio,
                    "structural_stop_price":        round(structural_stop, 6),
                    "preferred_initial_stop_mode":  "signal_structural",
                    "preferred_trail_mode":         "none",
                },
            )

        except Exception as e:
            self.logger.error(f"RAREStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        """Exit when Z-score reverts to neutral, or max_bars_hold."""
        if bars is None or len(bars) < self.params.zscore_window + 2:
            return None

        p    = self.params
        meta = entry_metadata or {}
        c    = bars["close"]

        roll_mean = c.rolling(p.zscore_window).mean()
        roll_std  = c.rolling(p.zscore_window).std().replace(0, np.nan)
        z_now     = float(((c - roll_mean) / roll_std).iloc[-1])

        if not np.isnan(z_now) and abs(z_now) < p.z_exit:
            return "rare_z_revert"

        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= p.max_bars_hold:
            return "rare_max_hold"

        return None
