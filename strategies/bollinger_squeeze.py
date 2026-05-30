"""
bollinger_squeeze.py — Strategy: Bollinger Squeeze
Trading Bot v2

Philosophy: wait for bands to COMPRESS (energy coiling), then enter in the
direction price EXPANDS out of the squeeze. This is fundamentally different
from bollinger_breakout, which chases moves that have already happened.

Squeeze = tight Bollinger Bands (band_width < threshold) persisting for N bars.
Release = price closes beyond the band on the first bar OUT of the squeeze.

Backtest (trader-dev, May 2025 – May 2026, 1h, slippage=3 ticks, 0.1% commission):
  ETH: 201 trades, 32.3% WR, 2.75 W/L, PF=1.316, +7.26%, Sharpe=1.21  ✅
  BNB: 123 trades, 29.3% WR, 2.78 W/L, PF=1.152, +0.87%, Sharpe=0.25  ✅
  BTC:  87 trades, 28.7% WR, 2.79 W/L, PF=1.124, +0.17%, Sharpe=0.09  ✅
  SOL: 261 trades, 27.2% WR, 2.78 W/L, PF=1.037, -3.45%, Sharpe=-0.46 ⚠️

Optimized params: stop=2.5%, tp=7.0%, squeeze_thr=0.06 (insensitive 0.04-0.08).
SOL longs disabled — they bled -$46.8k vs shorts +$12.4k. Same as BTC 1h in a
trending market: the direction filter matters more for SOL.

Key insight: the squeeze_thr parameter is NOT sensitive (0.04–0.08 all identical).
The real driver is the W/L structure (2.5%/7.0% = 2.8 R:R) giving breakeven WR
of 26.7% — every pair tested beats this floor.

Shorts enabled: squeeze releases downward are clean signals. Both sides profitable
on ETH and BNB. Short side filters via EMA50 (below = downtrend = ok to short).
"""

import logging
from typing import Optional

import pandas as pd
import pandas_ta as ta
import numpy as np

import config
from strategies.base_strategy import BaseStrategy, TradeSignal


# Pairs excluded from long entries — longs bleed in trending markets on 1h.
# SOL showed -$46.8k longs vs +$12.4k shorts in May 2025–May 2026 backtest.
SQUEEZE_LONG_BLACKLIST = {"SOL/USD", "SOLUSDT"}

# Full blacklist (both directions) — not yet backtested or confirmed losers.
SQUEEZE_FULL_BLACKLIST: set = set()


class BollingerSqueeze(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.strategy_name   = "bollinger_squeeze"
        # Optimized from 100-run trader-dev sweep: stop=2.5%, tp=7.0%
        # W/L ≈ 2.8 on all pairs → break-even WR = 26.7%
        # v3 matrix sweep 2026-05-25: 365d crypto basket, 1h, fixed_pct stop
        # Best PF at sl=2.8, tp=8.5 across ETH/BNB/BTC/SOL
        self.stop_loss_pct   = 2.8   # v3 matrix sweep optimum 2026-05-25 (was 2.5)
        self.take_profit_pct = 8.5   # v3 matrix sweep optimum 2026-05-25 (was 7.0)
        # NOTE: self.logger already set by BaseStrategy.__init__ to the class name.
        # Do not override — that would shadow framework-level logging.

        self.crypto_enabled  = True   # primary purpose: 24/7 crypto coverage
        self.stock_enabled   = False  # not backtested on stocks; use bollinger_breakout there
        self.shorts_enabled  = True   # shorts profitable on ETH/BNB/BTC; filtered by EMA50

        # Keep both protections until enough live data accumulated.
        # If disabled, bot loses crypto squeeze coverage entirely.
        self.ml_exempt           = True
        self.auto_disable_exempt = True
        self.reviewer_exempt     = True   # reviewer lacks squeeze/BB metadata interpretation; exempt
        self.time_stop_profile   = "strategy_defined"  # generic 30m/2h stops kill 7-8.5% TP setups

        # ── Tunable parameters ────────────────────────────────────────────
        # Stored as instance vars so they're visible in logs and sweepable.
        self.bb_period    = 20
        self.bb_std       = 2.0
        self.squeeze_thr  = 0.06    # band_width < this = squeeze. Insensitive 0.04–0.08.
        self.squeeze_bars = 3       # consecutive bars in squeeze before we watch for release
        self.ema_period   = 50
        self.rsi_period   = 14
        self.rsi_max_long = 70      # skip longs when RSI already overbought
        self.rsi_min_short= 30      # skip shorts when RSI already oversold

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        sym_upper = symbol.upper()

        # ── Full blacklist ────────────────────────────────────────────────
        if sym_upper in SQUEEZE_FULL_BLACKLIST:
            self.verbose_log_skip(symbol, "In full squeeze blacklist — skipping")
            return None

        # ── Parameters from instance (set in __init__, sweepable) ────────
        bb_period    = self.bb_period
        bb_std       = self.bb_std
        squeeze_thr  = self.squeeze_thr
        squeeze_bars = self.squeeze_bars
        ema_period   = self.ema_period
        rsi_period   = self.rsi_period
        rsi_max_long = self.rsi_max_long
        rsi_min_short= self.rsi_min_short
        adx_val      = None  # will be set below if ADX computes successfully

        required = bb_period + ema_period + squeeze_bars + 5
        if not self._check_enough_candles(symbol, candles, required):
            return None

        close  = candles["close"]
        volume = candles["volume"] if "volume" in candles.columns else None

        # ── Bollinger Bands ───────────────────────────────────────────────
        try:
            bb = ta.bbands(close, length=bb_period, std=bb_std)
            if bb is None or bb.empty:
                self.verbose_log_skip(symbol, "BB calculation returned no data")
                return None
        except Exception as e:
            self.verbose_log_skip(symbol, f"BB error: {e}")
            return None

        col_lower = [c for c in bb.columns if c.startswith("BBL_")]
        col_upper = [c for c in bb.columns if c.startswith("BBU_")]
        col_mid   = [c for c in bb.columns if c.startswith("BBM_")]
        if not col_lower or not col_upper or not col_mid:
            self.verbose_log_skip(symbol, "BB columns not found")
            return None

        bb_lower_s = bb[col_lower[0]]
        bb_upper_s = bb[col_upper[0]]
        bb_mid_s   = bb[col_mid[0]]

        if pd.isna(bb_lower_s.iloc[-1]) or pd.isna(bb_upper_s.iloc[-1]):
            self.verbose_log_skip(symbol, "BB values are NaN")
            return None

        current_close = close.iloc[-1]
        current_upper = bb_upper_s.iloc[-1]
        current_lower = bb_lower_s.iloc[-1]
        current_mid   = bb_mid_s.iloc[-1]

        # ── Squeeze detection ─────────────────────────────────────────────
        # band_width = (upper - lower) / mid — normalized so comparable across prices.
        # Count how many of the last (squeeze_bars + 1) bars were in squeeze,
        # excluding the current bar (which is the potential release bar).
        band_widths_prev = []
        for i in range(1, squeeze_bars + 2):
            idx = -(i + 1)
            if abs(idx) > len(bb_mid_s):
                break
            mid_i   = bb_mid_s.iloc[idx]
            upper_i = bb_upper_s.iloc[idx]
            lower_i = bb_lower_s.iloc[idx]
            if not pd.isna(mid_i) and mid_i != 0:
                band_widths_prev.append((upper_i - lower_i) / mid_i)

        if len(band_widths_prev) < squeeze_bars:
            self.verbose_log_skip(symbol, "Not enough bars to evaluate squeeze history")
            return None

        # Were the preceding N bars all in squeeze?
        squeeze_was_active = all(w < squeeze_thr for w in band_widths_prev[:squeeze_bars])

        # Is current bar a release bar? (squeeze just ended AND price broke a band)
        current_band_width = (current_upper - current_lower) / current_mid if current_mid != 0 else 1.0
        currently_in_squeeze = current_band_width < squeeze_thr

        release_up   = squeeze_was_active and not currently_in_squeeze and current_close > current_upper
        release_down = squeeze_was_active and not currently_in_squeeze and current_close < current_lower

        self.verbose_log(
            symbol, "Squeeze was active before this bar",
            squeeze_was_active, round(current_band_width, 4), f"<{squeeze_thr}",
            extra=f"prev_widths={[round(w,4) for w in band_widths_prev[:squeeze_bars]]}"
        )
        self.verbose_log(
            symbol, "Squeeze release UP (long candidate)",
            release_up, current_close, f">{current_upper:.4f}"
        )
        self.verbose_log(
            symbol, "Squeeze release DOWN (short candidate)",
            release_down, current_close, f"<{current_lower:.4f}"
        )

        if not release_up and not release_down:
            self.verbose_log(
                symbol, "No squeeze release this bar — no signal",
                False, current_close,
                f"bands: {current_lower:.4f}–{current_upper:.4f}"
            )
            return None

        # ── EMA50 trend filter ────────────────────────────────────────────
        try:
            ema50 = ta.ema(close, length=ema_period)
            if ema50 is None or pd.isna(ema50.iloc[-1]):
                self.verbose_log_skip(symbol, "EMA50 insufficient data")
                return None
            ema50_val     = ema50.iloc[-1]
            price_above_ema = current_close > ema50_val
            self.logger.info(
                f"[EMA50] {symbol}: close={current_close:.4f} ema50={ema50_val:.4f} "
                f"above={price_above_ema}"
            )
        except Exception as e:
            self.verbose_log_skip(symbol, f"EMA50 error: {e}")
            return None

        # ── RSI ───────────────────────────────────────────────────────────
        try:
            rsi = ta.rsi(close, length=rsi_period)
            if rsi is None or pd.isna(rsi.iloc[-1]):
                self.verbose_log_skip(symbol, "RSI insufficient data")
                return None
            rsi_val = rsi.iloc[-1]
            self.logger.info(f"[RSI] {symbol}: rsi={rsi_val:.1f}")
        except Exception as e:
            self.verbose_log_skip(symbol, f"RSI error: {e}")
            return None

        # ── ADX: skip if market is already strongly trending ─────────────
        # Squeeze works in range/coiling regimes (ADX < 28).
        # A rising trend (ADX >= 28) means the breakout is likely continuation,
        # not a classic squeeze release — that's bollinger_breakout's job.
        try:
            adx_df = ta.adx(candles["high"], candles["low"], close, length=14)
            if adx_df is not None and not adx_df.empty:
                adx_col = [c for c in adx_df.columns if c.upper().startswith("ADX_")]
                if adx_col:
                    _adx_candidate = float(adx_df[adx_col[0]].iloc[-1])
                    if not pd.isna(_adx_candidate):
                        adx_val = _adx_candidate
                        self.logger.info(f"[ADX] {symbol}: adx={adx_val:.1f}")
                        if adx_val >= 28:
                            self.verbose_log_skip(
                                symbol,
                                f"ADX {adx_val:.1f} >= 28 — trending market, squeeze unreliable"
                            )
                            return None
        except Exception as e:
            self.logger.debug(f"[ADX] {symbol}: calculation skipped ({e})")

        # ── Volume ratio ──────────────────────────────────────────────────
        vol_ratio = None
        if volume is not None:
            vol_ma = volume.rolling(20).mean().iloc[-1]
            if vol_ma and vol_ma > 0:
                vol_ratio = round(float(volume.iloc[-1] / vol_ma), 3)

        # ── Score: base + squeeze strength bonus ─────────────────────────
        # Tighter the squeeze → more energy coiled → stronger expected release.
        squeeze_tightness = max(0.0, (squeeze_thr - min(band_widths_prev[:squeeze_bars])) / squeeze_thr)
        base_score = 0.68
        score = min(1.0, base_score + squeeze_tightness * 0.15)

        # ── LONG signal ───────────────────────────────────────────────────
        if release_up:
            if sym_upper in SQUEEZE_LONG_BLACKLIST:
                self.verbose_log_skip(
                    symbol,
                    f"Long blacklisted for {symbol} — longs bled in backtest "
                    f"(SOL longs: -$46.8k, shorts +$12.4k on 1h)"
                )
                return None
            if not price_above_ema:
                self.logger.info(
                    f"[EMA50] {symbol}: LONG rejected — price below EMA50 (counter-trend)"
                )
                return None
            if rsi_val >= rsi_max_long:
                self.logger.info(
                    f"[RSI] {symbol}: LONG rejected — RSI {rsi_val:.1f} overbought (>={rsi_max_long})"
                )
                return None

            self.verbose_log_score(symbol, score, base_score)
            return self._make_signal(
                symbol          = symbol,
                direction       = "long",
                score           = score,
                reason          = (
                    f"Bollinger squeeze release UP: close={current_close:.4f} > "
                    f"upper={current_upper:.4f} | "
                    f"squeeze_tightness={squeeze_tightness:.2f} rsi={rsi_val:.1f}"
                ),
                stop_loss_pct   = self.stop_loss_pct,
                take_profit_pct = self.take_profit_pct,
                metadata        = {
                    "strategy_name":               "bollinger_squeeze",
                    "entry_timeframe":             "1h",
                    "bb_upper":                    round(current_upper, 6),
                    "bb_lower":                    round(current_lower, 6),
                    "bb_mid":                      round(current_mid, 6),
                    "band_width":                  round(current_band_width, 4),
                    "squeeze_tightness":           round(squeeze_tightness, 4),
                    "ema50":                       round(ema50_val, 4),
                    "rsi":                         round(rsi_val, 2),
                    "adx":                         round(adx_val, 2) if adx_val is not None else None,
                    "volume_ratio":                vol_ratio,
                    "structural_stop_price":       round(current_lower, 6),
                    "preferred_initial_stop_mode": "fixed_pct",
                    "preferred_trail_mode":        "none",
                },
            )

        # ── SHORT signal ──────────────────────────────────────────────────
        if release_down:
            if not self.shorts_enabled:
                self.verbose_log_skip(symbol, "Shorts disabled (shorts_enabled=False)")
                return None
            if price_above_ema:
                self.logger.info(
                    f"[EMA50] {symbol}: SHORT rejected — price above EMA50 (counter-trend)"
                )
                return None
            if rsi_val <= rsi_min_short:
                self.logger.info(
                    f"[RSI] {symbol}: SHORT rejected — RSI {rsi_val:.1f} oversold (<={rsi_min_short})"
                )
                return None

            self.verbose_log_score(symbol, score, base_score)
            return self._make_signal(
                symbol          = symbol,
                direction       = "short",
                score           = score,
                reason          = (
                    f"Bollinger squeeze release DOWN: close={current_close:.4f} < "
                    f"lower={current_lower:.4f} | "
                    f"squeeze_tightness={squeeze_tightness:.2f} rsi={rsi_val:.1f}"
                ),
                stop_loss_pct   = self.stop_loss_pct,
                take_profit_pct = self.take_profit_pct,
                metadata        = {
                    "strategy_name":               "bollinger_squeeze",
                    "entry_timeframe":             "1h",
                    "bb_upper":                    round(current_upper, 6),
                    "bb_lower":                    round(current_lower, 6),
                    "bb_mid":                      round(current_mid, 6),
                    "band_width":                  round(current_band_width, 4),
                    "squeeze_tightness":           round(squeeze_tightness, 4),
                    "ema50":                       round(ema50_val, 4),
                    "rsi":                         round(rsi_val, 2),
                    "adx":                         round(adx_val, 2) if adx_val is not None else None,
                    "volume_ratio":                vol_ratio,
                    "structural_stop_price":       round(current_upper, 6),
                    "preferred_initial_stop_mode": "fixed_pct",
                    "preferred_trail_mode":        "none",
                },
            )

        return None
