"""
CBAE — Candle Body Asymmetry Exhaustion
========================================
Classification : Watchlist (original Trader Dev result: PF 1.27, not swept cross-symbol)
Timeframe      : 1h

Mathematical hypothesis
-----------------------
The ratio of candle body to total range encodes directional commitment.
A running signed sum of body ratios accumulates when price moves with consistent
body-heavy candles (committed moves). When that accumulated commitment reaches an
extreme BUT individual bodies then start shrinking (absorption — the crowd is still
leaning but losing force), exhaustion is imminent.

Body ratio = |close - open| / max(high - low, 0.001)
Signed    : +body_ratio if bullish bar, -body_ratio if bearish
Sum       : rolling sum over N bars → extreme when > threshold or < -threshold
Absorption: sum extreme AND body shrinkage (last 3 bars avg < body_ma * shrink_factor)
Regime    : ADX < adx_max to avoid strong trending conditions

Entry: opposite direction to the commitment extreme (mean reversion)
Exit : custom — hold until body sum reverts past zero, or max_bars_hold
Stop : ATR-based catastrophic stop

NOTE: Parameters are UNTUNED first-pass defaults. This is a hypothesis validation
run against yfinance data — no optimization has been performed on this dataset.
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
class CBAEParams:
    # Body asymmetry accumulation
    body_window:      int   = 14    # Rolling window for body sum
    extreme_thresh:   float = 5.0   # Body sum extreme (max = window * 1.0 = fully one-sided)
    shrink_factor:    float = 0.6   # Absorption: last 3 bar avg body < this * body_ma

    # ATR stops
    atr_length:       int   = 14
    sl_atr_mult:      float = 1.5
    tp_atr_mult:      float = 2.0

    # Regime — avoid strong trends
    adx_period:       int   = 14
    adx_max:          float = 30.0  # Mean reversion dies above this

    # Macro trend for direction filter
    trend_ma_length:  int   = 100

    # Duration
    max_bars_hold:    int   = 20


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
    adx  = dx.ewm(span=period, adjust=False).mean()
    return adx


class CBAEStrategy(BaseStrategy):
    """Candle Body Asymmetry Exhaustion — commitment accumulation + absorption reversal."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "cbae_strategy"
        self.params                  = CBAEParams()
        self.stop_loss_pct           = 1.5
        self.take_profit_pct         = 2.0
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.reviewer_exempt         = True   # ATR-based dynamic stops
        self.ml_exempt               = True   # No historical data yet
        self.time_stop_profile       = "strategy_defined"

        p = self.params
        self.candle_limit = max(p.trend_ma_length, p.body_window, p.atr_length) + 30

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        MIN_BARS = max(p.trend_ma_length, p.body_window * 2, p.atr_length) + 10
        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None

        try:
            df = candles.copy()
            c  = df["close"]

            # ── Body ratio ────────────────────────────────────────────────────
            body     = (df["close"] - df["open"]).abs()
            rng      = (df["high"] - df["low"]).clip(lower=0.0001)
            body_ratio = body / rng                            # 0..1

            # Signed: positive = bullish bar, negative = bearish bar
            sign        = np.where(df["close"] >= df["open"], 1.0, -1.0)
            signed_body = pd.Series(sign * body_ratio.values, index=df.index)

            # Accumulated body commitment
            body_sum = signed_body.rolling(p.body_window).sum()
            body_ma  = body_ratio.rolling(p.body_window).mean()

            # Absorption: recent bodies shrinking relative to their own average
            body_last3_avg = body_ratio.rolling(3).mean()
            absorbing      = body_last3_avg < body_ma * p.shrink_factor

            # ── ATR ────────────────────────────────────────────────────────────
            hl   = df["high"] - df["low"]
            hpc  = (df["high"] - c.shift()).abs()
            lpc  = (df["low"]  - c.shift()).abs()
            tr   = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
            atr  = tr.ewm(span=p.atr_length, adjust=False).mean()

            # ── ADX regime ─────────────────────────────────────────────────────
            adx = _compute_adx(df, p.adx_period)

            # ── Macro trend ────────────────────────────────────────────────────
            macro_ma   = c.rolling(p.trend_ma_length).mean()
            bull_regime = c > macro_ma
            bear_regime = c < macro_ma

            # ── Signals ────────────────────────────────────────────────────────
            last = df.index[-1]
            bs   = float(body_sum.loc[last])
            adx_ = float(adx.loc[last])
            abs_ = bool(absorbing.loc[last])
            atr_ = float(atr.loc[last])
            cls  = float(c.loc[last])

            self.verbose_log(symbol, "body_sum_extreme",  abs(bs) >= p.extreme_thresh, bs, f"±{p.extreme_thresh}")
            self.verbose_log(symbol, "absorption",         abs_,  float(body_last3_avg.loc[last]), f"< {p.shrink_factor}×body_ma")
            self.verbose_log(symbol, "adx_below_max",      adx_ <= p.adx_max, adx_, p.adx_max)

            if adx_ > p.adx_max:
                return None
            if not abs_:
                return None

            direction = None
            if bs >= p.extreme_thresh and bool(bear_regime.loc[last]):
                direction = "short"   # extreme bullish commitment but absorbing → expect down
            elif bs <= -p.extreme_thresh and bool(bull_regime.loc[last]):
                direction = "long"    # extreme bearish commitment but absorbing → expect up

            if direction is None:
                return None

            sl_pct = round(atr_ * p.sl_atr_mult / cls * 100, 3)
            tp_pct = round(atr_ * p.tp_atr_mult / cls * 100, 3)

            structural_stop = (
                cls - atr_ * p.sl_atr_mult if direction == "long"
                else cls + atr_ * p.sl_atr_mult
            )

            exhaustion_depth = min(1.0, (abs(bs) - p.extreme_thresh) / p.body_window)
            score = round(max(0.50, min(0.88,
                0.55 + 0.25 * exhaustion_depth + 0.10 * (1.0 - adx_ / p.adx_max)
            )), 3)

            self.verbose_log_score(symbol, score, 0.50)

            entry_bar_time = str(candles.index[-1])

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (
                    f"CBAE: body_sum={bs:.2f} (thresh±{p.extreme_thresh}) "
                    f"absorbing=True adx={adx_:.1f}"
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":         "cbae_strategy",
                    "entry_timeframe":        self.crypto_candle_timeframe,
                    "_entry_bar_time":        entry_bar_time,
                    "body_sum":               round(bs, 4),
                    "adx":                    round(adx_, 2),
                    "atr":                    round(atr_, 6),
                    "structural_stop_price":  round(structural_stop, 6),
                    "preferred_trail_mode":   "none",
                },
            )

        except Exception as e:
            self.logger.error(f"CBAEStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        """Exit when body_sum reverts toward zero, or max_bars_hold reached."""
        if bars is None or len(bars) < self.params.body_window + 2:
            return None

        p    = self.params
        meta = entry_metadata or {}

        # Recompute body sum on current bars
        body    = (bars["close"] - bars["open"]).abs()
        rng     = (bars["high"]  - bars["low"]).clip(lower=0.0001)
        sign    = np.where(bars["close"] >= bars["open"], 1.0, -1.0)
        signed  = pd.Series(sign * (body / rng).values, index=bars.index)
        bsum    = float(signed.rolling(p.body_window).sum().iloc[-1])

        # Exit when body sum crosses zero (exhaustion played out)
        entry_sum = float(meta.get("body_sum", 0.0))
        if entry_sum >= p.extreme_thresh and bsum <= 0.0:
            return "cbae_sum_revert"
        if entry_sum <= -p.extreme_thresh and bsum >= 0.0:
            return "cbae_sum_revert"

        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= p.max_bars_hold:
            return "cbae_max_hold"

        return None
