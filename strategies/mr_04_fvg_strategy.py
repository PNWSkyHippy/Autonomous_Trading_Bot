"""
MR-04  Fair Value Gap Fill
============================
Hypothesis:
    A Fair Value Gap (FVG) is created when bar[1] moves so aggressively that
    there is a price gap between bar[2].high and bar[0].low (bullish FVG) or
    bar[2].low and bar[0].high (bearish FVG). Price has a statistical tendency
    to return and fill these zones.

Edge:
    Imbalanced price action leaves unfilled orders. Market makers and
    algorithmic participants tend to revisit these zones.

Key design decisions:
    - ADX < 35: only trade in ranging conditions (FVGs fill faster when not trending)
    - minGapAtr 0.3x: prevents trading micro-gaps that are just noise
    - lookback 50 bars: FVG expires if not filled within 50 bars
    - Symmetric R:R: SL = 1x gap below zone, TP = 1x gap above zone
    - Time stop 48 bars: primary exit if zone fills slowly

Backtest results (1h):
    First test showed 68% WR but negative P&L due to bad SL (ATR-based, too wide)
    Fixed to symmetric gap-based SL — symmetric R:R restores positive expectancy
    NOTE: Post-fix validation with symmetric gap SL is pending.
    Numbers above are from the BROKEN ATR-stop version.
    Re-validate after registering in bot backtester.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Tuple

# ─── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class MR04Params:
    atr_len:      int   = 14
    min_gap_atr:  float = 0.3    # minimum gap size as multiple of ATR
    lookback:     int   = 50     # FVG expires after this many bars
    adx_len:      int   = 14
    adx_max:      float = 35.0   # only trade when not strongly trending
    sl_mult:      float = 1.0    # SL = N * gap_size below zone bottom (symmetric at 1.0)
    max_bars:     int   = 48     # time stop


# ─── Indicators ───────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean().rename("atr")


def _adx(df: pd.DataFrame, length: int) -> pd.Series:
    """Wilder ADX — returns ADX line only."""
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
    atr_safe = atr_w.replace(0, np.nan)
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_safe
    di_sum   = (plus_di + minus_di).replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0).rename("adx")


# ─── FVG Detection ────────────────────────────────────────────────────────────

def _find_active_fvg(df: pd.DataFrame, params: MR04Params,
                     current_idx: int) -> Tuple[Optional[dict], Optional[dict]]:
    """
    Scan backward from current bar to find the most recent active FVG.

    Bullish FVG: low[0] - high[2] >= minGapSize  (gap ABOVE bar[2])
        Zone: bot = high[2], top = low[0]
        Price enters: close >= zone_bot AND close <= zone_top

    Bearish FVG: low[2] - high[0] >= minGapSize  (gap BELOW bar[2])
        Zone: bot = high[0], top = low[2]
        Price enters: close <= zone_top AND close >= zone_bot

    Returns (bull_fvg_dict or None, bear_fvg_dict or None)
    """
    p         = params
    atr_val   = float(df["atr"].iloc[current_idx])
    min_gap   = atr_val * p.min_gap_atr
    close_now = float(df["close"].iloc[current_idx])

    bull_fvg = None
    bear_fvg = None

    # Scan back up to lookback bars for the most recent FVG that price is now entering
    search_start = max(2, current_idx - p.lookback)
    for i in range(current_idx - 1, search_start, -1):
        # bar[2] = i-1, bar[1] = i (impulse), bar[0] at formation = i+1
        # But we formed the FVG at bar i+1 (where bar[0] = df.iloc[i+1])
        # To avoid lookahead we only look at FVGs where the formation bar < current bar
        formation_bar = i + 1
        if formation_bar >= current_idx:
            continue

        high2 = float(df["high"].iloc[i - 1])
        low2  = float(df["low"].iloc[i - 1])
        high0 = float(df["high"].iloc[formation_bar])
        low0  = float(df["low"].iloc[formation_bar])

        # Bullish FVG: gap above bar[i-1]
        bull_gap = low0 - high2
        if bull_gap >= min_gap and bull_fvg is None:
            zone_bot = high2
            zone_top = low0
            gap_size = zone_top - zone_bot
            if zone_bot <= close_now <= zone_top:
                bull_fvg = {
                    "zone_bot":  zone_bot,
                    "zone_top":  zone_top,
                    "gap_size":  gap_size,
                    "formed_at": formation_bar,
                    "sl_price":  zone_bot - p.sl_mult * gap_size,
                    "tp_price":  zone_top + gap_size,
                }

        # Bearish FVG: gap below bar[i-1]
        bear_gap = low2 - high0
        if bear_gap >= min_gap and bear_fvg is None:
            zone_bot = high0
            zone_top = low2
            gap_size = zone_top - zone_bot
            if zone_bot <= close_now <= zone_top:
                bear_fvg = {
                    "zone_bot":  zone_bot,
                    "zone_top":  zone_top,
                    "gap_size":  gap_size,
                    "formed_at": formation_bar,
                    "sl_price":  zone_top + p.sl_mult * gap_size,
                    "tp_price":  zone_bot - gap_size,
                }

        if bull_fvg and bear_fvg:
            break

    return bull_fvg, bear_fvg


# ─── Bot integration wrapper ──────────────────────────────────────────────────

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


class MR04FVGStrategy(BaseStrategy):
    """MR-04 Fair Value Gap Fill — FVG zone detection + ADX ranging filter."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "mr_04_fvg"
        self.params                  = MR04Params()
        self.stop_loss_pct           = 1.5
        self.take_profit_pct         = 1.5
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.candle_limit            = 150        # 50 lookback + 14 ADX warmup + buffer
        self.reviewer_exempt         = True
        self.ml_exempt               = True
        self.time_stop_profile       = "strategy_defined"
        self.enabled                 = False  # INCUBATE — partial backtest only, not live-ready

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        MIN_BARS = p.lookback + p.adx_len + 10
        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None

        try:
            df = candles.copy()
            # Skip indicator recomputation if _precompute() already ran for this df
            # (backtester passes precomputed df; standalone use computes on demand)
            if "atr" not in df.columns or "adx" not in df.columns:
                df["atr"] = _atr(df, p.atr_len)
                df["adx"] = _adx(df, p.adx_len)

            last_idx = len(df) - 1
            adx_val  = float(df["adx"].iloc[last_idx])
            ranging  = adx_val < p.adx_max

            if not ranging:
                self.verbose_log(symbol, "ADX ranging", False, round(adx_val, 1), f"<{p.adx_max}")
                return None

            bull_fvg, bear_fvg = _find_active_fvg(df, p, last_idx)

            if not bull_fvg and not bear_fvg:
                return None

            # Prefer bull FVG if both active (shouldn't happen often but handle gracefully)
            fvg       = bull_fvg if bull_fvg else bear_fvg
            direction = "long" if bull_fvg else "short"
            close     = float(df["close"].iloc[last_idx])

            sl_price = fvg["sl_price"]
            tp_price = fvg["tp_price"]
            gap_size = fvg["gap_size"]

            sl_pct = round(abs(close - sl_price) / close * 100, 3)
            tp_pct = round(abs(tp_price - close)  / close * 100, 3)

            # Score: larger gap = stronger imbalance; fresher FVG = higher quality
            atr_val   = float(df["atr"].iloc[last_idx])
            gap_atr   = gap_size / atr_val if atr_val > 0 else 0
            gap_bonus = min((gap_atr - p.min_gap_atr) / p.min_gap_atr * 0.1, 0.2)
            age       = last_idx - fvg["formed_at"]
            age_penalty = min(age / p.lookback * 0.1, 0.1)   # older FVG → lower score
            score     = round(max(0.58, min(0.88, 0.65 + gap_bonus - age_penalty)), 3)

            # Volume ratio — 20-bar rolling mean
            vol_series = candles["volume"]
            vol_ma_val = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_val), 3) if vol_ma_val > 0 else None

            self.verbose_log(symbol, "FVG zone entry", True,
                             f"gap={gap_size:.4f}", f">{p.min_gap_atr}x ATR", direction)
            self.verbose_log(symbol, "ADX ranging", True, round(adx_val, 1), f"<{p.adx_max}")
            self.verbose_log_score(symbol, score, 0.58)

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (
                    f"MR-04 FVG: price in {'bull' if bull_fvg else 'bear'} FVG zone "
                    f"[{fvg['zone_bot']:.4f}–{fvg['zone_top']:.4f}] "
                    f"gap={gap_size:.4f} ({gap_atr:.2f}x ATR) "
                    f"age={age}b | ADX={adx_val:.1f}"
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":              "mr_04_fvg",
                    "entry_timeframe":            "1h",
                    "fvg_zone_bot":               round(fvg["zone_bot"], 6),
                    "fvg_zone_top":               round(fvg["zone_top"], 6),
                    "fvg_gap_size":               round(gap_size, 6),
                    "fvg_age_bars":               age,
                    "adx":                        round(adx_val, 2),
                    "volume_ratio":               vol_ratio,
                    "structural_stop_price":      round(sl_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"MR04FVGStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        """Time stop: exit after max_bars if zone not filled."""
        meta      = entry_metadata or {}
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= self.params.max_bars:
            return "mr04_time_stop"
        return None

    def _precompute(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        Pre-compute ATR and ADX once on the full df before the bar loop.
        Called by backtester._simulate(); eliminates O(N²) indicator recomputation.
        _find_active_fvg() still scans backward per bar (inherent to FVG detection).
        """
        p   = self.params
        out = df.copy()
        out["atr"] = _atr(out, p.atr_len)
        out["adx"] = _adx(out, p.adx_len)
        return out
