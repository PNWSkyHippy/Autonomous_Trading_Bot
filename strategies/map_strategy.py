"""
MAP — Multi-Period Alignment
==============================
Classification : CANDIDATE
Timeframe      : 4h crypto
Symbol         : BTC/USD only (validated; ETH watchlist — not deployed)

Hypothesis:
    When 3 consecutive return windows (4 / 8 / 16 bars) ALL exceed a minimum
    magnitude threshold in the SAME direction — and this alignment is NEW (did
    not exist on the previous bar) — it marks the beginning of multi-scale
    momentum initiation rather than a late entry into an existing move.
    ADX ≥ 20 blocks choppy regimes where this is pure noise.

Mathematical edge:
    Multi-timeframe return alignment is a proxy for autocorrelation burst.
    In trending crypto, positive autocorrelation is strongest at the ONSET
    of a new aligned phase — the "first bar of alignment" criterion targets
    that onset.  Minimum return magnitudes (not just sign) filter weak
    alignments, keeping only confirmed momentum moves.

Backtest results (Trader Dev, BTC/USD 4h, 2024-01-01 → 2026-05-26):
    PF    : 1.54   (target ≥ 1.4 ✓)
    WR    : 44.4%  (target ≥ 40% ✓)
    Trades: 126    (target ≥ 100 ✓)
    Max DD: 3.0%   ✓
    Sharpe: 1.38
    Longs : +$599  (64 trades, 45.3% WR)
    Shorts: +$503  (62 trades, 43.5% WR)
    Commission paid: $126 (~1.3% of capital over 2.4 years)

Timeframe sensitivity (same parameters):
    4h  → PF 1.54  WR 44%  126 trades  ← LIVE
    1h  → PF 1.17  WR 38%  511 trades  (commission drag kills edge)
    15m → PF 1.00  WR 34%  818 trades  (dead — pure noise)
    5m  → PF 0.98  WR 33% 1253 trades  (dead)

Symbol sensitivity (4h, same parameters):
    BTC  → PF 1.54  WR 44%  126 trades ← LIVE
    ETH  → PF 1.16  WR 40%  126 trades (watchlist — positive but below PF target)
    SOL  → PF 1.06  WR 34%  142 trades (reject)
    ADA  → PF 0.96  WR 33%  130 trades (reject)
    XRP  → PF 1.01  WR 34%  118 trades (reject)

Parameters (default, tuned on BTC 4h — do not change without re-running matrix):
    r1_len    = 4      bars  (16h return window)
    r2_len    = 8      bars  (32h return window)
    r3_len    = 16     bars  (64h / ~2.7-day return window)
    min_r1    = 0.003  minimum 0.3% return for R1 to qualify
    min_r2    = 0.005  minimum 0.5% return for R2 to qualify
    min_r3    = 0.008  minimum 0.8% return for R3 to qualify
    ema_len   = 50     trend direction filter
    adx_len   = 14     standard ADX period
    adx_thresh= 20     minimum ADX for trending market gate
    sl_mult   = 2.0    × ATR  (~3% on BTC 4h typical)
    tp_mult   = 4.0    × ATR  (~6% on BTC 4h typical)  →  2:1 RR
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

@dataclass
class MAPParams:
    r1_len:     int   = 4
    r2_len:     int   = 8
    r3_len:     int   = 16
    min_r1:     float = 0.003   # 0.3% minimum return
    min_r2:     float = 0.005   # 0.5% minimum return
    min_r3:     float = 0.008   # 0.8% minimum return
    ema_len:    int   = 50
    adx_len:    int   = 14
    adx_thresh: float = 20.0
    atr_len:    int   = 14
    sl_mult:    float = 2.0
    tp_mult:    float = 4.0
    long_only:  bool  = False   # both sides validated; set True to disable shorts


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean().rename("atr")


def _adx(df: pd.DataFrame, length: int) -> pd.Series:
    """Wilder-smoothed ADX — matches TradingView / Pine Script ADX."""
    high, low, close = df["high"], df["low"], df["close"]
    up   = high - high.shift(1)
    down = low.shift(1) - low

    plus_dm  = np.where((up > down) & (up > 0),   up.values,   0.0)
    minus_dm = np.where((down > up) & (down > 0), down.values, 0.0)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    alpha    = 1.0 / length
    sm_tr    = tr.ewm(alpha=alpha, adjust=False).mean()
    sm_plus  = pd.Series(plus_dm,  index=df.index).ewm(alpha=alpha, adjust=False).mean()
    sm_minus = pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean()

    plus_di  = 100 * sm_plus  / sm_tr.replace(0, np.nan)
    minus_di = 100 * sm_minus / sm_tr.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=alpha, adjust=False).mean().rename("adx")


# ---------------------------------------------------------------------------
# Signal generation  (no lookahead)
# ---------------------------------------------------------------------------

def generate_signals(df: pd.DataFrame, params: MAPParams = MAPParams()) -> pd.DataFrame:
    """
    Adds signal columns to df.  All calculations on confirmed closed bars only.

    Added columns:
        long_signal, short_signal  : bool
        sl_long, sl_short          : stop loss price
        tp_long, tp_short          : take profit price
        adx, ema50, atr            : indicator values for metadata
    """
    p   = params
    out = df.copy()

    # Return for each window
    for n, col in [(p.r1_len, "ret1"), (p.r2_len, "ret2"), (p.r3_len, "ret3")]:
        prev     = out["close"].shift(n)
        out[col] = (out["close"] - prev) / prev.replace(0, np.nan)

    # Aligned when ALL windows exceed minimum magnitude in same direction
    bull_align = (
        (out["ret1"] >  p.min_r1) &
        (out["ret2"] >  p.min_r2) &
        (out["ret3"] >  p.min_r3)
    )
    bear_align = (
        (out["ret1"] < -p.min_r1) &
        (out["ret2"] < -p.min_r2) &
        (out["ret3"] < -p.min_r3)
    )

    # Fire ONLY on first bar of new alignment (momentum initiation, not continuation)
    new_bull = bull_align & ~bull_align.shift(1, fill_value=False)
    new_bear = bear_align & ~bear_align.shift(1, fill_value=False)

    # Indicators
    out["ema50"] = out["close"].ewm(span=p.ema_len, adjust=False).mean()
    out["adx"]   = _adx(out, p.adx_len)
    out["atr"]   = _atr(out, p.atr_len)

    bull_trend  = out["close"] > out["ema50"]
    bear_trend  = out["close"] < out["ema50"]
    is_trending = out["adx"] >= p.adx_thresh

    out["long_signal"]  = new_bull & bull_trend & is_trending
    out["short_signal"] = (
        pd.Series(False, index=out.index)
        if p.long_only
        else new_bear & bear_trend & is_trending
    )

    out["sl_long"]  = out["close"] - p.sl_mult * out["atr"]
    out["sl_short"] = out["close"] + p.sl_mult * out["atr"]
    out["tp_long"]  = out["close"] + p.tp_mult * out["atr"]
    out["tp_short"] = out["close"] - p.tp_mult * out["atr"]

    return out


# ---------------------------------------------------------------------------
# Bot integration wrapper
# ---------------------------------------------------------------------------

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


# BTC-only — validated symbols only; ETH watchlist, not deployed
_ALLOWED_SYMBOLS = ["BTC/USD", "BTC-USD", "BTCUSD", "BTCUSDT", "BTC/USDT"]


class MAPStrategy(BaseStrategy):
    """
    MAP — Multi-Period Alignment (4h BTC crypto trend strategy).

    Candidate — meets all live criteria on BTC/USD 4h:
        PF 1.54 | WR 44.4% | 126 trades | DD 3.0% | Sharpe 1.38
    Both longs (+$599) and shorts (+$503) are profitable.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name           = "map_strategy"
        self.params                  = MAPParams()
        self.stop_loss_pct           = 3.0    # ~2 × ATR on BTC 4h
        self.take_profit_pct         = 6.0    # ~4 × ATR on BTC 4h
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "4h"
        self.candle_limit            = 120    # r3(16) + ema50(50) + adx warmup + buffer
        self.reviewer_exempt         = True
        self.ml_exempt               = True
        self.time_stop_profile       = "none"

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:

        # BTC-only guard — strategy validated on BTC only
        if not self._passes_symbol_whitelist(symbol, _ALLOWED_SYMBOLS, "MAP allowed_symbols"):
            return None

        p = self.params
        MIN_BARS = p.r3_len + p.ema_len + 20
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

            close    = float(last["close"])
            atr      = float(last["atr"])
            ema50    = float(last["ema50"]) if not pd.isna(last["ema50"]) else None
            adx_val  = float(last["adx"])   if not pd.isna(last["adx"])  else None
            ret1_pct = float(last["ret1"]) * 100
            ret2_pct = float(last["ret2"]) * 100
            ret3_pct = float(last["ret3"]) * 100

            sl_price = float(last["sl_long"])  if long_sig else float(last["sl_short"])
            tp_price = float(last["tp_long"])  if long_sig else float(last["tp_short"])

            sl_pct = round(abs(close - sl_price) / close * 100, 3)
            tp_pct = round(abs(tp_price - close)  / close * 100, 3)

            # Score: base + ADX bonus (higher ADX = more trending = higher confidence)
            adx_bonus = min(0.15, max(0.0, (adx_val - 20) / 100)) if adx_val else 0.0
            score     = round(min(0.88, 0.65 + adx_bonus), 3)

            self.verbose_log(symbol, "MAP alignment", True, round(close, 4),
                             f"R1={ret1_pct:.2f}% R2={ret2_pct:.2f}% R3={ret3_pct:.2f}%",
                             direction)
            self.verbose_log_score(symbol, score, 0.65)

            reason = (
                f"MAP {'bull' if long_sig else 'bear'} alignment: "
                f"R1={ret1_pct:.2f}% R2={ret2_pct:.2f}% R3={ret3_pct:.2f}% | "
                f"ADX={adx_val:.1f} | {'above' if long_sig else 'below'} EMA50"
            )

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = reason,
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":               "map_strategy",
                    "entry_timeframe":             "4h",
                    "r1_return_pct":               round(ret1_pct, 4),
                    "r2_return_pct":               round(ret2_pct, 4),
                    "r3_return_pct":               round(ret3_pct, 4),
                    "adx":                         round(adx_val, 2) if adx_val else None,
                    "ema50":                       round(ema50, 4)   if ema50   else None,
                    "atr":                         round(atr, 6),
                    "structural_stop_price":       round(sl_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )

        except Exception as e:
            self.logger.error(f"MAPStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None
