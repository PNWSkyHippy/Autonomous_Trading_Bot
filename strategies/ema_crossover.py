"""
ema_crossover.py — Strategy 3: EMA Crossover
Trading Bot v2

Classic fast/slow exponential moving average crossover.
Trend EMA (50) used to confirm direction.

Long:  Fast EMA crosses above slow EMA, and price is above trend EMA
Short: Fast EMA crosses below slow EMA, and price is below trend EMA

Periods configurable in config.SIGNAL_TUNING.
"""

from typing import Optional

import pandas as pd
import pandas_ta as ta

import config
from strategies.base_strategy import BaseStrategy, TradeSignal


class EMACrossover(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.strategy_name   = "ema_crossover"
        self.stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT
        self.take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT
        self.stock_enabled   = True
        self.crypto_enabled  = True
        self.candle_limit    = 70   # trend_period(50) + buffer
        self.reviewer_exempt = True
        self.time_stop_profile = "strategy_defined"
        self.enabled = False   # no validated backtest results — enable only after testing
    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        tuning      = config.SIGNAL_TUNING
        fast_period = tuning["ema_fast_period"]
        slow_period = tuning["ema_slow_period"]
        trend_period= tuning["ema_trend_period"]
        min_score   = tuning["ema_crossover_min_score"]

        required = trend_period + 5
        if not self._check_enough_candles(symbol, candles, required):
            return None

        close = candles["close"]

        # Calculate EMAs
        try:
            ema_fast  = ta.ema(close, length=fast_period)
            ema_slow  = ta.ema(close, length=slow_period)
            ema_trend = ta.ema(close, length=trend_period)
        except Exception as e:
            self.verbose_log_skip(symbol, f"EMA calculation error: {e}")
            return None

        if ema_fast is None or ema_slow is None or ema_trend is None:
            self.verbose_log_skip(symbol, "One or more EMA calculations returned None")
            return None

        # Current and previous values
        fast_now   = ema_fast.iloc[-1]
        fast_prev  = ema_fast.iloc[-2]
        slow_now   = ema_slow.iloc[-1]
        slow_prev  = ema_slow.iloc[-2]
        trend_now  = ema_trend.iloc[-1]
        price_now  = close.iloc[-1]

        if any(pd.isna(v) for v in [fast_now, fast_prev, slow_now, slow_prev, trend_now]):
            self.verbose_log_skip(symbol, "EMA values contain NaN")
            return None

        # ----------------------------------------------------------------
        # LONG SIGNAL: fast EMA crossed above slow EMA from below
        # ----------------------------------------------------------------
        golden_cross = (fast_prev <= slow_prev) and (fast_now > slow_now)
        self.verbose_log(
            symbol, "Golden cross (fast EMA crossed above slow EMA)",
            golden_cross,
            f"fast={fast_now:.4f} prev_fast={fast_prev:.4f}",
            f"slow={slow_now:.4f} prev_slow={slow_prev:.4f}",
            "long"
        )

        if golden_cross:
            above_trend = price_now > trend_now
            self.verbose_log(
                symbol, "Price above trend EMA (long confirmation)",
                above_trend, price_now, f">{trend_now:.4f}", "long"
            )

            # How separated are the EMAs? More separation = stronger signal
            separation = abs(fast_now - slow_now) / slow_now if slow_now != 0 else 0
            score = min(1.0, min_score + separation * 10)
            if above_trend:
                score = min(1.0, score + 0.05)

            self.verbose_log_score(symbol, score, min_score)

            if score >= min_score:
                vol_series = candles["volume"]
                vol_ma     = vol_series.rolling(20).mean().iloc[-1]
                vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma), 3) if vol_ma > 0 else None
                stop_price = float(close.iloc[-1]) * (1 - config.DEFAULT_STOP_LOSS_PCT / 100)
                return self._make_signal(
                    symbol          = symbol,
                    direction       = "long",
                    score           = score,
                    reason          = (
                        f"Golden cross: fast={fast_now:.4f} > slow={slow_now:.4f} "
                        f"{'(above trend)' if above_trend else '(below trend)'}"
                    ),
                    stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT,
                    take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT,
                    metadata        = {
                        "strategy_name":               "ema_crossover",
                        "ema_fast":                    round(float(fast_now), 4),
                        "ema_slow":                    round(float(slow_now), 4),
                        "ema_trend":                   round(float(trend_now), 4),
                        "price_above_trend":           bool(above_trend),
                        "separation_pct":              round(separation * 100, 4),
                        "volume_ratio":                vol_ratio,
                        "structural_stop_price":       round(stop_price, 6),
                        "preferred_initial_stop_mode": "percent",
                        "preferred_trail_mode":        "percent",
                    },
                )

        # ----------------------------------------------------------------
        # SHORT SIGNAL: fast EMA crossed below slow EMA from above
        # ----------------------------------------------------------------
        death_cross = (fast_prev >= slow_prev) and (fast_now < slow_now)
        self.verbose_log(
            symbol, "Death cross (fast EMA crossed below slow EMA)",
            death_cross,
            f"fast={fast_now:.4f} prev_fast={fast_prev:.4f}",
            f"slow={slow_now:.4f} prev_slow={slow_prev:.4f}",
            "short"
        )

        if death_cross:
            below_trend = price_now < trend_now
            self.verbose_log(
                symbol, "Price below trend EMA (short confirmation)",
                below_trend, price_now, f"<{trend_now:.4f}", "short"
            )

            separation = abs(fast_now - slow_now) / slow_now if slow_now != 0 else 0
            score = min(1.0, min_score + separation * 10)
            if below_trend:
                score = min(1.0, score + 0.05)

            self.verbose_log_score(symbol, score, min_score)

            if score >= min_score:
                vol_series = candles["volume"]
                vol_ma     = vol_series.rolling(20).mean().iloc[-1]
                vol_ratio  = round(float(vol_series.iloc[-1] / vol_ma), 3) if vol_ma > 0 else None
                stop_price = float(close.iloc[-1]) * (1 + config.DEFAULT_STOP_LOSS_PCT / 100)
                return self._make_signal(
                    symbol          = symbol,
                    direction       = "short",
                    score           = score,
                    reason          = (
                        f"Death cross: fast={fast_now:.4f} < slow={slow_now:.4f} "
                        f"{'(below trend)' if below_trend else '(above trend)'}"
                    ),
                    stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT,
                    take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT,
                    metadata        = {
                        "strategy_name":               "ema_crossover",
                        "ema_fast":                    round(float(fast_now), 4),
                        "ema_slow":                    round(float(slow_now), 4),
                        "ema_trend":                   round(float(trend_now), 4),
                        "price_below_trend":           bool(below_trend),
                        "separation_pct":              round(separation * 100, 4),
                        "volume_ratio":                vol_ratio,
                        "structural_stop_price":       round(stop_price, 6),
                        "preferred_initial_stop_mode": "percent",
                        "preferred_trail_mode":        "percent",
                    },
                )

        # No crossover this candle
        self.verbose_log(
            symbol, "No EMA crossover (no signal)",
            False,
            f"fast={fast_now:.4f}",
            f"slow={slow_now:.4f}",
            extra=f"trend={trend_now:.4f}"
        )
        return None
