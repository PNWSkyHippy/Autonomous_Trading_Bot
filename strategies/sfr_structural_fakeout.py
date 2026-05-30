"""
SFR — Structural Fakeout Reversal
==================================
Hypothesis
----------
Price pokes BEYOND a *structural* level (opening-range high/low or prior-day
high/low), tripping resting stops and breakout chasers, then closes back
INSIDE the level within 1-2 bars. The breakout has failed and the chasers are
trapped. Enter the OPPOSITE direction, targeting the other side of the
structure. Stop sits just beyond the fakeout extreme (the liquidity-grab wick).

This is the structural-level analogue of mr_03_fbs (which fades Bollinger-Band
false breaks). It keys off levels traders actually defend (ORB, prior day)
rather than a volatility band.

Pattern
-------
  Upside fakeout  : a bar HIGH > resistance level (ORB high / prior-day high)
                    by >= poke_min_atr x ATR, current bar CLOSES back below it  -> SHORT
  Downside fakeout: a bar LOW  < support level (ORB low / prior-day low)
                    by >= poke_min_atr x ATR, current bar CLOSES back above it  -> LONG

  Stop   : beyond the fakeout extreme (poke high/low) +/- sl_atr_buffer x ATR
  Target : toward the opposite structural level (tp_range_frac of the way),
           floored at tp_min_rr x structural risk
  Exit   : time stop at max_bars (TP/SL are structural guards only)

Filters
-------
  - poke_max_atr: reject pokes too large to be a fakeout (real breakout / gap)
  - vol_spike_min: the poke bar must carry volume (a real trap, not a drift)
  - Sentiment gate (mirrors mr_03_fbs v5.1): block SHORTS when morning
    sentiment is BULLISH, block LONGS when BEARISH. NEUTRAL/UNKNOWN pass.

RELATION TO fels_strategy.py (READ THIS)
----------------------------------------
fels_strategy ("Failed Extension Liquidity Sweep", Strategy 30) also fades a
failed breakout, but of a ROLLING N-bar high/low, on a SINGLE bar, with the
close allowed to remain within 1 ATR *outside* the level, targeting only a
PARTIAL (0.5x) reversion. SFR differs deliberately:
  - Levels      : named structural levels (ORB, prior-day) vs rolling extreme
  - Confirmation: close back strictly INSIDE within 1-2 bars vs single-bar,
                  close-still-outside-within-1ATR
  - Target      : the OPPOSITE structural level vs a fixed partial revert
These are distinct edges; if both are deployed, monitor for correlated signals.

STATUS: WATCHLIST — UNTUNED first-pass defaults. Backtest before enabling live.
"""

import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pandas as pd

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:  # allows standalone import in a backtest harness
    BaseStrategy = object
    TradeSignal  = None

_log = logging.getLogger(__name__)


def _get_market_sentiment() -> str:
    """
    Return the morning-briefing sentiment ("BULLISH"/"BEARISH"/"NEUTRAL"/
    "UNKNOWN"). Fails silently so a reviewer outage never blocks signals.
    Mirrors mr_03_fbs_strategy._get_market_sentiment.
    """
    try:
        from intelligence.claude_reviewer import claude_reviewer
        ctx = claude_reviewer.get_morning_context()
        return (ctx.market_sentiment or "UNKNOWN").upper()
    except Exception:
        return "UNKNOWN"


# ─── Parameters ────────────────────────────────────────────────────────────────

@dataclass
class SFRParams:
    snap_bars:        int   = 2      # poke must close back inside within this many bars
    orb_bars_crypto:  int   = 4      # 4 x 15m = first 60 min (matches orb_breakout)
    orb_bars_stock:   int   = 6      # 6 x 5m  = first 30 min (matches orb_breakout)
    atr_len:          int   = 14
    poke_min_atr:     float = 0.10   # poke beyond the level must be >= this x ATR (filters noise)
    poke_max_atr:     float = 2.5    # poke beyond the level must be <= this x ATR (else real break/gap)
    sl_atr_buffer:    float = 0.25   # stop = fakeout extreme +/- this x ATR
    vol_spike_min:    float = 1.20   # poke-window volume >= this x 20-bar avg (stronger trap)
    tp_range_frac:    float = 0.75   # target = this fraction of the distance to the opposite level
    tp_min_rr:        float = 1.5    # floor the target at this R multiple of structural risk
    max_bars:         int   = 24     # time stop (primary exit)
    use_prior_day:    bool  = True   # fade fakeouts of prior-day high/low
    use_orb:          bool  = True   # fade fakeouts of opening-range high/low (stocks only — crypto ORB is midnight UTC noise)
    use_orb_crypto:   bool  = False  # disabled: crypto has no meaningful session open; prior-day only
    long_only:        bool  = False  # both directions; sentiment gate handles regime


# ─── Indicators ──────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean().rename("atr")


# ─── Strategy ────────────────────────────────────────────────────────────────

class SFRStrategy(BaseStrategy):
    """Structural Fakeout Reversal — fade ORB / prior-day false breaks."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "sfr_structural_fakeout"
        self.params                  = SFRParams()
        self.stop_loss_pct           = 1.5    # fallback only — overridden per-signal
        self.take_profit_pct         = 2.5    # fallback only — overridden per-signal
        self.stock_candle_timeframe  = "5Min"
        self.crypto_candle_timeframe = "15m"  # 15m so ORB matches orb_breakout's definition
        self.candle_limit            = 300
        self.crypto_enabled          = False  # no edge on crypto — fee hurdle 0.62%, best PF was 0.42
        self.stock_enabled           = True   # selective edge: AAPL long, NVDA short, TSLA long confirmed
        self.reviewer_exempt         = True
        self.ml_exempt               = True
        self.auto_disable_exempt     = True   # protect during incubation (mirrors orb_breakout)
        self.time_stop_profile       = "strategy_defined"
        self.enabled                 = False  # WATCHLIST — backtest before enabling live

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _normalise_index(self, candles: pd.DataFrame, asset_class: str) -> pd.DataFrame:
        """Stocks -> America/New_York (naive); crypto -> UTC (naive). Mirrors orb_breakout."""
        out = candles.copy()
        idx = pd.DatetimeIndex(out.index)
        try:
            if idx.tz is not None:
                if asset_class == "stock":
                    from zoneinfo import ZoneInfo
                    idx = idx.tz_convert(ZoneInfo("America/New_York")).tz_localize(None)
                else:
                    idx = idx.tz_convert("UTC").tz_localize(None)
        except Exception:
            pass
        out.index = idx
        return out.sort_index()

    def _structural_levels(
        self, df: pd.DataFrame, asset_class: str, p: SFRParams
    ) -> List[Tuple[str, float, str]]:
        """Return [(name, price, kind)] where kind is 'resistance' or 'support'."""
        levels: List[Tuple[str, float, str]] = []
        if asset_class == "stock":
            try:
                sdf = df.between_time("09:30", "16:00")
            except Exception:
                sdf = df
        else:
            sdf = df
        if sdf.empty:
            return levels

        day      = pd.Index([ts.date() for ts in sdf.index])
        today    = day[-1]
        today_df = sdf[day == today]

        # ORB: enabled for stocks; disabled for crypto (crypto ORB = midnight UTC = noise)
        orb_enabled = p.use_orb if asset_class != "crypto" else p.use_orb_crypto
        if orb_enabled and not today_df.empty:
            orb_bars = p.orb_bars_crypto if asset_class == "crypto" else p.orb_bars_stock
            if len(today_df) > orb_bars:
                opening = today_df.iloc[:orb_bars]
                levels.append(("ORB_high", float(opening["high"].max()), "resistance"))
                levels.append(("ORB_low",  float(opening["low"].min()),  "support"))

        if p.use_prior_day:
            prior_days = [d for d in pd.unique(day) if d < today]
            if prior_days:
                pday = max(prior_days)
                pdf  = sdf[day == pday]
                if not pdf.empty:
                    levels.append(("prior_day_high", float(pdf["high"].max()), "resistance"))
                    levels.append(("prior_day_low",  float(pdf["low"].min()),  "support"))

        return levels

    def _target_price(
        self, p: SFRParams, direction: str, cur_close: float, risk: float,
        levels: List[Tuple[str, float, str]]
    ) -> float:
        """Target the opposite structural level, floored at tp_min_rr x risk."""
        if direction == "short":
            supports  = [lvl for (_, lvl, kind) in levels if kind == "support" and lvl < cur_close]
            struct_tp = cur_close - p.tp_range_frac * (cur_close - max(supports)) if supports else None
            rr_tp     = cur_close - p.tp_min_rr * risk
            return min(struct_tp, rr_tp) if struct_tp is not None else rr_tp
        else:
            resist    = [lvl for (_, lvl, kind) in levels if kind == "resistance" and lvl > cur_close]
            struct_tp = cur_close + p.tp_range_frac * (min(resist) - cur_close) if resist else None
            rr_tp     = cur_close + p.tp_min_rr * risk
            return max(struct_tp, rr_tp) if struct_tp is not None else rr_tp

    # ── Main analysis ─────────────────────────────────────────────────────────

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        if not self._check_enough_candles(symbol, candles, p.atr_len + 30):
            return None

        try:
            asset_class = "crypto" if "/" in symbol else "stock"
            df = self._normalise_index(candles, asset_class)
            if df.empty or len(df) < p.atr_len + 5:
                return None

            atr_val = _atr(df, p.atr_len).iloc[-1]
            if pd.isna(atr_val) or atr_val <= 0:
                return None
            atr = float(atr_val)

            cur_close = float(df["close"].iloc[-1])

            levels = self._structural_levels(df, asset_class, p)
            if not levels:
                self.verbose_log_skip(symbol, "No structural levels available")
                return None

            recent    = df.iloc[-p.snap_bars:]
            poke_high = float(recent["high"].max())
            poke_low  = float(recent["low"].min())

            vol_ma   = float(df["volume"].rolling(20).mean().iloc[-1]) if len(df) >= 20 else float(df["volume"].mean())
            poke_vol = float(recent["volume"].max())
            vol_ratio = (poke_vol / vol_ma) if vol_ma > 0 else 0.0

            # (poke_in_atr, direction, level_name, level_price, stop_price)
            candidates: List[Tuple[float, str, str, float, float]] = []
            for name, lvl, kind in levels:
                if kind == "resistance":
                    beyond = poke_high - lvl
                    if (p.poke_min_atr * atr <= beyond <= p.poke_max_atr * atr) and cur_close < lvl:
                        stop_price = poke_high + p.sl_atr_buffer * atr
                        candidates.append((beyond / atr, "short", name, lvl, stop_price))
                else:  # support
                    beyond = lvl - poke_low
                    if (p.poke_min_atr * atr <= beyond <= p.poke_max_atr * atr) and cur_close > lvl:
                        stop_price = poke_low - p.sl_atr_buffer * atr
                        candidates.append((beyond / atr, "long", name, lvl, stop_price))

            if not candidates:
                return None
            if vol_ratio < p.vol_spike_min:
                self.verbose_log_skip(symbol, f"Weak trap — vol {vol_ratio:.2f}x < {p.vol_spike_min}x")
                return None

            candidates.sort(reverse=True)   # strongest trap = biggest poke beyond the level
            poke_atr, direction, level_name, level_price, stop_price = candidates[0]

            if p.long_only and direction == "short":
                return None

            sentiment = _get_market_sentiment()
            if direction == "short" and sentiment == "BULLISH":
                _log.debug("[SFR] %s SHORT blocked — sentiment BULLISH", symbol)
                return None
            if direction == "long" and sentiment == "BEARISH":
                _log.debug("[SFR] %s LONG blocked — sentiment BEARISH", symbol)
                return None

            risk = (stop_price - cur_close) if direction == "short" else (cur_close - stop_price)
            if risk <= 0:
                self.verbose_log_skip(symbol, "Structural risk <= 0")
                return None

            tp_price = self._target_price(p, direction, cur_close, risk, levels)

            if direction == "short":
                if tp_price >= cur_close:
                    return None
                tp_pct = round((cur_close - tp_price) / cur_close * 100, 3)
            else:
                if tp_price <= cur_close:
                    return None
                tp_pct = round((tp_price - cur_close) / cur_close * 100, 3)
            sl_pct = round(risk / cur_close * 100, 3)
            sl_pct = max(sl_pct, 0.3)
            tp_pct = max(tp_pct, 0.3)

            poke_score = min(1.0, poke_atr / 1.5)
            vol_score  = min(1.0, max(0.0, vol_ratio - p.vol_spike_min))
            score = round(max(0.55, min(0.90, 0.60 + 0.18 * poke_score + 0.08 * vol_score)), 3)

            self.verbose_log(symbol, "structural fakeout", True, round(cur_close, 6),
                             f"{level_name}={level_price:.4f} poke={poke_atr:.2f}ATR", direction)
            self.verbose_log_score(symbol, score, 0.55)

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (
                    f"SFR: fakeout of {level_name} ({level_price:.4f}) "
                    f"poke={poke_atr:.2f}ATR vol={vol_ratio:.2f}x closed back inside "
                    f"| sentiment={sentiment}"
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":               "sfr_structural_fakeout",
                    "entry_timeframe":             self.crypto_candle_timeframe if asset_class == "crypto" else self.stock_candle_timeframe,
                    "entry_type":                  f"structural_fakeout_{direction}",
                    "faded_level_name":            level_name,
                    "faded_level_price":           round(level_price, 6),
                    "fakeout_extreme":             round(poke_high if direction == "short" else poke_low, 6),
                    "poke_atr":                    round(poke_atr, 3),
                    "atr":                         round(atr, 6),
                    "volume_ratio":                round(vol_ratio, 3),
                    "market_sentiment":            sentiment,
                    "target_price":                round(tp_price, 6),
                    "structural_stop_price":       round(stop_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                    "entry_time_utc":              datetime.now(timezone.utc).isoformat(),
                    "_entry_bar_time":             str(candles.index[-1]),
                },
            )

        except Exception as e:
            self.logger.error(f"SFRStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        """Time stop: primary exit after max_bars (TP/SL are structural guards)."""
        meta      = entry_metadata or {}
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= self.params.max_bars:
            return "sfr_time_stop"
        return None
