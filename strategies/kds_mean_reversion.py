"""
KDS Mean Reversion
==================
Classification : INCUBATE
Author         : Quant Engineering Session 2026-05-24

Core Concept
------------
The Keltner Deviation Score (KDS) measures how many ATRs price has moved
away from its EMA. This is a volatility-normalised distance metric —
mathematically cleaner than standard Bollinger Bands because ATR is robust
to vol-regime shifts that inflate/deflate rolling std.

When KDS is extreme (price >2 ATRs from EMA), RSI confirms the over-extension,
AND ADX confirms the market is ranging (not trending), mean reversion is
statistically likely.

Entry Logic
-----------
  Long  : KDS < -kds_thresh  AND  RSI < rsi_os  AND  ADX < adx_max
  Short : KDS >  kds_thresh  AND  RSI > rsi_ob  AND  ADX < adx_max

Exit Logic
----------
  TP : EMA (mean reversion target — price returning to the mean)
  SL : Entry ± sl_mult × ATR (hard stop behind the extreme)

The TP/SL ratio is naturally asymmetric: TP is the EMA distance away
(which equals kds_thresh × ATR at entry), SL is sl_mult × ATR. With
kds_thresh=2.0 and sl_mult=1.5, that's a ~1.33:1 RR minimum.

Parameters
----------
  ema_length  = 20       EMA lookback
  atr_length  = 14       ATR lookback (Wilder smoothing)
  rsi_length  = 7        RSI lookback (shorter = more sensitive)
  kds_thresh  = 2.0      Min ATR-distance from EMA for entry
  rsi_os      = 35       RSI oversold threshold (long entry)
  rsi_ob      = 65       RSI overbought threshold (short entry)
  adx_max     = 28       Max ADX — above this = trending, skip
  adx_length  = 14       ADX lookback
  sl_mult     = 1.5      Stop loss ATR multiple
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional


# ── Parameters ───────────────────────────────────────────────────────────────

@dataclass
class KDSMeanReversionParams:
    ema_length: int   = 20
    atr_length: int   = 14
    rsi_length: int   = 7
    kds_thresh: float = 2.0    # ATRs from EMA to qualify as over-extended
    rsi_os:     float = 35.0   # oversold — long entry
    rsi_ob:     float = 65.0   # overbought — short entry
    adx_max:    float = 28.0   # regime gate: market must be ranging
    adx_length: int   = 14
    sl_mult:    float = 1.5    # stop = entry ± sl_mult × ATR
    max_bars:   int   = 72     # 3 days on 1d — safety net if mean reversion is slow


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
    hl   = high - low
    hpc  = (high - close.shift()).abs()
    lpc  = (low  - close.shift()).abs()
    tr   = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
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


# ── Signal Generation ─────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame,
                     params: KDSMeanReversionParams = KDSMeanReversionParams()) -> pd.DataFrame:
    p   = params
    out = df.copy()
    c   = out["close"]

    out["ema"]  = _ema(c, p.ema_length)
    out["atr"]  = _atr(out, p.atr_length)
    out["rsi"]  = _rsi(c, p.rsi_length)
    out["adx"]  = _adx(out, p.adx_length)

    # KDS: how many ATRs is price from the EMA
    out["kds"]  = (c - out["ema"]) / out["atr"].replace(0, np.nan)

    # Regime: only trade when market is ranging (ADX below threshold)
    out["ranging"] = out["adx"] < p.adx_max

    out["long_signal"]  = (
        out["ranging"] &
        (out["kds"] < -p.kds_thresh) &
        (out["rsi"] < p.rsi_os)
    )
    out["short_signal"] = (
        out["ranging"] &
        (out["kds"] > p.kds_thresh) &
        (out["rsi"] > p.rsi_ob)
    )

    return out


# ── Bot Integration ───────────────────────────────────────────────────────────

try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object
    TradeSignal  = None


class KDSMeanReversionStrategy(BaseStrategy):
    """
    ATR-normalised mean reversion. Enters when price is >2 ATRs from EMA
    in a ranging market. TP = EMA (mean), SL = 1.5×ATR behind entry.
    Works long and short. Suitable for crypto 1h and stocks 1h/1d.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name           = "kds_mean_reversion"
        self.params                  = KDSMeanReversionParams()
        self.stop_loss_pct           = 3.0
        self.take_profit_pct         = 4.0   # approximate — actual exit is EMA-based via check_custom_exit
        self.crypto_enabled          = True
        self.stock_enabled           = True
        self.crypto_candle_timeframe = "1d"  # daily bars — KDS/ATR-normalised at daily granularity
        self.time_stop_profile       = "strategy_defined"
        self.reviewer_exempt         = True
        self.ml_exempt               = True  # daily low-frequency signal; ML blending adds noise
        self.candle_limit            = 120

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        if not self._check_enough_candles(symbol, candles, 60):
            return None
        try:
            sig  = generate_signals(candles, p)
            last = sig.iloc[-1]

            long_sig  = bool(last["long_signal"])
            short_sig = bool(last["short_signal"])
            if not long_sig and not short_sig:
                return None

            close = float(last["close"])
            atr   = float(last["atr"])
            rsi   = float(last["rsi"])
            adx   = float(last["adx"])
            kds   = float(last["kds"])
            ema   = float(last["ema"])

            direction = "long" if long_sig else "short"

            # SL: ATR-based absolute stop (was erroneously computing kds*0.75*100 ≈ 150%)
            sl_price = (close - atr * p.sl_mult if direction == "long"
                        else close + atr * p.sl_mult)
            sl_pct   = round(abs(close - sl_price) / close * 100, 3)
            # TP: EMA distance at entry — wide backstop; real exit is EMA-based via check_custom_exit
            tp_pct   = round(max(abs(close - ema) / close * 100, 2.0), 3)

            # Score: higher when deeper over-extension + stronger ranging signal
            ranging_score = max(0.0, min(1.0, (p.adx_max - adx) / p.adx_max))
            ext_score     = min(1.0, (abs(kds) - p.kds_thresh) / 2.0)
            score         = round(0.60 + ranging_score * 0.15 + ext_score * 0.15, 3)
            score         = max(0.60, min(0.85, score))

            # Volume ratio — 20-bar rolling mean
            vol_series = candles["volume"]
            vol_ma_val = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma_val), 3) if vol_ma_val > 0 else None

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (f"KDS={kds:.2f} RSI={rsi:.1f} ADX={adx:.1f} "
                                   f"EMA={ema:.4f}"),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    "strategy_name":               "kds_mean_reversion",
                    "kds":                         round(kds, 3),
                    "rsi":                         round(rsi, 2),
                    "adx":                         round(adx, 2),
                    "atr":                         round(atr, 6),
                    "ema":                         round(ema, 6),
                    "atr_mult_used":               p.kds_thresh,
                    "volume_ratio":                vol_ratio,
                    "structural_stop_price":       round(sl_price, 6),
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                },
            )
        except Exception as e:
            self.logger.error(f"KDSMeanReversionStrategy error on {symbol}: {e}",
                              exc_info=True)
            return None

    def _precompute(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        Pre-compute KDS/RSI/ADX/EMA on full df before bar loop. NOT dead code.
        Called once by backtester._simulate(); result passed to _analyze_from_precomputed()
        and _exit_from_precomputed() for O(1) lookups.
        """
        return generate_signals(df, self.params)

    def _analyze_from_precomputed(self, symbol: str, i: int,
                                  sigs: pd.DataFrame, df: pd.DataFrame) -> Optional[TradeSignal]:
        p   = self.params
        row = sigs.iloc[i]
        long_sig  = bool(row["long_signal"])
        short_sig = bool(row["short_signal"])
        if not long_sig and not short_sig:
            return None
        if pd.isna(row["atr"]) or pd.isna(row["kds"]):
            return None

        close = float(row["close"])
        atr   = float(row["atr"])
        rsi   = float(row["rsi"])
        adx   = float(row["adx"])
        kds   = float(row["kds"])
        ema   = float(row["ema"])

        direction = "long" if long_sig else "short"

        # SL: ATR-based (fixed — was kds*0.75*100 ≈ 150% stop)
        sl_price = (close - atr * p.sl_mult if direction == "long"
                    else close + atr * p.sl_mult)
        sl_pct   = round(abs(close - sl_price) / close * 100, 3)
        tp_pct   = round(max(abs(close - ema) / close * 100, 2.0), 3)

        ranging_score = max(0.0, min(1.0, (p.adx_max - adx) / p.adx_max))
        ext_score     = min(1.0, (abs(kds) - p.kds_thresh) / 2.0)
        score         = round(max(0.60, min(0.85,
                            0.60 + ranging_score * 0.15 + ext_score * 0.15)), 3)

        # Volume ratio — 20-bar rolling mean from precomputed df
        vol_series = df["volume"]
        start_v    = max(0, i - 19)
        vol_ma_val = vol_series.iloc[start_v:i].mean() if i > start_v else None
        vol_ratio  = (round(float(vol_series.iloc[i] / vol_ma_val), 3)
                      if vol_ma_val and vol_ma_val > 0 else None)

        return self._make_signal(
            symbol          = symbol,
            direction       = direction,
            score           = score,
            reason          = f"KDS={kds:.2f} RSI={rsi:.1f} ADX={adx:.1f}",
            stop_loss_pct   = sl_pct,
            take_profit_pct = tp_pct,
            metadata={
                "strategy_name":               "kds_mean_reversion",
                "kds":                         round(kds, 3),
                "rsi":                         round(rsi, 2),
                "adx":                         round(adx, 2),
                "atr":                         round(atr, 6),
                "ema":                         round(ema, 6),
                "atr_mult_used":               p.kds_thresh,
                "volume_ratio":                vol_ratio,
                "structural_stop_price":       round(sl_price, 6),
                "preferred_initial_stop_mode": "signal_structural",
                "preferred_trail_mode":        "none",
            },
        )

    def _exit_from_precomputed(self, i: int, sigs: pd.DataFrame,
                               direction: str, meta: dict) -> Optional[str]:
        """
        O(1) exit check — uses EMA from precomputed signals df. NOT dead code.
        Called by backtester._simulate() on every bar that has an open trade.
        """
        p = self.params
        try:
            row       = sigs.iloc[i]
            close_now = float(row["close"])
            ema_now   = float(row["ema"])
            if not pd.isna(ema_now):
                if direction == "long"  and close_now >= ema_now:
                    return "kds_ema_reversion_exit"
                if direction == "short" and close_now <= ema_now:
                    return "kds_ema_reversion_exit"
        except Exception:
            pass

        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= p.max_bars:
            return "kds_time_stop"

        return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        """
        KDS exit logic for the live path (and backtester slow path).

        Priority:
          A. EMA mean-reversion target exit — thesis is complete when price reaches the mean
          B. Time stop safety net — max_bars (72 daily bars ≈ 72 trading days)

        _bars_held is injected into entry_metadata by the backtester.
        """
        if bars is None or len(bars) < 2:
            return None

        p    = self.params
        meta = entry_metadata or {}

        # A. EMA mean-reversion exit
        try:
            close_now = float(bars["close"].iloc[-1])
            ema_now   = float(bars["close"].ewm(span=p.ema_length, adjust=False).mean().iloc[-1])
            if direction == "long"  and close_now >= ema_now:
                return "kds_ema_reversion_exit"
            if direction == "short" and close_now <= ema_now:
                return "kds_ema_reversion_exit"
        except Exception:
            pass

        # B. Time stop safety net
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held >= p.max_bars:
            return "kds_time_stop"

        return None
