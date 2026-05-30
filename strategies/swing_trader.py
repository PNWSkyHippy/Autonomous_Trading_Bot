"""
swing_trader.py — Strategy 6: Swing Trader
Trading Bot v2

Wider stops (2.5%) and bigger targets (6%). Designed for trending markets.
Requires ADX > threshold to confirm a trend exists before entering.
Uses RSI for entry timing within the trend.

ADX and RSI thresholds configurable in config.SIGNAL_TUNING.
"""

import logging
from typing import Optional

import pandas as pd
import pandas_ta as ta

import config
from strategies.base_strategy import BaseStrategy, TradeSignal


class SwingTrader(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.strategy_name   = "swing_trader"
        self.stop_loss_pct   = 2.5     # Wider stop for swing trades
        self.take_profit_pct = 6.0     # Bigger target
        self.enabled = True
        # Swing trader needs 1hr candles — 5min produces too few trades
        # to generate meaningful signals (1-5 per symbol at 5min).
        # At 1hr/365 days swing_trader shows proper trade counts and GO results.
        self.stock_candle_timeframe  = "1Hour"
        self.crypto_candle_timeframe = "1h"
        self.candle_limit            = 200   # Need more 1hr bars for trend context

        self.logger = logging.getLogger("SwingTrader")

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        tuning        = config.SIGNAL_TUNING
        adx_min       = tuning["swing_adx_min"]
        adx_period    = tuning["swing_adx_period"]
        rsi_oversold  = tuning["swing_rsi_oversold"]
        rsi_overbought= tuning["swing_rsi_overbought"]
        min_score     = tuning["swing_min_score"]

        required = max(adx_period, 14) + 10
        if not self._check_enough_candles(symbol, candles, required):
            return None

        # Calculate ADX
        try:
            adx_data = ta.adx(
                candles["high"], candles["low"], candles["close"],
                length=adx_period
            )
            if adx_data is None or adx_data.empty:
                self.verbose_log_skip(symbol, "ADX calculation returned no data")
                return None
        except Exception as e:
            self.verbose_log_skip(symbol, f"ADX calculation error: {e}")
            return None

        adx_col = [c for c in adx_data.columns if c.startswith("ADX_")]
        dmp_col = [c for c in adx_data.columns if c.startswith("DMP_")]  # +DI
        dmn_col = [c for c in adx_data.columns if c.startswith("DMN_")]  # -DI

        if not adx_col:
            self.verbose_log_skip(symbol, "ADX column not found in output")
            return None

        adx_value = adx_data[adx_col[0]].iloc[-1]
        dmp_value = adx_data[dmp_col[0]].iloc[-1] if dmp_col else None
        dmn_value = adx_data[dmn_col[0]].iloc[-1] if dmn_col else None

        if pd.isna(adx_value):
            self.verbose_log_skip(symbol, "ADX value is NaN")
            return None

        # ADX must confirm a trending market
        trend_confirmed = adx_value >= adx_min
        self.verbose_log(
            symbol, "ADX confirms trend (swing entry requires trending market)",
            trend_confirmed, adx_value, f">={adx_min}",
            extra=f"+DI={dmp_value:.2f} -DI={dmn_value:.2f}" if dmp_value and dmn_value else ""
        )

        if not trend_confirmed:
            return None

        # Calculate RSI for entry timing
        try:
            rsi_series = ta.rsi(candles["close"], length=14)
            if rsi_series is None:
                self.verbose_log_skip(symbol, "RSI returned None")
                return None
        except Exception as e:
            self.verbose_log_skip(symbol, f"RSI error: {e}")
            return None

        current_rsi = rsi_series.iloc[-1]
        if pd.isna(current_rsi):
            self.verbose_log_skip(symbol, "RSI is NaN")
            return None

        # ----------------------------------------------------------------
        # LONG SIGNAL: trending up (+DI > -DI) AND RSI pulled back to oversold
        # ----------------------------------------------------------------
        if dmp_value is not None and dmn_value is not None:
            bullish_trend = dmp_value > dmn_value
        else:
            bullish_trend = True  # Assume bullish if DI data unavailable

        self.verbose_log(
            symbol, "Bullish trend direction (+DI > -DI)",
            bullish_trend,
            f"+DI={dmp_value:.2f}" if dmp_value else "N/A",
            f"-DI={dmn_value:.2f}" if dmn_value else "N/A",
            "long"
        )

        if bullish_trend:
            rsi_pullback = current_rsi < rsi_oversold
            self.verbose_log(
                symbol, "RSI pullback in uptrend (swing long entry)",
                rsi_pullback, current_rsi, f"<{rsi_oversold}", "long"
            )
            if rsi_pullback:
                adx_strength = min(1.0, (adx_value - adx_min) / 30)
                score = min(1.0, min_score + adx_strength * 0.2)
                self.verbose_log_score(symbol, score, min_score)
                if score >= min_score:
                    return self._make_signal(
                        symbol    = symbol,
                        direction = "long",
                        score     = score,
                        reason    = (
                            f"Swing long: ADX={adx_value:.1f} trending, "
                            f"RSI pullback={current_rsi:.1f}"
                        )
                    )

        # ----------------------------------------------------------------
        # SHORT SIGNAL: trending down (-DI > +DI) AND RSI extended to overbought
        # ----------------------------------------------------------------
        if dmp_value is not None and dmn_value is not None:
            bearish_trend = dmn_value > dmp_value
        else:
            bearish_trend = False

        self.verbose_log(
            symbol, "Bearish trend direction (-DI > +DI)",
            bearish_trend,
            f"-DI={dmn_value:.2f}" if dmn_value else "N/A",
            f"+DI={dmp_value:.2f}" if dmp_value else "N/A",
            "short"
        )

        if bearish_trend:
            rsi_extended = current_rsi > rsi_overbought
            self.verbose_log(
                symbol, "RSI extended in downtrend (swing short entry)",
                rsi_extended, current_rsi, f">{rsi_overbought}", "short"
            )
            if rsi_extended:
                adx_strength = min(1.0, (adx_value - adx_min) / 30)
                score = min(1.0, min_score + adx_strength * 0.2)
                self.verbose_log_score(symbol, score, min_score)
                if score >= min_score:
                    return self._make_signal(
                        symbol    = symbol,
                        direction = "short",
                        score     = score,
                        reason    = (
                            f"Swing short: ADX={adx_value:.1f} trending, "
                            f"RSI extended={current_rsi:.1f}"
                        )
                    )

        self.verbose_log(
            symbol, "Swing: ADX confirms trend but no entry condition met",
            False, current_rsi,
            f"need RSI <{rsi_oversold} (long) or >{rsi_overbought} (short)"
        )
        return None
