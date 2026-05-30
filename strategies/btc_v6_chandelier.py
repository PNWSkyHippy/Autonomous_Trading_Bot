"""
BTC V6 Chandelier
=================
Classification : INCUBATE
Source         : Pine Script "BTC V6 F40d+F41 — Webhook" (TraderDev)

Core logic
----------
  Entry : RSI(14) < 43  AND  EMA(21) > EMA(55)  AND  ADX >= 20  AND  flat
  Exit  : Chandelier trailing stop = rolling_high_since_entry − ATR(14) × 5.0
           The chandelier ratchets up with price, never down.
           No fixed TP — ride the trend until price violates the chandelier.
  Side  : Long only (trend-following, crypto primary)

Original Pine also had complex dynamic sizing (vol mult × ADX mult × win-streak
mult × martingale). Here those are converted into the signal score rather than
altering position size directly.

Pine parameters translated
--------------------------
  growthRate  = 1.5   (streak mult growth on win)
  decayRate   = 0.5   (streak mult decay on loss)
  martMultMax = 1.0   (martingale factor — 1.0 = disabled)
  atrMult     = 5.0   (chandelier ATR multiple)
  rsiThresh   = 43    (RSI entry threshold)
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional


# ── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class BTCV6ChandelierParams:
    rsi_length:   int   = 14
    rsi_thresh:   float = 45.0    # enter when RSI < this — tuned 2026-05-25 (was 43.0)
    ema_fast:     int   = 21
    ema_slow:     int   = 55
    adx_min:      float = 20.0
    atr_length:   int   = 14
    atr_mult:     float = 4.0     # chandelier distance — tuned 2026-05-25 v3 matrix (was 5.5)
    hard_sl_pct:  float = 9.0     # absolute fallback stop if chandelier not established


# ── Indicators ────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift()).abs()
    lpc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(span=length, adjust=False).mean()


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


def _adx(df: pd.DataFrame, length: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    hl  = high - low
    hpc = (high - close.shift()).abs()
    lpc = (low  - close.shift()).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    up   = high.diff()
    down = -low.diff()
    plus_dm  = pd.Series(np.where((up > down) & (up > 0),   up,   0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    alpha    = 1.0 / length
    atr_w    = tr.ewm(alpha=alpha, adjust=False).mean()
    safe_atr = atr_w.replace(0, np.nan)
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / safe_atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / safe_atr
    di_sum   = (plus_di + minus_di).replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0).rename("adx")


# ── Signal generation ─────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame,
                     params: BTCV6ChandelierParams = BTCV6ChandelierParams()) -> pd.DataFrame:
    p   = params
    out = df.copy()
    c   = out["close"]

    out["rsi"]     = _rsi(c, p.rsi_length)
    out["adx"]     = _adx(out, p.atr_length)
    out["atr"]     = _atr(out, p.atr_length)
    out["ema_fast"] = _ema(c, p.ema_fast)
    out["ema_slow"] = _ema(c, p.ema_slow)
    out["trend_up"] = out["ema_fast"] > out["ema_slow"]

    # Vol multiplier (Pine: volRatio clamped 1.0–1.5)
    out["norm_atr"] = out["atr"] / c.replace(0, np.nan)
    out["ref_atr"]  = out["norm_atr"].rolling(200).mean()
    out["vol_mult"] = (out["norm_atr"] / out["ref_atr"].replace(0, np.nan)).clip(1.0, 1.5)

    # Signal multiplier from ADX (Pine: 1.0 + (ADX-20)/30 clamped 1.0–2.0)
    out["sig_mult"] = (1.0 + (out["adx"] - 20.0) / 30.0).clip(1.0, 2.0)

    out["long_signal"] = (
        (out["rsi"] < p.rsi_thresh) &
        out["trend_up"] &
        (out["adx"] >= p.adx_min)
    )

    return out


# ── Bot integration ───────────────────────────────────────────────────────────

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


class BTCV6ChandelierStrategy(BaseStrategy):
    """
    RSI dip + EMA21>EMA55 trend + ADX momentum → Chandelier trailing stop.
    Long only. Rides trend until price violates rolling_high - ATR×5.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name           = "btc_v6_chandelier"
        self.params                  = BTCV6ChandelierParams()
        self.stop_loss_pct           = self.params.hard_sl_pct
        self.take_profit_pct         = 30.0   # wide — chandelier exits before TP normally
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.time_stop_profile       = "strategy_defined"
        self.reviewer_exempt         = True
        self.candle_limit            = 250    # 200-bar vol ref + EMA55 warmup + buffer

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        if not self._check_enough_candles(symbol, candles, 230):
            return None
        try:
            sig  = generate_signals(candles, p)
            last = sig.iloc[-1]
            if not bool(last["long_signal"]):
                return None

            close   = float(last["close"])
            atr     = float(last["atr"])
            rsi     = float(last["rsi"])
            adx     = float(last["adx"])
            vol_m   = float(last["vol_mult"]) if not pd.isna(last["vol_mult"]) else 1.0
            sig_m   = float(last["sig_mult"])

            chan_init = close - atr * p.atr_mult
            sl_pct    = max(round((close - chan_init) / close * 100, 3), 1.5)
            score     = round(max(0.66, min(0.88,
                            0.66 + (vol_m - 1.0) * 0.15 + (sig_m - 1.0) * 0.10)), 3)

            vol_series = candles["volume"]
            vol_ma_val = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_val), 3) if vol_ma_val > 0 else None

            return self._make_signal(
                symbol          = symbol,
                direction       = "long",
                score           = score,
                reason          = (f"BTCV6Chan: RSI={rsi:.1f} ADX={adx:.1f} "
                                   f"volM={vol_m:.2f} sigM={sig_m:.2f}"),
                stop_loss_pct   = sl_pct,
                take_profit_pct = self.take_profit_pct,
                metadata={
                    "strategy_name":               "btc_v6_chandelier",
                    "rsi":                         round(rsi, 2),
                    "adx":                         round(adx, 2),
                    "atr":                         round(atr, 6),
                    "vol_mult":                    round(vol_m, 3),
                    "sig_mult":                    round(sig_m, 3),
                    "chandelier_init":             round(chan_init, 6),
                    "entry_atr":                   round(atr, 6),
                    "atr_mult":                    p.atr_mult,
                    "volume_ratio":                vol_ratio,
                    "structural_stop_price":       round(chan_init, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "chandelier",
                },
            )
        except Exception as e:
            self.logger.error(f"BTCV6ChandelierStrategy error on {symbol}: {e}", exc_info=True)
            return None

    def _precompute(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        return generate_signals(df, self.params)

    def _analyze_from_precomputed(self, symbol: str, i: int,
                                  sigs: pd.DataFrame, df: pd.DataFrame) -> Optional[TradeSignal]:
        p   = self.params
        row = sigs.iloc[i]
        if not bool(row["long_signal"]) or pd.isna(row["atr"]):
            return None

        close = float(row["close"])
        atr   = float(row["atr"])
        rsi   = float(row["rsi"])
        adx   = float(row["adx"])
        vol_m = float(row["vol_mult"]) if not pd.isna(row["vol_mult"]) else 1.0
        sig_m = float(row["sig_mult"])

        chan_init = close - atr * p.atr_mult
        sl_pct   = max(round((close - chan_init) / close * 100, 3), 1.5)
        score    = round(max(0.66, min(0.88,
                       0.66 + (vol_m - 1.0) * 0.15 + (sig_m - 1.0) * 0.10)), 3)

        vol_series = df["volume"]
        start_v    = max(0, i - 19)
        vol_ma_val = vol_series.iloc[start_v:i].mean() if i > start_v else None
        vol_ratio  = (round(float(vol_series.iloc[i] / vol_ma_val), 3)
                      if vol_ma_val and vol_ma_val > 0 else None)

        return self._make_signal(
            symbol          = symbol,
            direction       = "long",
            score           = score,
            reason          = f"BTCV6Chan: RSI={rsi:.1f} ADX={adx:.1f}",
            stop_loss_pct   = sl_pct,
            take_profit_pct = self.take_profit_pct,
            metadata={
                "strategy_name":               "btc_v6_chandelier",
                "rsi":                         round(rsi, 2),
                "adx":                         round(adx, 2),
                "atr":                         round(atr, 6),
                "vol_mult":                    round(vol_m, 3),
                "sig_mult":                    round(sig_m, 3),
                "chandelier_init":             round(chan_init, 6),
                "entry_atr":                   round(atr, 6),
                "atr_mult":                    p.atr_mult,
                "volume_ratio":                vol_ratio,
                "structural_stop_price":       round(chan_init, 6),
                "preferred_initial_stop_mode": "signal_structural",
                "preferred_trail_mode":        "none",
            },
        )

    def _exit_from_precomputed(self, i: int, sigs: pd.DataFrame,
                               direction: str, meta: dict) -> Optional[str]:
        if direction != "long":
            return None
        p         = self.params
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held < 1:
            return None

        entry_i     = max(0, i - bars_held)
        track_high  = float(sigs["high"].iloc[entry_i:i + 1].max())
        current_atr = float(sigs["atr"].iloc[i])
        chandelier  = track_high - current_atr * p.atr_mult
        cur_close   = float(sigs["close"].iloc[i])

        if cur_close <= chandelier:
            return "chandelier_stop"
        return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        if bars is None or len(bars) < 2 or direction != "long":
            return None
        p         = self.params
        meta      = entry_metadata or {}
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held < 1:
            return None

        since      = bars.iloc[-bars_held:] if bars_held <= len(bars) else bars
        track_high = float(since["high"].max())
        cur_atr    = float(meta.get("entry_atr", float(bars["close"].iloc[-1]) * 0.02))
        chandelier = track_high - cur_atr * p.atr_mult
        cur_close  = float(bars["close"].iloc[-1])

        if cur_close <= chandelier:
            return "chandelier_stop"
        return None