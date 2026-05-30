"""
rsi_momentum.py — Strategy 1: RSI Momentum
Trading Bot v2

Enters when RSI crosses back from oversold or overbought territory.
Long:  RSI was below oversold threshold, now crossing back above it.
Short: RSI was above overbought threshold, now crossing back below it.

Thresholds are configurable in config.SIGNAL_TUNING.
"""

import math
from typing import Optional

import pandas as pd
import pandas_ta as ta

import config
from strategies.base_strategy import BaseStrategy, TradeSignal


class RSIMomentum(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.strategy_name   = "rsi_momentum"
        self.stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT
        self.take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT
        self.stock_enabled   = True
        self.crypto_enabled  = True
        self.candle_limit    = 30   # RSI warmup + buffer
        self.reviewer_exempt = True
        self.time_stop_profile = "strategy_defined"
        self.enabled = False   # no validated backtest results — enable only after testing
    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:
        """Analyze RSI for crossback signals from extreme levels."""

        tuning      = config.SIGNAL_TUNING
        oversold    = tuning["rsi_momentum_oversold"]
        overbought  = tuning["rsi_momentum_overbought"]
        period      = tuning["rsi_momentum_period"]
        min_score   = tuning["rsi_momentum_min_score"]

        # Need enough candles for RSI calculation
        required = period + 5
        if not self._check_enough_candles(symbol, candles, required):
            return None

        # Calculate RSI
        try:
            rsi_series = ta.rsi(candles["close"], length=period)
            if rsi_series is None or rsi_series.isna().all():
                self.verbose_log_skip(symbol, "RSI calculation returned no data")
                return None
        except Exception as e:
            self.verbose_log_skip(symbol, f"RSI calculation error: {e}")
            return None

        current_rsi  = rsi_series.iloc[-1]
        previous_rsi = rsi_series.iloc[-2]

        if pd.isna(current_rsi) or pd.isna(previous_rsi):
            self.verbose_log_skip(symbol, "RSI values are NaN")
            return None

        # ----------------------------------------------------------------
        # LONG SIGNAL: RSI was below oversold, now crossing back above it
        # ----------------------------------------------------------------
        was_oversold = previous_rsi < oversold
        self.verbose_log(
            symbol, "Previous RSI was oversold (long setup)",
            was_oversold, previous_rsi, f"<{oversold}", "long"
        )

        if was_oversold:
            now_recovering = current_rsi >= oversold
            self.verbose_log(
                symbol, "Current RSI crossed back above oversold (long entry)",
                now_recovering, current_rsi, f">={oversold}", "long"
            )

            if now_recovering:
                # Score based on how deep the oversold was
                depth = max(0.0, (oversold - previous_rsi) / oversold)
                score = min(1.0, min_score + depth * 0.3)
                self.verbose_log_score(symbol, score, min_score)

                if score >= min_score:
                    vol_series = candles["volume"]
                    vol_ma     = vol_series.rolling(20).mean().iloc[-1]
                    vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma), 3) if vol_ma > 0 else None
                    return self._make_signal(
                        symbol          = symbol,
                        direction       = "long",
                        score           = score,
                        reason          = (
                            f"RSI crossback from oversold: "
                            f"prev={previous_rsi:.1f} → curr={current_rsi:.1f} "
                            f"(threshold={oversold})"
                        ),
                        stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT,
                        take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT,
                        metadata        = {
                            "strategy_name":               "rsi_momentum",
                            "rsi":                         round(float(current_rsi), 2),
                            "rsi_prev":                    round(float(previous_rsi), 2),
                            "rsi_depth":                   round(depth, 4),
                            "volume_ratio":                vol_ratio,
                            "preferred_initial_stop_mode": "percent",
                            "preferred_trail_mode":        "percent",
                        },
                    )

        # ----------------------------------------------------------------
        # SHORT SIGNAL: RSI was above overbought, now crossing back below it
        # ----------------------------------------------------------------
        was_overbought = previous_rsi > overbought
        self.verbose_log(
            symbol, "Previous RSI was overbought (short setup)",
            was_overbought, previous_rsi, f">{overbought}", "short"
        )

        if was_overbought:
            now_retreating = current_rsi <= overbought
            self.verbose_log(
                symbol, "Current RSI crossed back below overbought (short entry)",
                now_retreating, current_rsi, f"<={overbought}", "short"
            )

            if now_retreating:
                height = max(0.0, (previous_rsi - overbought) / (100 - overbought))
                score  = min(1.0, min_score + height * 0.3)
                self.verbose_log_score(symbol, score, min_score)

                if score >= min_score:
                    vol_series = candles["volume"]
                    vol_ma     = vol_series.rolling(20).mean().iloc[-1]
                    vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma), 3) if vol_ma > 0 else None
                    return self._make_signal(
                        symbol          = symbol,
                        direction       = "short",
                        score           = score,
                        reason          = (
                            f"RSI crossback from overbought: "
                            f"prev={previous_rsi:.1f} → curr={current_rsi:.1f} "
                            f"(threshold={overbought})"
                        ),
                        stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT,
                        take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT,
                        metadata        = {
                            "strategy_name":               "rsi_momentum",
                            "rsi":                         round(float(current_rsi), 2),
                            "rsi_prev":                    round(float(previous_rsi), 2),
                            "rsi_height":                  round(height, 4),
                            "volume_ratio":                vol_ratio,
                            "preferred_initial_stop_mode": "percent",
                            "preferred_trail_mode":        "percent",
                        },
                    )

        # No signal — log the current RSI so we can see how far off we are
        self.verbose_log(
            symbol, "RSI in neutral zone (no signal)",
            False, current_rsi,
            f"need <{oversold} or >{overbought}"
        )
        return None

    def check_custom_exit(self, symbol: str, bars: pd.DataFrame,
                          direction: str, entry_metadata: Optional[dict] = None) -> Optional[str]:
        if bars is None or len(bars) < 2:
            return None
        try:
            tuning   = config.SIGNAL_TUNING
            period   = tuning["rsi_momentum_period"]
            rsi_s    = ta.rsi(bars["close"], length=period)
            if rsi_s is None or rsi_s.empty:
                return None
            rsi_now  = float(rsi_s.iloc[-1])
            if math.isnan(rsi_now):
                return None
            if direction == "long"  and rsi_now >= tuning["rsi_momentum_overbought"]:
                return "rsi_overbought_exit"
            if direction == "short" and rsi_now <= tuning["rsi_momentum_oversold"]:
                return "rsi_oversold_exit"
        except Exception:
            pass
        return None
