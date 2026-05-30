"""
adaptive_regime.py — Strategy 12: Adaptive Regime (Trend-Long Stock Edition)
Trading Bot v2

This file was originally a dual-mode adaptive strategy that switched between:
  - trend breakouts
  - mean reversion trades

Audit results showed the profitable, repeatable slice was:
  - stocks
  - 1h timeframe
  - trend mode
  - long side only

This refactor converts the strategy into a clean stock-only trend-following model.

ENTRY:
  ADX >= 25
  EMA20 > EMA100
  close > prior-bar 20-bar Donchian high
  RSI <= 68
  volume_ratio >= 1.1

RISK:
  Initial stop = ATR * 2.5 below entry
  Backstop TP  = ATR * 3.0 above entry

EXIT:
  EMA20 <= EMA100  -> adaptive_ema_cross_exit

Notes:
  - strategy_name remains "adaptive_regime" for framework compatibility
  - no mean reversion logic remains
  - no short entries remain
  - crypto disabled

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRIOR AUDIT HISTORY (dual-mode phase) — archived for reference
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STATUS: DISABLED → RE-ENABLED AS SPECIALIST (2026-05-16)

ROOT CAUSE OF PRIOR FAILURE: Structural dual-mode flaw. Mean-rev and trend
entries shared a single position slot. The mean-rev long component showed
22–33% WR across the entire basket — worse than random. BB lower touches in
2024 crypto bull markets are frequently continuation (dip → correction), not
reversal. The 1.5×ATR SL was routinely hit before recovery.

DECISION: Salvage the profitable slice (trend-long stocks) and discard the rest.
Mean-reversion logic removed. Short entries removed. Crypto removed.

ITERATION CHANGELOG (dual-mode audit, crypto):
  Iter0  baseline: 10 trades, 80% WR, +0.51% — EMA100 proximity gate was
         blocking almost all mean-rev signals (geometric impossibility).
  Iter1  (gate removed): 88 trades, 62.5% WR, -1.80%
  Iter2  (+200 SMA macro filter): 27 trades, 63% WR, -1.15%
  Iter3–5 (time stop + exit fixes): Structural flaw confirmed — mean-rev longs
         consistently fail, mean-rev shorts inconsistent
  Iter6  (mean-rev longs disabled): BTC PF=1.17, ETH PF=0.37, SOL PF=12.68
         (outlier, few trades), BNB PF=0.33. No basket-wide positive edge.

CONVERSION (2026-05-16):
  - Removed mean-reversion mode entirely
  - Removed trend short entries
  - Disabled crypto (stock-only)
  - Tightened ADX threshold: 22 → 25 (cleaner trend filter)
  - Tightened Donchian lookback: 12 → 20 (more meaningful channel)
  - Tightened RSI max: 72 → 68 (less exhausted entries)
  - Raised volume_ratio gate: 1.0 → 1.1 (stronger breakout confirmation)
  - candle_limit: 150 → 180 (EMA100 + buffer)

FRAMEWORK PARITY (applied prior session):
  - adaptive_regime in TIME_STOP_EXEMPT (backtester.py) ✅
  - position_monitor delegates to check_custom_exit() ✅
  - time_stop_profile = "strategy_defined" ✅
  - preferred_initial_stop_mode = "signal_structural" ✅
  - preferred_trail_mode = "two_bar" ✅
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Stop model:
  Initial stop is ATR-based (structural) — set in signal metadata.
  Trailing uses the standard two-bar structural engine via position_monitor.

Exit model:
  Strategy-specific exit implemented in check_custom_exit() here and
  mirrored in position_monitor._check_adaptive_regime_exit() which
  delegates directly to this method for live/backtest parity.

Timeframe: 1h stocks only.
"""

import logging
from typing import Optional

import pandas as pd
import pandas_ta as ta

import config
from strategies.base_strategy import BaseStrategy, TradeSignal

logger = logging.getLogger(__name__)

# ── Parameters ───────────────────────────────────────────────────────────────
FAST_EMA          = 20
SLOW_EMA          = 100
ADX_PERIOD        = 14
RSI_PERIOD        = 14
ATR_PERIOD        = 14
VOLUME_PERIOD     = 20
BREAKOUT_PERIOD   = 20    # Donchian channel lookback — tightened from 12

ADX_TREND_THRESHOLD = 25  #  ADX >= this → strong trend gate (raised from 22)
STOP_ATR_MULT       = 2.5 # Initial stop distance below entry
TRAIL_ATR_MULT      = 3.0 # Backstop TP (primary exit is EMA cross)
MAX_TREND_ENTRY_RSI = 68  #  Block exhausted entries (tightened from 72)
MIN_VOL_RATIO       = 1.1 #  Minimum volume_ratio for breakout confirmation

MIN_BARS = max(
    SLOW_EMA,
    ADX_PERIOD,
    RSI_PERIOD,
    ATR_PERIOD,
    VOLUME_PERIOD,
    BREAKOUT_PERIOD + 1,
) + 10


class AdaptiveRegime(BaseStrategy):
    """
    Adaptive Regime — Trend-Long Stock-Only Edition.
    ADX + EMA alignment + Donchian breakout on 1h stock bars.
    """

    def __init__(self):
        super().__init__()
        self.strategy_name   = "adaptive_regime"
        self.stop_loss_pct   = 1.5   # v3 matrix sweep optimum 2026-05-25
                                     # 90d stock basket, 1h, signal_structural + two_bar trail
                                     # NVDA/META/MSFT/AAPL — best PF at sl=1.5, tp=3.2
        self.take_profit_pct = 3.2   # v3 matrix sweep optimum 2026-05-25 (was 3.0)

        self.stock_candle_timeframe  = "1Hour"
        self.crypto_candle_timeframe = "1h"
        self.candle_limit            = 180   # EMA100 needs ~100 + generous buffer

        self.stock_enabled  = True
        self.crypto_enabled = False   # conversion 2026-05-16: crypto disabled
        self.enabled        = True    # re-enabled as stock-only specialist

        # Temporary auto-disable protection — 2026-05-16.
        # strategy_results had 13 rows at 15.4% WR from the old dual-mode runs.
        # Those records were cleared on enable; this flag prevents re-disable
        # while fresh paper-trade data accumulates.
        # Set False once 50+ post-2026-05-16 trades are logged.
        self.auto_disable_exempt = True      # prevent auto-disable while fresh data accumulates post 2026-05-16 redesign
        self.ml_exempt           = True     # ML model dominated by grid_bot (1507 trades) — prior 14 adaptive trades invalid
        self.reviewer_exempt     = True     # reviewer may suppress valid breakouts under bearish sentiment; exempt pending paper data

        self.time_stop_profile           = "strategy_defined"
        self.preferred_initial_stop_mode = "signal_structural"
        self.preferred_trail_mode        = "two_bar"

        self._reset_diagnostics()

        logger.info(
            f"[{self.strategy_name}] Initialized — trend-long stock-only mode. "
            f"ADX>={ADX_TREND_THRESHOLD}, EMA{FAST_EMA}>EMA{SLOW_EMA}, "
            f"Donchian {BREAKOUT_PERIOD}-bar breakout, "
            f"RSI<={MAX_TREND_ENTRY_RSI}, vol_ratio>={MIN_VOL_RATIO}, 1h stocks only."
        )

    # ─────────────────────────────────────────────────────────────────────────
    # DIAGNOSTICS  (lightweight counters — no effect on strategy behaviour)
    # ─────────────────────────────────────────────────────────────────────────

    def _reset_diagnostics(self):
        """Zero all per-run counters. Called by backtester before each symbol."""
        self.diagnostics = {
            # Regime bar distribution
            "bars_analyzed":   0,
            "trend_bars":      0,    # ADX >= ADX_TREND_THRESHOLD
            "range_bars":      0,    # (unused — no mean-rev mode; kept for compat)
            "transition_bars": 0,    # ADX below threshold — no signal zone

            # Trend-mode signal counts
            "trend_long":      0,
            "trend_short":     0,    # always 0 — shorts removed

            # Trend-mode blocker counts
            "trend_vol_fail":  0,    # volume_ratio < MIN_VOL_RATIO
            "trend_ema_fail":  0,    # EMA alignment not bullish
            "trend_don_fail":  0,    # price did not break Donchian high
            "trend_rsi_fail":  0,    # RSI too extended

            # Mean-rev keys — kept for backtester diagnostic compat (always 0)
            "mean_long":       0,
            "mean_short":      0,
            "mean_vol_fail":   0,
            "mean_bb_fail":    0,
            "mean_rsi_fail":   0,
        }

    def get_diagnostics(self) -> dict:
        """Return a copy of the current diagnostic counters."""
        return dict(self.diagnostics)

    # ─────────────────────────────────────────────────────────────────────────
    # INDICATOR HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _calc_adx(self, high: pd.Series, low: pd.Series,
                  close: pd.Series) -> Optional[float]:
        try:
            adx_df = ta.adx(high, low, close, length=ADX_PERIOD)
            if adx_df is None or adx_df.empty:
                return None
            col = [c for c in adx_df.columns if c.startswith("ADX_")]
            if not col:
                return None
            val = adx_df[col[0]].iloc[-1]
            return float(val) if not pd.isna(val) else None
        except Exception:
            return None

    def _calc_atr(self, high: pd.Series, low: pd.Series,
                  close: pd.Series) -> Optional[float]:
        try:
            atr_s = ta.atr(high, low, close, length=ATR_PERIOD)
            if atr_s is None or atr_s.empty:
                return None
            val = atr_s.iloc[-1]
            return float(val) if not pd.isna(val) else None
        except Exception:
            return None

    def _calc_rsi(self, close: pd.Series) -> Optional[float]:
        try:
            rsi_s = ta.rsi(close, length=RSI_PERIOD)
            if rsi_s is None or rsi_s.empty:
                return None
            val = rsi_s.iloc[-1]
            return float(val) if not pd.isna(val) else None
        except Exception:
            return None

    def _calc_macd_hist(self, close: pd.Series) -> Optional[float]:
        """Return MACD histogram value (fast=12, slow=26, signal=9)."""
        try:
            macd_df  = ta.macd(close, fast=12, slow=26, signal=9)
            if macd_df is None or macd_df.empty:
                return None
            hist_col = [c for c in macd_df.columns if "MACDh" in c]
            if not hist_col:
                return None
            val = macd_df[hist_col[0]].iloc[-1]
            return round(float(val), 6) if not pd.isna(val) else None
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        if not self.enabled:
            return None

        # Stock-only — crypto symbols contain "/"
        if "/" in symbol:
            self.verbose_log_skip(symbol, "Stock-only version — crypto disabled")
            return None

        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None

        close  = candles["close"].astype(float)
        high   = candles["high"].astype(float)
        low    = candles["low"].astype(float)
        volume = candles["volume"].astype(float)
        current_close = float(close.iloc[-1])

        _d = self.diagnostics
        _d["bars_analyzed"] += 1

        # ── ADX trend-strength gate ──────────────────────────────────────────
        adx = self._calc_adx(high, low, close)
        if adx is None:
            self.verbose_log_skip(symbol, "ADX unavailable")
            return None

        if adx < ADX_TREND_THRESHOLD:
            _d["transition_bars"] += 1
            self.verbose_log_skip(
                symbol,
                f"ADX {adx:.1f} below trend threshold {ADX_TREND_THRESHOLD}"
            )
            return None

        _d["trend_bars"] += 1

        # ── EMA alignment: EMA20 must be above EMA100 ────────────────────────
        try:
            ema_fast_s = ta.ema(close, length=FAST_EMA)
            ema_slow_s = ta.ema(close, length=SLOW_EMA)
            if ema_fast_s is None or ema_slow_s is None:
                self.verbose_log_skip(symbol, "EMA calculation failed")
                return None
            ema_fast = float(ema_fast_s.iloc[-1])
            ema_slow = float(ema_slow_s.iloc[-1])
        except Exception as ex:
            self.verbose_log_skip(symbol, f"EMA error: {ex}")
            return None

        if ema_fast <= ema_slow:
            _d["trend_ema_fail"] += 1
            self.verbose_log_skip(
                symbol,
                f"EMA trend not aligned: EMA{FAST_EMA}={ema_fast:.4f} "
                f"<= EMA{SLOW_EMA}={ema_slow:.4f}"
            )
            return None

        # ── Volume participation ─────────────────────────────────────────────
        vol_sma      = float(volume.iloc[-VOLUME_PERIOD:].mean())
        current_vol  = float(volume.iloc[-1])
        volume_ratio = round(current_vol / vol_sma, 3) if vol_sma > 0 else 1.0

        if volume_ratio < MIN_VOL_RATIO:
            _d["trend_vol_fail"] += 1
            self.verbose_log_skip(
                symbol,
                f"Volume ratio {volume_ratio:.3f} < {MIN_VOL_RATIO} — "
                f"insufficient breakout participation"
            )
            return None

        # ── ATR for dynamic stop sizing ──────────────────────────────────────
        atr = self._calc_atr(high, low, close)
        if atr is None or atr <= 0:
            self.verbose_log_skip(symbol, "ATR unavailable")
            return None

        # ── RSI exhaustion filter ────────────────────────────────────────────
        rsi = self._calc_rsi(close)
        if rsi is None:
            self.verbose_log_skip(symbol, "RSI unavailable")
            return None

        if rsi > MAX_TREND_ENTRY_RSI:
            _d["trend_rsi_fail"] += 1
            self.verbose_log_skip(
                symbol,
                f"RSI {rsi:.1f} above max trend-entry RSI {MAX_TREND_ENTRY_RSI}"
            )
            return None

        # ── Donchian breakout confirmation ───────────────────────────────────
        # Use prior bar's N-bar high (iloc[-2]) — no lookahead.
        don_high = float(high.rolling(BREAKOUT_PERIOD).max().iloc[-2])

        if current_close <= don_high:
            _d["trend_don_fail"] += 1
            self.verbose_log_skip(
                symbol,
                f"Close {current_close:.4f} not above {BREAKOUT_PERIOD}-bar "
                f"Donchian high {don_high:.4f}"
            )
            return None

        # ── Build long signal ────────────────────────────────────────────────
        stop_price = current_close - (atr * STOP_ATR_MULT)
        if stop_price >= current_close:
            self.verbose_log_skip(symbol, "ATR stop invalid (above entry)")
            return None

        sl_pct = (current_close - stop_price) / current_close * 100
        tp_pct = (atr * TRAIL_ATR_MULT) / current_close * 100
        tp_pct = max(1.0, min(15.0, tp_pct))   # backstop TP; real exit is EMA cross

        macd_hist = self._calc_macd_hist(close)

        # Score: base 0.72, small ADX bonus + volume participation bonus — capped at 1.0
        vol_bonus = min(0.05, (volume_ratio - MIN_VOL_RATIO) * 0.03)
        score = min(1.0, 0.72 + (adx - ADX_TREND_THRESHOLD) * 0.002 + vol_bonus)
        self.verbose_log_score(symbol, score, 0.65)

        _d["trend_long"] += 1

        logger.info(
            f"[AdaptiveRegime] {symbol}: TREND LONG STOCK | "
            f"close={current_close:.4f} > don_high={don_high:.4f} | "
            f"ADX={adx:.1f} EMA{FAST_EMA}={ema_fast:.4f}>EMA{SLOW_EMA}={ema_slow:.4f} | "
            f"RSI={rsi:.1f} vol_ratio={volume_ratio:.3f} ATR={atr:.4f} SL=${stop_price:.4f}"
        )

        return self._make_signal(
            symbol          = symbol,
            direction       = "long",
            score           = round(score, 3),
            reason          = (
                f"Adaptive trend long stock: close {current_close:.4f} > "
                f"Donchian {don_high:.4f} | ADX={adx:.1f} | "
                f"EMA{FAST_EMA}>EMA{SLOW_EMA} | RSI={rsi:.1f} | "
                f"vol_ratio={volume_ratio:.3f}"
            ),
            stop_loss_pct   = round(sl_pct, 3),
            take_profit_pct = round(tp_pct, 3),
            metadata        = {
                "adaptive_exit_mode":          "trend",
                "structural_stop_price":       round(stop_price, 6),
                "adx":                         round(adx, 2),
                "ema_fast":                    round(ema_fast, 4),
                "ema_slow":                    round(ema_slow, 4),
                "donchian_high":               round(don_high, 4),
                "atr":                         round(atr, 6),
                "rsi":                         round(rsi, 2),
                "regime":                      "trend_long_stock_only",
                "entry_timeframe":             "1h",
                "volume_ratio":                volume_ratio,
                "macd_hist":                   macd_hist,
                "preferred_initial_stop_mode": "signal_structural",
                "preferred_trail_mode":        "two_bar",
            }
        )

    # ─────────────────────────────────────────────────────────────────────────
    # CUSTOM EXIT HOOK  (called by backtester and live position_monitor)
    # ─────────────────────────────────────────────────────────────────────────

    def check_custom_exit(
        self,
        symbol: str,
        bars: pd.DataFrame,
        direction: str,
        entry_metadata: dict = None
    ):
        """
        Trend-long stock-only exit:
          Exit when EMA20 crosses below EMA100.

        This is the single source of truth for both backtester and live monitor.
        position_monitor._check_adaptive_regime_exit() delegates here directly.

        Returns:
          "adaptive_ema_cross_exit" or None
        """
        if bars is None or len(bars) < SLOW_EMA + 5:
            return None

        try:
            closes   = bars["close"].astype(float)
            ema20_s  = closes.ewm(span=FAST_EMA,  adjust=False).mean()
            ema100_s = closes.ewm(span=SLOW_EMA, adjust=False).mean()

            ema20  = float(ema20_s.iloc[-1])
            ema100 = float(ema100_s.iloc[-1])

            if ema20 <= ema100:
                return "adaptive_ema_cross_exit"

            return None

        except Exception:
            return None
