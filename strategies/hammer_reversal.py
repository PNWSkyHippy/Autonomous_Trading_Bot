"""
Hammer Reversal Strategy (Strategy 10)
=======================================
Port of Gerald Lonlas' freqtrade Strategy002 to our framework.

Original backtest: 9 trades, 3.21% avg profit, Jan 2018 (crypto 5m bars).

Entry conditions (ALL must be true — very selective by design):
  1. RSI < 30               (oversold)
  2. Stochastic slowK < 20  (confirming oversold)
  3. Price < lower Bollinger Band  (extreme deviation below mean)
  4. Hammer candlestick detected   (reversal signal)
  5. OBV rising over last 3 bars   (volume confirming buyers stepping in)

All five firing simultaneously means the market has:
  - Been sold off hard (RSI + Stoch both oversold)
  - Closed below the lower BB (statistically extreme)
  - Printed a reversal candle (buyers stepping in)
  - Volume confirming the reversal (OBV rising = accumulation not distribution)

The OBV filter is the key addition over the original freqtrade strategy.
Without it, hammer candles in a waterfall selloff get triggered on every
bounce attempt even when volume is still flowing out. OBV rising means
real buyers are absorbing the sell pressure.

Exit / Take-profit — tiered ROI ladder (mirrors freqtrade original):
  <20 bars held  → target 5% gain
  20-30 bars     → target 4%
  30-60 bars     → target 3%
  60+ bars       → target 1% (kill switch — take anything)

Fisher RSI exit: exit when fisher_rsi > 0.3 (RSI no longer extreme)

Stop-loss: 10% (wide, matching original — the hammer entry is already
  conservative so a tight stop would whipsaw too often).

Whitelist:
  VALIDATED (backtested Apr 2026, 5m 30d):
    SOL/USD  — Sharpe 1.37, Profit Factor 2.17 ✔ GO (primary)
    BTC/USD  — Sharpe 0.75, marginal — OBV filter adds robustness; monitor live.
    ETH/USD  — Sharpe -3.20 — hard fail, excluded permanently until re-tested.

  MONITOR (not individually backtested — similar reversal characteristics to SOL):
    ADA / AVAX / DOT / LINK — monitor live carefully, not full-size validated.
    Controlled by HAMMER_INCLUDE_MONITOR flag (default False).

Asset class: crypto only, 5-minute bars
Default: ENABLED — validated symbols only
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional

import config
from strategies.base_strategy import BaseStrategy, TradeSignal

logger = logging.getLogger(__name__)

# ── Validated symbols (backtested Apr 2026, 5m 30d) ──────────────────────────
HAMMER_WHITELIST_VALIDATED = {
    "SOL/USD",  "SOL/USDT",
    "BTC/USD",  "BTC/USDT",
}

# ── Monitor symbols (not backtested individually — signal quality similar to SOL)
# Set HAMMER_INCLUDE_MONITOR = True to include these. Off by default.
HAMMER_INCLUDE_MONITOR = False
HAMMER_WHITELIST_MONITOR = {
    "ADA/USD",  "ADA/USDT",
    "AVAX/USD", "AVAX/USDT",
    "DOT/USD",  "DOT/USDT",
    "LINK/USD", "LINK/USDT",
}

HAMMER_WHITELIST = (
    HAMMER_WHITELIST_VALIDATED | HAMMER_WHITELIST_MONITOR
    if HAMMER_INCLUDE_MONITOR
    else HAMMER_WHITELIST_VALIDATED
)

# Number of consecutive OBV bars that must be rising to confirm buying pressure
OBV_RISING_BARS = 3


class HammerReversal(BaseStrategy):
    """
    Hammer Reversal — port of freqtrade Strategy002.
    RSI+Stoch oversold + below lower BB + Hammer candle + OBV rising.
    Very selective, high average profit per trade.
    SOL/USD primary based on backtest results.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name   = "hammer_reversal"
        self.stop_loss_pct   = 10.0   # Wide stop — entry is already conservative
        self.take_profit_pct = 5.0    # Initial TP — tiered ladder overrides in exits

        # ── Tunable parameters ──────────────────────────────────────────
        self.rsi_period       = 14
        self.rsi_oversold     = 30
        self.stoch_k_period   = 14
        self.stoch_d_period   = 3
        self.stoch_oversold   = 20
        self.bb_period        = 20
        self.bb_std           = 2.0
        self.min_score        = 0.70
        self.fisher_rsi_exit  = 0.3   # Exit when RSI no longer in extreme territory
        self.obv_rising_bars  = OBV_RISING_BARS

        # ROI ladder thresholds (bars held → min profit % to exit)
        self.roi_ladder = {
            0:  5.0,
            20: 4.0,
            30: 3.0,
            60: 1.0,
        }

        # Enabled — validated symbols only
        self.enabled          = True

        # ML model is dominated by grid_bot (1507 trades); hammer_reversal has
        # ~0 representation so ML blending collapses valid scores below threshold.
        self.ml_exempt        = True   # Skip ML score blending
        self.reviewer_exempt  = True   # Skip Claude reviewer gate

        # Time-stop profile — let the ROI ladder play out (60+ bar kill switch)
        # Generic intraday time stops (~30m) would fire before the ladder exits.
        self.time_stop_profile = "strategy_defined"

        logger.info(
            f"[{self.strategy_name}] Initialized — ENABLED. "
            f"Validated whitelist: {sorted(HAMMER_WHITELIST_VALIDATED)}. "
            f"Monitor symbols {'ON' if HAMMER_INCLUDE_MONITOR else 'OFF'}. "
            f"5-filter stack: RSI + Stoch + BB + Hammer + OBV."
        )

    def check_custom_exit(
        self,
        symbol: str,
        bars: pd.DataFrame,
        direction: str,
        entry_metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Tiered ROI ladder exit + Fisher RSI exit.

        Signature matches BaseStrategy.check_custom_exit and the backtester
        call: (symbol, bars_window, direction, entry_metadata).
        _bars_held and _entry_price are injected into entry_metadata by the
        backtester before this method is called.

        ROI ladder (matches freqtrade original):
          <20 bars → take profit at 5%
          20-30    → take profit at 4%
          30-60    → take profit at 3%
          60+      → take anything ≥1% (kill switch)

        Fisher RSI exit:
          Exit when fisher_rsi > fisher_rsi_exit threshold AND trade is
          profitable (RSI has recovered from extreme oversold — thesis complete).
        """
        meta        = entry_metadata or {}
        bars_held   = int(meta.get("_bars_held", 0))
        entry_price = meta.get("_entry_price")

        if not entry_price or entry_price <= 0:
            return None

        current_price = float(bars["close"].iloc[-1])
        if direction == "long":
            pnl_pct = (current_price - entry_price) / entry_price * 100.0
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100.0

        # ── ROI ladder ───────────────────────────────────────────────────
        if bars_held >= 60:
            target = 1.0
        elif bars_held >= 30:
            target = 3.0
        elif bars_held >= 20:
            target = 4.0
        else:
            target = 5.0

        if pnl_pct >= target:
            logger.info(
                f"[HammerReversal] {symbol}: ROI ladder exit "
                f"bars_held={bars_held} pnl={pnl_pct:.2f}% >= target={target}%"
            )
            return "hammer_roi_exit"

        # ── Fisher RSI exit ─────────────────────────────────────────────
        if pnl_pct > 0:
            try:
                closes   = bars["close"].astype(float)
                delta    = closes.diff()
                gain     = delta.clip(lower=0)
                loss     = -delta.clip(upper=0)
                avg_gain = gain.ewm(com=self.rsi_period - 1, adjust=False).mean()
                avg_loss = loss.ewm(com=self.rsi_period - 1, adjust=False).mean()
                rs       = avg_gain / avg_loss.replace(0, np.nan)
                rsi      = 100 - (100 / (1 + rs))
                rsi_now  = float(rsi.iloc[-1])

                rsi_scaled = 0.1 * (rsi_now - 50)
                fisher_rsi = (np.exp(2 * rsi_scaled) - 1) / (np.exp(2 * rsi_scaled) + 1)

                if fisher_rsi > self.fisher_rsi_exit:
                    logger.info(
                        f"[HammerReversal] {symbol}: Fisher RSI exit "
                        f"fisher={fisher_rsi:.3f}>{self.fisher_rsi_exit} "
                        f"pnl={pnl_pct:.2f}%"
                    )
                    return "hammer_fisher_exit"
            except Exception:
                pass

        return None

    def _calc_obv(self, closes: pd.Series, volumes: pd.Series) -> pd.Series:
        """
        On-Balance Volume (OBV).
        OBV rises when close > previous close (buying pressure).
        OBV falls when close < previous close (selling pressure).
        """
        direction = closes.diff().apply(
            lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
        )
        obv = (volumes * direction).cumsum()
        return obv

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:
        """
        Check all five entry conditions. Returns signal only when ALL fire.
        Validated whitelist gate is first check.
        """
        # ── Whitelist gate ────────────────────────────────────────────────
        if not self._passes_symbol_whitelist(
            symbol, HAMMER_WHITELIST, "Hammer whitelist"
        ):
            self.verbose_log_skip(symbol, "Not in Hammer whitelist")
            return None

        if not self._check_enough_candles(symbol, candles, 60):
            return None

        try:
            closes  = candles["close"].astype(float)
            highs   = candles["high"].astype(float)
            lows    = candles["low"].astype(float)
            opens   = candles["open"].astype(float)
            volumes = candles["volume"].astype(float)

            # ── 1. RSI ──────────────────────────────────────────────────────
            delta    = closes.diff()
            gain     = delta.clip(lower=0)
            loss     = -delta.clip(upper=0)
            avg_gain = gain.ewm(com=self.rsi_period - 1, adjust=False).mean()
            avg_loss = loss.ewm(com=self.rsi_period - 1, adjust=False).mean()
            rs       = avg_gain / avg_loss.replace(0, np.nan)
            rsi      = 100 - (100 / (1 + rs))
            rsi_now  = rsi.iloc[-1]

            rsi_ok = rsi_now < self.rsi_oversold
            self.verbose_log(symbol, "RSI oversold", rsi_ok,
                             rsi_now, f"<{self.rsi_oversold}", "long")
            if not rsi_ok:
                return None

            # ── 2. Stochastic slowK ────────────────────────────────────────
            low_min  = lows.rolling(self.stoch_k_period).min()
            high_max = highs.rolling(self.stoch_k_period).max()
            fastk    = 100 * (closes - low_min) / (high_max - low_min + 1e-10)
            slowk    = fastk.rolling(self.stoch_d_period).mean()
            slowk_now= slowk.iloc[-1]

            stoch_ok = slowk_now < self.stoch_oversold
            self.verbose_log(symbol, "Stoch slowK oversold", stoch_ok,
                             slowk_now, f"<{self.stoch_oversold}", "long")
            if not stoch_ok:
                return None

            # ── 3. Price below lower Bollinger Band ───────────────────────
            typical  = (highs + lows + closes) / 3
            bb_mid   = typical.rolling(self.bb_period).mean()
            bb_std   = typical.rolling(self.bb_period).std()
            bb_lower = bb_mid - self.bb_std * bb_std
            price_now  = closes.iloc[-1]
            bb_low_now = bb_lower.iloc[-1]

            bb_ok = price_now < bb_low_now
            self.verbose_log(symbol, "Price below lower BB", bb_ok,
                             price_now, f"<{bb_low_now:.4f}", "long")
            if not bb_ok:
                return None

            # ── 4. Hammer candlestick ────────────────────────────────────
            hammer_ok = self._detect_hammer(
                opens.iloc[-1], highs.iloc[-1],
                lows.iloc[-1],  closes.iloc[-1]
            )
            self.verbose_log(symbol, "Hammer candle", hammer_ok,
                             1 if hammer_ok else 0, "==1", "long")
            if not hammer_ok:
                return None

            # ── 5. OBV rising (buying pressure confirming reversal) ────────
            obv = self._calc_obv(closes, volumes)
            obv_vals = obv.iloc[-(self.obv_rising_bars + 1):].values
            obv_rising = all(
                obv_vals[i] < obv_vals[i + 1]
                for i in range(len(obv_vals) - 1)
            )

            self.verbose_log(symbol, f"OBV rising ({self.obv_rising_bars} bars)",
                             obv_rising,
                             round(float(obv.iloc[-1]), 0),
                             "rising", "long")
            if not obv_rising:
                logger.debug(
                    f"[HammerReversal] {symbol}: SKIP — OBV not rising "
                    f"(last {self.obv_rising_bars} bars: "
                    f"{[round(float(v), 0) for v in obv_vals]})"
                )
                return None

            # ── All 5 conditions met — compute dynamic score ──────────────
            rsi_scaled = 0.1 * (rsi_now - 50)
            fisher_rsi = (np.exp(2 * rsi_scaled) - 1) / (np.exp(2 * rsi_scaled) + 1)

            # Dynamic score: deeper RSI + deeper Stoch = stronger signal
            # Both components scaled 0.0–0.5, clamped to [0.65, 1.0]
            rsi_component   = max(0.0, min(0.5,
                (self.rsi_oversold - rsi_now) / self.rsi_oversold * 0.5
            ))
            stoch_component = max(0.0, min(0.5,
                (self.stoch_oversold - slowk_now) / self.stoch_oversold * 0.5
            ))
            score = max(0.65, min(1.0, 0.65 + rsi_component + stoch_component))

            reason = (
                f"Hammer Reversal: RSI={rsi_now:.1f} "
                f"SlowK={slowk_now:.1f} "
                f"Price={price_now:.4f}<BB={bb_low_now:.4f} "
                f"OBV rising✓ "
                f"Fisher={fisher_rsi:.3f} "
                f"Score={score:.3f}"
            )

            logger.info(f"[HammerReversal] Signal: {symbol} LONG | {reason}")

            return self._make_signal(
                symbol          = symbol,
                direction       = "long",
                score           = score,
                reason          = reason,
                stop_loss_pct   = self.stop_loss_pct,
                take_profit_pct = self.take_profit_pct,
                metadata        = {
                    "rsi":          round(rsi_now, 2),
                    "slowk":        round(slowk_now, 2),
                    "bb_lower":     round(bb_low_now, 4),
                    "fisher_rsi":   round(fisher_rsi, 4),
                    "hammer":       True,
                    "obv_rising":   True,
                    "obv_value":    round(float(obv.iloc[-1]), 0),
                    "roi_ladder":   self.roi_ladder,
                }
            )

        except Exception as e:
            logger.error(f"[HammerReversal] Error analyzing {symbol}: {e}",
                         exc_info=config.VERBOSE_MODE)
            return None

    def _detect_hammer(
        self,
        open_: float, high: float,
        low: float,   close: float
    ) -> bool:
        """
        Detect a hammer candlestick.
        - Small real body (<=30% of range) in upper portion
        - Long lower shadow (>=2x body)
        - Little or no upper shadow (<=10% of range)
        """
        body      = abs(close - open_)
        range_    = high - low

        if range_ < 1e-10:
            return False

        body_top    = max(open_, close)
        body_bottom = min(open_, close)
        upper_shadow= high - body_top
        lower_shadow= body_bottom - low

        small_body  = body <= range_ * 0.3
        long_lower  = lower_shadow >= body * 2.0
        small_upper = upper_shadow <= range_ * 0.1
        body_upper  = body_top >= low + range_ * 0.6

        return small_body and long_lower and small_upper and body_upper
