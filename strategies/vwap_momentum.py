"""
vwap_momentum.py — Strategy 9: VWAP Momentum
Trading Bot v2

Combines the EMA 5/13/34 momentum stack from the Reddit algotrading
community with VWAP directional filtering, ADX trend confirmation,
RSI momentum zone check, and volume conviction requirement.

Entry logic (long):
  1. Price above VWAP (intraday trend is up)
  2. EMA5 > EMA13 > EMA34 (short-term momentum stack bullish)
  3. ADX >= 25 (market is trending, not ranging)
  4. RSI between 40-60 (momentum building, not extended/exhausted)
  5. RSI hard block: never enter if RSI > 82 (learned from IONQ at RSI 98)
  6. Volume >= 1.5x 20-bar average (conviction behind the move)

Entry logic (short): mirror of above — below VWAP, EMA stack bearish,
RSI 40-60, RSI hard block < 18.

Timeframe: 5-minute bars (stock market hours only).
Stock-only: crypto has no reliable VWAP anchor on 24/7 markets.

Metadata includes entry_time_utc for position_monitor's 90-minute
time-stop to detect dead positions (no TP/SL hit after 90 minutes).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import pandas_ta as ta
import numpy as np

import config
from strategies.base_strategy import BaseStrategy, TradeSignal


# ── Stock whitelist ──────────────────────────────────────────────────────────
# High-liquidity momentum names where VWAP is well-respected intraday.
# Avoid low-float stocks where VWAP is less meaningful.
VWAP_MOM_WHITELIST = {
    "META", "NVDA", "MSFT", "GOOGL", "AMZN", "TSLA",
    "AMD",  "PLTR", "ALAB", "IREN",  "MARA", "MRVL",
    "INTC", "CLS",  "CRWV", "APLD",  "NIO",
}

# ── RSI hard block thresholds ────────────────────────────────────────────────
# Never enter a long when RSI is this high — price is exhausted.
# Never enter a short when RSI is this low — price is capitulating.
# Learned the hard way: buying at RSI 98 (IONQ) is how you get destroyed.
RSI_HARD_BLOCK_LONG  = 82    # No longs above this RSI
RSI_HARD_BLOCK_SHORT = 18    # No shorts below this RSI


def _calc_vwap(candles: pd.DataFrame) -> Optional[float]:
    """Intraday VWAP from available bars. Returns current value or None."""
    try:
        typical = (candles["high"] + candles["low"] + candles["close"]) / 3
        cum_tpv = (typical * candles["volume"]).cumsum()
        cum_vol = candles["volume"].cumsum()
        vwap    = cum_tpv / cum_vol.replace(0, float("nan"))
        val     = vwap.iloc[-1]
        return float(val) if not pd.isna(val) else None
    except Exception:
        return None


def _calc_adx(candles: pd.DataFrame, period: int = 14) -> Optional[float]:
    """ADX value from pandas_ta. Returns current value or None."""
    try:
        adx_df = ta.adx(
            candles["high"], candles["low"], candles["close"], length=period
        )
        if adx_df is None or adx_df.empty:
            return None
        col = [c for c in adx_df.columns if c.startswith("ADX_")]
        if not col:
            return None
        val = adx_df[col[0]].iloc[-1]
        return float(val) if not pd.isna(val) else None
    except Exception:
        return None


class VWAPMomentum(BaseStrategy):
    """
    VWAP + EMA 5/13/34 momentum strategy.
    Stock-only, 5-minute bars, trending markets only.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name   = "vwap_momentum"
        self.stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT
        self.take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT

        # VWAP momentum confirmed at 1hr/365 days on stocks (almost all GO).
        # 5min had very few trades per symbol — not enough for reliable signals.
        # VWAP is a daily level concept and works better on 1hr bars.
        # Crypto disabled — 24/7 markets have no reliable intraday VWAP anchor.
        self.stock_candle_timeframe  = "1Hour"
        self.candle_limit            = 200
        self.crypto_enabled          = False

        # ML model is dominated by grid_bot (1507 trades); vwap_momentum has
        # minimal training representation — ML blending collapses valid scores.
        self.ml_exempt = True

        self.logger          = logging.getLogger("VWAPMomentum")

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        # ── Whitelist ────────────────────────────────────────────────────
        if not self._passes_symbol_whitelist(
            symbol, VWAP_MOM_WHITELIST, "VWAP momentum whitelist"
        ):
            self.verbose_log_skip(
                symbol,
                f"Not in VWAP momentum whitelist — skipping"
            )
            return None

        # ── Skip ranging markets — VWAP momentum needs a trend ───────────
        if market_condition == "ranging":
            self.verbose_log_skip(symbol, "Ranging market — VWAP momentum unreliable")
            return None

        tuning       = config.SIGNAL_TUNING
        adx_min      = tuning.get("vwap_mom_adx_min",        25)
        adx_period   = tuning.get("vwap_mom_adx_period",     14)
        rsi_period   = tuning.get("vwap_mom_rsi_period",     14)
        rsi_low      = tuning.get("vwap_mom_rsi_low",        40)
        rsi_high     = tuning.get("vwap_mom_rsi_high",       60)
        vol_ratio    = tuning.get("vwap_mom_volume_ratio",   1.5)
        min_score    = tuning.get("vwap_mom_min_score",      0.65)

        # Need enough bars for EMA34 + ADX14 to be meaningful
        if not self._check_enough_candles(symbol, candles, 50):
            return None

        close  = candles["close"]
        high   = candles["high"]
        low    = candles["low"]
        volume = candles["volume"]
        current_close = float(close.iloc[-1])

        # ── 1. VWAP directional filter ───────────────────────────────────
        vwap_val = _calc_vwap(candles)
        if vwap_val is None:
            self.verbose_log_skip(symbol, "VWAP unavailable")
            return None
        price_above_vwap = current_close > vwap_val
        self.logger.info(
            f"[VWAPMom] {symbol}: price={current_close:.4f} "
            f"VWAP={vwap_val:.4f} above={price_above_vwap}"
        )

        # ── 2. EMA 5/13/34 stack alignment ──────────────────────────────
        # Bullish stack: EMA5 > EMA13 > EMA34
        # Bearish stack: EMA5 < EMA13 < EMA34
        try:
            ema5  = ta.ema(close, length=5)
            ema13 = ta.ema(close, length=13)
            ema34 = ta.ema(close, length=34)
            if any(e is None for e in [ema5, ema13, ema34]):
                self.verbose_log_skip(symbol, "EMA calculation failed")
                return None
            e5  = float(ema5.iloc[-1])
            e13 = float(ema13.iloc[-1])
            e34 = float(ema34.iloc[-1])
            ema_bullish = e5 > e13 > e34
            ema_bearish = e5 < e13 < e34
            self.logger.info(
                f"[VWAPMom] {symbol}: EMA5={e5:.4f} EMA13={e13:.4f} "
                f"EMA34={e34:.4f} bullish={ema_bullish} bearish={ema_bearish}"
            )
        except Exception as ex:
            self.verbose_log_skip(symbol, f"EMA error: {ex}")
            return None

        # ── 3. ADX trend strength ────────────────────────────────────────
        adx_val = _calc_adx(candles, period=adx_period)
        if adx_val is None:
            self.verbose_log_skip(symbol, "ADX unavailable")
            return None
        adx_ok = adx_val >= adx_min
        self.verbose_log(
            symbol, f"ADX trend strength (need >={adx_min})",
            adx_ok, round(adx_val, 1), f">={adx_min}"
        )
        if not adx_ok:
            self.logger.info(
                f"[VWAPMom] {symbol}: SKIP — ADX={adx_val:.1f} < {adx_min} (choppy)"
            )
            return None

        # ── 4. RSI momentum zone ─────────────────────────────────────────
        # RSI 40-60: momentum is building but not exhausted.
        # This is the "sweet spot" — not oversold (mean reversion territory),
        # not overbought (exhaustion territory). Pure momentum continuation.
        try:
            rsi_series = ta.rsi(close, length=rsi_period)
            if rsi_series is None or rsi_series.isna().all():
                self.verbose_log_skip(symbol, "RSI unavailable")
                return None
            current_rsi = float(rsi_series.iloc[-1])
            if pd.isna(current_rsi):
                self.verbose_log_skip(symbol, "RSI is NaN")
                return None
        except Exception as ex:
            self.verbose_log_skip(symbol, f"RSI error: {ex}")
            return None

        self.logger.info(f"[VWAPMom] {symbol}: RSI={current_rsi:.1f}")

        # ── 5. Volume conviction ─────────────────────────────────────────
        avg_vol     = volume.iloc[-20:].mean()
        current_vol = float(volume.iloc[-1])
        cur_vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0
        volume_ok   = cur_vol_ratio >= vol_ratio
        self.verbose_log(
            symbol, f"Volume conviction (need >={vol_ratio}x)",
            volume_ok, round(cur_vol_ratio, 2), f">={vol_ratio}x"
        )
        if not volume_ok:
            self.logger.info(
                f"[VWAPMom] {symbol}: SKIP — volume {cur_vol_ratio:.2f}x < {vol_ratio}x"
            )
            return None

        # ── Metadata shared by both signal directions ────────────────────
        base_metadata = {
            "vwap":           round(vwap_val, 4),
            "ema5":           round(e5, 4),
            "ema13":          round(e13, 4),
            "ema34":          round(e34, 4),
            "adx":            round(adx_val, 1),
            "rsi":            round(current_rsi, 1),
            "volume_ratio":   round(cur_vol_ratio, 2),
            # Entry timestamp for 90-minute time-stop in position_monitor
            "entry_time_utc": datetime.now(timezone.utc).isoformat(),
            "time_stop_min":  90,
        }

        # ── LONG signal ──────────────────────────────────────────────────
        if price_above_vwap and ema_bullish:
            # RSI must be in momentum zone (40-60) for longs
            rsi_in_zone = rsi_low <= current_rsi <= rsi_high
            self.verbose_log(
                symbol, f"RSI in momentum zone {rsi_low}-{rsi_high} (long)",
                rsi_in_zone, round(current_rsi, 1),
                f"{rsi_low}-{rsi_high}", "long"
            )
            if not rsi_in_zone:
                self.logger.info(
                    f"[VWAPMom] {symbol}: LONG rejected — "
                    f"RSI {current_rsi:.1f} outside zone {rsi_low}-{rsi_high}"
                )
                return None

            # Hard block: never buy an exhausted move
            if current_rsi > RSI_HARD_BLOCK_LONG:
                self.logger.info(
                    f"[VWAPMom] {symbol}: LONG hard-blocked — "
                    f"RSI {current_rsi:.1f} > {RSI_HARD_BLOCK_LONG} (exhausted)"
                )
                return None

            # Score: base + bonus for strong volume and ADX
            score = min(1.0, min_score
                        + (cur_vol_ratio - vol_ratio) * 0.03
                        + (adx_val - adx_min) * 0.002)
            self.verbose_log_score(symbol, score, min_score)

            if score >= min_score:
                return self._make_signal(
                    symbol    = symbol,
                    direction = "long",
                    score     = round(score, 3),
                    reason    = (
                        f"VWAP Mom long: price ${current_close:.4f} > VWAP ${vwap_val:.4f} "
                        f"| EMA5>{e13:.2f}>EMA34 "
                        f"| ADX={adx_val:.1f} RSI={current_rsi:.1f} "
                        f"| vol={cur_vol_ratio:.2f}x"
                    ),
                    metadata  = {**base_metadata, "signal_type": "vwap_long"}
                )

        # ── SHORT signal ─────────────────────────────────────────────────
        if not price_above_vwap and ema_bearish:
            rsi_in_zone = rsi_low <= current_rsi <= rsi_high
            self.verbose_log(
                symbol, f"RSI in momentum zone {rsi_low}-{rsi_high} (short)",
                rsi_in_zone, round(current_rsi, 1),
                f"{rsi_low}-{rsi_high}", "short"
            )
            if not rsi_in_zone:
                self.logger.info(
                    f"[VWAPMom] {symbol}: SHORT rejected — "
                    f"RSI {current_rsi:.1f} outside zone {rsi_low}-{rsi_high}"
                )
                return None

            # Hard block: never short a capitulating move
            if current_rsi < RSI_HARD_BLOCK_SHORT:
                self.logger.info(
                    f"[VWAPMom] {symbol}: SHORT hard-blocked — "
                    f"RSI {current_rsi:.1f} < {RSI_HARD_BLOCK_SHORT} (capitulating)"
                )
                return None

            score = min(1.0, min_score
                        + (cur_vol_ratio - vol_ratio) * 0.03
                        + (adx_val - adx_min) * 0.002)
            self.verbose_log_score(symbol, score, min_score)

            if score >= min_score:
                return self._make_signal(
                    symbol    = symbol,
                    direction = "short",
                    score     = round(score, 3),
                    reason    = (
                        f"VWAP Mom short: price ${current_close:.4f} < VWAP ${vwap_val:.4f} "
                        f"| EMA5<{e13:.2f}<EMA34 "
                        f"| ADX={adx_val:.1f} RSI={current_rsi:.1f} "
                        f"| vol={cur_vol_ratio:.2f}x"
                    ),
                    metadata  = {**base_metadata, "signal_type": "vwap_short"}
                )

        # No signal — conditions not fully aligned
        self.logger.debug(
            f"[VWAPMom] {symbol}: no signal — "
            f"above_vwap={price_above_vwap} ema_bullish={ema_bullish} "
            f"ema_bearish={ema_bearish} RSI={current_rsi:.1f}"
        )
        return None
