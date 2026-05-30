"""
MR-03 v5  False Breakout Snap
================================
Hypothesis:
    When a bar closes outside a Bollinger Band and the following 1-2 bars
    close back inside, retail breakout chasers are trapped. The snap-back bar
    is the entry. The move is faded back toward the mean over 36 hours.

Edge:
    False BB breakouts on 1h crypto trap breakout participants. The snap-back
    has a directional drift bias (~62% WR on BTC, tested 2022-2025).

Key design decisions (v5):
    - BB Mult 1.75: 27-combo TV sweep confirmed dominance over 2.0
    - snapBars=2: 1-bar snaps miss valid setups; 3-bar snaps take stale entries
    - SMA200 direction filter: long only above SMA200, short only below
    - No ADX filter: BB breakouts correlate POSITIVELY with ADX — kills signals
    - Time stop at 36 bars: sweep showed 36-bar plateau outperforms 48-72
    - ATR-based TP/SL: emergency guards only (time stop is the primary exit)

Sentiment gate (v5.1):
    Live performance showed catastrophic losses when shorting into BULLISH sessions
    (16.7% WR, -$127 in a single BULLISH day, 2026-05-25).
    Root cause: no market regime awareness — strategy shorted all morning into
    9/9 green sectors, conviction 72 BULLISH.
    Fix: block shorts when sentiment is BULLISH; block longs when BEARISH.
    Neutral/Unknown → pass through (conservative default).

Backtest results (BTCUSDT 1h, 2022-2025):
    v4 long-only (above SMA200): 209 trades | PF 1.39 | WR 56.0% | +6.83%
    v5 best combo (both dirs):   227 trades | PF 1.07 | WR 61.7% | +0.73%
    Note: long-only above SMA200 significantly outperforms both-dirs
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)


def _get_market_sentiment() -> str:
    """
    Return the current morning-briefing sentiment ("BULLISH", "BEARISH",
    "NEUTRAL", or "UNKNOWN").  Fails silently so a reviewer outage never
    blocks trade signals.
    """
    try:
        from intelligence.claude_reviewer import claude_reviewer
        ctx = claude_reviewer.get_morning_context()
        return (ctx.market_sentiment or "UNKNOWN").upper()
    except Exception:
        return "UNKNOWN"

# ─── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class MR03Params:
    bb_len:     int   = 20
    bb_mult:    float = 1.75   # 27-combo sweep: 1.75 dominates 2.0
    snap_bars:  int   = 2      # look back N bars for the outside-band close
    ma_len:     int   = 200    # SMA200 trend direction filter
    atr_len:    int   = 14
    tp_mult:    float = 1.5    # emergency TP guard — time stop is primary exit
    sl_mult:    float = 2.0
    max_bars:   int   = 36     # time stop — sweep plateau at 36 bars
    # long_only defaults to True: v4 long-only (PF 1.39, +6.83%) dominates
    # v5 both-dirs (PF 1.07, +0.73%) per documented backtest — see class header.
    # Set to False to enable shorts (below SMA200) for research/comparison only.
    long_only:  bool  = True


# ─── Indicators ───────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean().rename("atr")


# ─── Signal generation ────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame, params: MR03Params = MR03Params()) -> pd.DataFrame:
    """
    Returns DataFrame with signal columns added.
    No lookahead — all on confirmed bars.

    Signal logic:
        Scan the last snap_bars bars: did any close outside a BB band?
        If yes AND current bar has snapped back inside → signal
        Long : broke below BB lower → snapped back above → above SMA200
        Short: broke above BB upper → snapped back below → below SMA200
    """
    p   = params
    out = df.copy()

    out["bb_mid"]   = out["close"].rolling(p.bb_len).mean()
    bb_std          = out["close"].rolling(p.bb_len).std()
    out["bb_upper"] = out["bb_mid"] + p.bb_mult * bb_std
    out["bb_lower"] = out["bb_mid"] - p.bb_mult * bb_std
    out["atr"]      = _atr(out, p.atr_len)
    out["ma200"]    = out["close"].rolling(p.ma_len).mean()

    # Check if any of the last snap_bars bars broke outside a band
    # We need to look back snap_bars candles (not including current bar)
    broke_up = pd.Series(False, index=out.index)
    broke_dn = pd.Series(False, index=out.index)
    for i in range(1, p.snap_bars + 1):
        broke_up = broke_up | (out["close"].shift(i) > out["bb_upper"].shift(i))
        broke_dn = broke_dn | (out["close"].shift(i) < out["bb_lower"].shift(i))

    # Current bar snapped back inside
    snap_back_up = broke_dn & (out["close"] > out["bb_lower"])   # broke below → now inside
    snap_back_dn = broke_up & (out["close"] < out["bb_upper"])   # broke above → now inside

    bull_bias = out["close"] > out["ma200"]
    bear_bias = out["close"] < out["ma200"]

    out["long_signal"]  = snap_back_up & bull_bias
    # Short signals available but long-only historically outperforms — see class header.
    # Disabled by default via params.long_only=True (v4 PF 1.39 vs v5 both-dirs PF 1.07).
    if params.long_only:
        out["short_signal"] = pd.Series(False, index=out.index)
    else:
        out["short_signal"] = snap_back_dn & bear_bias

    out["sl_long"]  = out["close"] - p.sl_mult * out["atr"]
    out["sl_short"] = out["close"] + p.sl_mult * out["atr"]
    out["tp_long"]  = out["close"] + p.tp_mult * out["atr"]
    out["tp_short"] = out["close"] - p.tp_mult * out["atr"]

    return out


# ─── Bot integration wrapper ──────────────────────────────────────────────────

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


class MR03FBSStrategy(BaseStrategy):
    """MR-03 v5 False Breakout Snap — BB(20, 1.75) false breakout + SMA200 direction filter."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "mr_03_fbs"
        self.params                  = MR03Params()
        self.stop_loss_pct           = 2.0
        self.take_profit_pct         = 1.5
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.candle_limit            = 250
        self.reviewer_exempt         = True
        self.ml_exempt               = True
        self.time_stop_profile       = "strategy_defined"

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        MIN_BARS = p.ma_len + p.bb_len + 10
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

            # ── Sentiment gate (v5.1) ──────────────────────────────────────
            # Block shorts during BULLISH sessions and longs during BEARISH.
            # Live data showed 16.7% WR when shorting into confirmed bull
            # sessions (2026-05-25 case study: -$127 in a single BULLISH day).
            # NEUTRAL and UNKNOWN are permissive (no data = no block).
            sentiment = _get_market_sentiment()
            if direction == "short" and sentiment == "BULLISH":
                _log.debug(
                    "[MR03] %s SHORT blocked — morning sentiment is BULLISH "
                    "(regime gate)", symbol
                )
                return None
            if direction == "long" and sentiment == "BEARISH":
                _log.debug(
                    "[MR03] %s LONG blocked — morning sentiment is BEARISH "
                    "(regime gate)", symbol
                )
                return None

            close     = float(last["close"])
            atr       = float(last["atr"])

            sl_price = float(last["sl_long"])  if long_sig else float(last["sl_short"])
            tp_price = float(last["tp_long"])  if long_sig else float(last["tp_short"])

            sl_pct = round(abs(close - sl_price) / close * 100, 3)
            tp_pct = round(abs(tp_price - close)  / close * 100, 3)

            # Score: distance snapped = strength of the return move
            bb_lower = float(last["bb_lower"]) if not pd.isna(last["bb_lower"]) else close
            bb_upper = float(last["bb_upper"]) if not pd.isna(last["bb_upper"]) else close
            bb_width = bb_upper - bb_lower
            snap_depth = 0.0
            if long_sig and bb_width > 0:
                snap_depth = min((close - bb_lower) / (bb_width * 0.5), 0.3)
            elif short_sig and bb_width > 0:
                snap_depth = min((bb_upper - close) / (bb_width * 0.5), 0.3)
            score = round(max(0.58, min(0.88, 0.63 + snap_depth * 0.15)), 3)

            ma200 = float(last["ma200"]) if not pd.isna(last["ma200"]) else None

            # Volume ratio — 20-bar rolling mean
            vol_series = candles["volume"]
            vol_ma_val = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_val), 3) if vol_ma_val > 0 else None

            self.verbose_log(symbol, "BB snap-back", True, round(close, 4),
                             f"inside BB({'above lower' if long_sig else 'below upper'})", direction)
            self.verbose_log_score(symbol, score, 0.58)

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (
                    f"MR-03 FBS: snapped {'above lower' if long_sig else 'below upper'} BB "
                    f"(mult={p.bb_mult}) | "
                    f"{'above' if long_sig else 'below'} SMA200 | "
                    f"sentiment={sentiment}"
                    + (" [long-only mode]" if p.long_only else " [both-dirs mode]")
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":              "mr_03_fbs",
                    "entry_timeframe":            "1h",
                    "long_only_mode":             p.long_only,
                    "atr":                        round(atr, 6),
                    "bb_upper":                   round(bb_upper, 4),
                    "bb_lower":                   round(bb_lower, 4),
                    "ma200":                      round(ma200, 4) if ma200 else None,
                    "volume_ratio":               vol_ratio,
                    "market_sentiment":           sentiment,
                    "structural_stop_price":      round(sl_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"MR03FBSStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        """Time stop: primary exit after max_bars (TP/SL are emergency guards only)."""
        meta      = entry_metadata or {}
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= self.params.max_bars:
            return "mr03_time_stop"
        return None
