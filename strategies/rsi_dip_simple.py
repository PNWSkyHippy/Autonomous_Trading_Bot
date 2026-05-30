"""
RSI Dip Simple
==============
Classification : INCUBATE
Source         : Pine Script "BTCUSDT RSI Dip & Spike — 1h Bybit (v2 tuned)"
                 from TraderDev optimizer sweep (best Sortino, 4yr BTC 1h)

Core logic
----------
  Entry : RSI(7) crosses BELOW 35  →  long
  Exit  : RSI(7) crosses ABOVE 80  →  close long
  Stops : ATR-based hard stop (not in original Pine — added for risk management)
          Original Pine had no stop loss at all (100% equity, RSI exit only)

Difference from rsi_dip_spike_v4
---------------------------------
  v4 adds: SMA(200) trend filter + ADX >= 25 gate + ATR TP/SL
  This version: naked RSI cross only — no trend filter, no ADX
  Lower signal quality per trade but fires more often and on more symbols.
  Worth testing to see if the simpler version generalises better across crypto.

Pine parameters
---------------
  rsiLength  = 7
  oversold   = 35   (original Pine)
              → tuned to 25 in v3 matrix sweep (2026-05-25) — see RSIDipSimpleParams
  overbought = 80
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional


# ── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class RSIDipSimpleParams:
    rsi_length:   int   = 7
    rsi_oversold: float = 16.0   # tuned 2026-05-25 v3 matrix (was 26.0)
    rsi_exit:     float = 85.33   # tuned 2026-05-25 v3 matrix (was 82.5) — exit earlier on crypto
    atr_length:   int   = 14
    sl_atr_mult:  float = 1.25    # hard stop — added vs original Pine
    max_bars_hold: int  = 72     # 3 days on 1h — safety net


# ── Indicators ────────────────────────────────────────────────────────────────

def _rsi(close: pd.Series, length: int) -> pd.Series:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    alpha    = 1.0 / length
    avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    rsi      = 100 - 100 / (1 + rs)
    loss_zero = avg_loss <= 0
    gain_zero = avg_gain <= 0
    rsi = rsi.where(~(loss_zero &  gain_zero), other=50.0)
    rsi = rsi.where(~(loss_zero & ~gain_zero), other=100.0)
    return rsi.rename("rsi")


def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


# ── Signal generation ─────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame,
                     params: RSIDipSimpleParams = RSIDipSimpleParams()) -> pd.DataFrame:
    p   = params
    out = df.copy()
    c   = out["close"]

    out["rsi"] = _rsi(c, p.rsi_length)
    out["atr"] = _atr(out, p.atr_length)

    rsi_prev = out["rsi"].shift(1)

    # Cross under oversold threshold → long
    out["long_signal"] = (rsi_prev >= p.rsi_oversold) & (out["rsi"] < p.rsi_oversold)

    out["sl_long"] = c - out["atr"] * p.sl_atr_mult

    return out


# ── Bot integration ───────────────────────────────────────────────────────────

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


class RSIDipSimpleStrategy(BaseStrategy):
    """
    Naked RSI(7) dip entry — no trend/ADX filter. Long only.
    Exit: RSI crosses above 80 OR ATR hard stop OR max bars.
    Simpler than v4 — tests whether the unfiltered signal generalises.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name           = "rsi_dip_simple"
        self.params                  = RSIDipSimpleParams()
        self.stop_loss_pct           = 3.0   # approximate; ATR-based preferred
        self.take_profit_pct         = 8.0   # wide — RSI exit handles closes normally
        self.crypto_enabled          = True
        self.stock_enabled           = False  # crypto-tuned RSI levels
        self.crypto_candle_timeframe = "1h"
        self.time_stop_profile       = "strategy_defined"
        self.reviewer_exempt         = True   # single RSI entry signal, no complex confluence to review
        self.candle_limit            = 80     # bumped from 50: RSI(7)+ATR(14) need clean warmup headroom

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        if not self._check_enough_candles(symbol, candles, 30):
            return None
        try:
            sig  = generate_signals(candles, p)
            last = sig.iloc[-1]
            if not bool(last["long_signal"]):
                return None

            close = float(last["close"])
            rsi   = float(last["rsi"])
            atr   = float(last["atr"])
            sl    = float(last["sl_long"])
            sl_pct = max(round((close - sl) / close * 100, 3), 1.0)

            # Simple score: deeper dip = stronger signal
            depth = max(0.0, (p.rsi_oversold - rsi) / p.rsi_oversold)
            score = round(max(0.55, min(0.82, 0.58 + depth * 0.20)), 3)

            # Volume ratio — 20-bar rolling mean
            vol_series = candles["volume"]
            vol_ma_val = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_val), 3) if vol_ma_val > 0 else None

            return self._make_signal(
                symbol          = symbol,
                direction       = "long",
                score           = score,
                reason          = f"RSIDipSimple: RSI={rsi:.1f} (crossed below {p.rsi_oversold})",
                stop_loss_pct   = sl_pct,
                take_profit_pct = self.take_profit_pct,
                metadata={
                    "strategy_name":               "rsi_dip_simple",
                    "rsi":                         round(rsi, 2),
                    "atr":                         round(atr, 6),
                    "volume_ratio":                vol_ratio,
                    "structural_stop_price":       round(sl, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"RSIDipSimpleStrategy error on {symbol}: {e}", exc_info=True)
            return None

    def _precompute(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        Pre-compute RSI + ATR signals on the FULL df before the bar loop.
        Called once by backtester._simulate(); result stored as _all_sigs.
        Eliminates O(N²) indicator recomputation. NOT dead code.
        """
        return generate_signals(df, self.params)

    def _analyze_from_precomputed(self, symbol: str, i: int,
                                  sigs: pd.DataFrame, df: pd.DataFrame) -> Optional[TradeSignal]:
        """
        O(1) signal lookup — replaces per-bar analyze() call inside _simulate.
        Uses precomputed signals df instead of re-running generate_signals(). NOT dead code.
        """
        p   = self.params
        row = sigs.iloc[i]
        if not bool(row["long_signal"]) or pd.isna(row["rsi"]):
            return None

        close  = float(row["close"])
        rsi    = float(row["rsi"])
        atr    = float(row["atr"])
        sl     = float(row["sl_long"])
        sl_pct = max(round((close - sl) / close * 100, 3), 1.0)
        depth  = max(0.0, (p.rsi_oversold - rsi) / p.rsi_oversold)
        score  = round(max(0.55, min(0.82, 0.58 + depth * 0.20)), 3)

        # Volume ratio — 20-bar rolling mean from precomputed df
        vol_series = df["volume"]
        start_v    = max(0, i - 19)
        vol_ma_val = vol_series.iloc[start_v:i].mean() if i > start_v else None
        vol_ratio  = (round(float(vol_series.iloc[i] / vol_ma_val), 3)
                      if vol_ma_val and vol_ma_val > 0 else None)

        return self._make_signal(
            symbol          = symbol,
            direction       = "long",
            score           = score,
            reason          = f"RSIDipSimple: RSI={rsi:.1f}",
            stop_loss_pct   = sl_pct,
            take_profit_pct = self.take_profit_pct,
            metadata={
                "strategy_name":               "rsi_dip_simple",
                "rsi":                         round(rsi, 2),
                "atr":                         round(atr, 6),
                "volume_ratio":                vol_ratio,
                "structural_stop_price":       round(sl, 6),
                "preferred_initial_stop_mode": "signal_structural",
                "preferred_trail_mode":        "none",
            },
        )

    def _exit_from_precomputed(self, i: int, sigs: pd.DataFrame,
                               direction: str, meta: dict) -> Optional[str]:
        """
        O(1) exit check — replaces per-bar check_custom_exit() call inside _simulate.
        Reads RSI directly from precomputed signals instead of recomputing it. NOT dead code.
        """
        p = self.params
        try:
            rsi_now = float(sigs.iloc[i]["rsi"])
            if not pd.isna(rsi_now) and direction == "long" and rsi_now >= p.rsi_exit:
                return "rsi_overbought_exit"
        except Exception:
            pass

        if int(meta.get("_bars_held", 0)) >= p.max_bars_hold:
            return "rsi_simple_max_hold"
        return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        if bars is None or len(bars) < 2 or direction != "long":
            return None
        p    = self.params
        meta = entry_metadata or {}

        try:
            rsi_series = _rsi(bars["close"], p.rsi_length)
            if float(rsi_series.iloc[-1]) >= p.rsi_exit:
                return "rsi_overbought_exit"
        except Exception:
            pass

        if int(meta.get("_bars_held", 0)) >= p.max_bars_hold:
            return "rsi_simple_max_hold"
        return None
