"""
MR-02 v2  Volatility Exhaustion Fade
=====================================
Hypothesis:
    When a candle's range spikes far above the recent ATR average (panic/FOMO
    candle) AND the very next bar fails to continue in the same direction —
    the move is exhausted. Fade it in the direction of the macro trend.

Edge:
    Volatility spikes on 1h crypto are frequently local exhaustion events.
    Trapped traders on the wrong side create the snapback fuel.

Key design decisions (v2):
    - RSI filter dropped: anti-selects on volatility spikes
    - ADX filter dropped: spike candles correlate with HIGH ADX — same issue
    - SMA200 direction filter: long only above SMA200, short only below
    - ATR spike multiplier: 1.5x (2.0x produces near-zero trades)

Backtest results (2022-2025 1h, no filter baseline):
    BTCUSDT: 61 trades | PF 1.29 | WR 55.7% | +1.19% | DD 2.92%
    ETHUSDT: 49 trades | PF 1.71 | WR 63.3% | +3.45% | DD 1.91%
    SOLUSDT: 37 trades | PF 1.20 | WR 51.4% | +1.33% | DD 4.73%

With SMA200 filter (cross-asset, 2022-2025):
    BTC: PF 3.45 | WR 62.5% | +4.84%
    ETH: PF 2.09 | WR 52.0% | +2.87%
    SOL: PF 1.47 | WR 53.8% | +2.68%
    LINK: PF 1.51 | WR 40.9% | +1.70%
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional

# ─── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class MR02Params:
    atr_len:          int   = 14
    atr_ma_len:       int   = 20
    atr_spike:        float = 1.5   # spike candle = ATR > atrMA * this multiplier
    body_ratio:       float = 0.5   # spike bar must have body >= 50% of range
    ma_len:           int   = 200   # SMA200 trend filter
    sl_mult:          float = 2.0   # stop loss ATR multiplier
    tp_mult:          float = 2.0   # take profit ATR multiplier
    max_bars:         int   = 24    # time stop (primary exit)
    # Regime filter: normalised ATR (ATR/close) must be in a tradable range.
    # Too low = dead market, no reversion fuel.
    # Too high = crisis/cascade, reversals fail.
    regime_atr_min:   float = 0.003   # 0.3% of price  — below this: skip
    regime_atr_max:   float = 0.030   # 3.0% of price  — above this: skip


# ─── Indicators ───────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean().rename("atr")


# ─── Signal generation ────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame, params: MR02Params = MR02Params()) -> pd.DataFrame:
    """
    Returns DataFrame with signal columns added.
    All logic is on confirmed bars — no lookahead.

    Signal logic (bar[-2] = spike bar, bar[-1] = current bar):
        Spike candle : ATR[-2] > ATR_MA[-2] * spike_mult AND big body
        Failed bull  : spike was bullish + current bar is red  → SHORT
        Failed bear  : spike was bearish + current bar is green → LONG
        Trend filter : LONG only above SMA200, SHORT only below
    """
    p   = params
    out = df.copy()

    out["atr"]    = _atr(out, p.atr_len)
    out["atr_ma"] = out["atr"].rolling(p.atr_ma_len).mean()
    out["ma200"]  = out["close"].rolling(p.ma_len).mean()

    # Spike bar detection (bar[-2] relative to current bar[-1])
    range_b1  = out["high"].shift(1) - out["low"].shift(1)
    body_b1   = (out["close"].shift(1) - out["open"].shift(1)).abs()
    body_frac = (body_b1 / range_b1.replace(0, np.nan)).fillna(0)

    spiked     = out["atr"].shift(1) > out["atr_ma"].shift(1) * p.atr_spike
    big_bull_1 = (out["close"].shift(1) > out["open"].shift(1)) & (body_frac >= p.body_ratio)
    big_bear_1 = (out["close"].shift(1) < out["open"].shift(1)) & (body_frac >= p.body_ratio)

    # Failed continuation
    failed_bull = spiked & big_bull_1 & (out["close"] < out["open"])  # spike up → current red
    failed_bear = spiked & big_bear_1 & (out["close"] > out["open"])  # spike down → current green

    # Trend filter
    bull_bias = out["close"] > out["ma200"]
    bear_bias = out["close"] < out["ma200"]

    # Regime filter: normalised ATR must be in [regime_atr_min, regime_atr_max]
    # Kills dead-market noise entries and cascade-selloff knife-catches.
    norm_atr     = out["atr_ma"] / out["close"].replace(0, np.nan)
    regime_ok    = (norm_atr >= p.regime_atr_min) & (norm_atr <= p.regime_atr_max)

    out["long_signal"]  = failed_bear & bull_bias & regime_ok
    out["short_signal"] = failed_bull & bear_bias & regime_ok

    # Spike bar ATR ratio — used for score (spike bar = shift(1), not current)
    out["atr_spike_ratio"] = out["atr"].shift(1) / out["atr_ma"].shift(1).replace(0, np.nan)

    # Stop / TP levels
    out["sl_long"]  = out["low"].shift(1)  - p.sl_mult * out["atr"]
    out["sl_short"] = out["high"].shift(1) + p.sl_mult * out["atr"]
    out["tp_long"]  = out["close"] + p.tp_mult * out["atr"]
    out["tp_short"] = out["close"] - p.tp_mult * out["atr"]

    return out


# ─── Bot integration wrapper ──────────────────────────────────────────────────

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


class MR02VEFStrategy(BaseStrategy):
    """MR-02 v2 Volatility Exhaustion Fade — ATR spike + failed continuation + SMA200 filter."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "mr_02_vef"
        self.params                  = MR02Params()
        self.stop_loss_pct           = 2.0
        self.take_profit_pct         = 2.0
        self.crypto_enabled          = True
        self.stock_enabled           = False      # crypto mean reversion — not applicable to stocks
        self.crypto_candle_timeframe = "1h"
        self.candle_limit            = 250        # SMA200 needs 200+ bars
        self.reviewer_exempt         = True
        self.ml_exempt               = True
        self.time_stop_profile       = "strategy_defined"
        self.enabled                 = True

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        MIN_BARS = p.ma_len + p.atr_ma_len + 10
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
            close     = float(last["close"])
            atr       = float(last["atr"])

            sl_price = float(last["sl_long"])  if long_sig else float(last["sl_short"])
            tp_price = float(last["tp_long"])  if long_sig else float(last["tp_short"])

            sl_pct = round(abs(close - sl_price) / close * 100, 3)
            tp_pct = round(abs(tp_price - close)  / close * 100, 3)

            # Score: stronger spike on the SPIKE BAR (shift(1)) = stronger signal
            # Use the atr_spike_ratio column which captured shift(1) ATR / shift(1) ATR_MA
            spike_ratio    = float(last["atr_spike_ratio"]) if not pd.isna(last["atr_spike_ratio"]) else p.atr_spike
            atr_ma         = float(last["atr_ma"]) if not pd.isna(last["atr_ma"]) else atr
            spike_strength = min((spike_ratio - p.atr_spike) / p.atr_spike, 0.3)
            score          = round(max(0.58, min(0.88, 0.65 + spike_strength)), 3)

            ma200 = float(last["ma200"]) if not pd.isna(last["ma200"]) else None

            norm_atr_now = atr_ma / float(last["close"]) if float(last["close"]) > 0 else 0

            # volume_ratio: current bar volume vs 20-bar average
            vol_series = candles["volume"]
            vol_ma_now = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_now), 3) if vol_ma_now and vol_ma_now > 0 else None

            self.verbose_log(symbol, "ATR spike (spike bar)", True, round(spike_ratio, 2),
                             f">{p.atr_spike}x ATR_MA", direction)
            self.verbose_log(symbol, "Regime ATR", True,
                             round(norm_atr_now * 100, 3),
                             f"[{p.regime_atr_min*100:.1f}%–{p.regime_atr_max*100:.1f}%]",
                             direction)
            self.verbose_log_score(symbol, score, 0.58)

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (
                    f"MR-02 VEF: ATR spike {spike_ratio:.2f}x MA | "
                    f"failed {'bull' if short_sig else 'bear'} continuation | "
                    f"{'above' if long_sig else 'below'} SMA200"
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":               "mr_02_vef",
                    "entry_timeframe":             "1h",
                    "atr":                         round(atr, 6),
                    "atr_spike_ratio":             round(spike_ratio, 3),
                    "atr_norm_pct":                round(norm_atr_now * 100, 3),
                    "volume_ratio":                vol_ratio,
                    "ma200":                       round(ma200, 4) if ma200 else None,
                    "structural_stop_price":       round(sl_price, 6),
                    "structural_tp_price":         round(tp_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"MR02VEFStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(
        self,
        symbol: str,
        bars: pd.DataFrame,
        direction: str,
        entry_metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Time stop: exit after max_bars regardless of P&L.

        Signature matches BaseStrategy.check_custom_exit and the backtester
        call: (symbol, bars_window, direction, entry_metadata).
        _bars_held is injected into entry_metadata by the backtester.
        """
        bars_held = int((entry_metadata or {}).get("_bars_held", 0))
        if bars_held >= self.params.max_bars:
            return "mr02_time_stop"
        return None
