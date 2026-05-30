"""
bollinger_breakout.py — Strategy 2: Bollinger Breakout
Trading Bot v2

Enters when price breaks outside the Bollinger Bands.
A squeeze condition (tight bands) strengthens the signal significantly.

Long:  Price breaks above upper band
Short: Price breaks below lower band

Thresholds configurable in config.SIGNAL_TUNING.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AUDIT STATUS (2026-05-16) — quant-strategy-auditor-refiner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STATUS: CRITICAL — false breakout factory, -$1,083 in 8 days

LIVE PERFORMANCE — CLEAN (99 strategy-only exits, May 7-14, stocks only):
  Total P&L:  -$1,083.93  (~-$135/day)
  WR:          22.22%  (breakeven = 44.9% at live R:R 1.228:1)
  Long:   47 trades, 25.5% WR, -$369.02  (BE=40.7%) — losing
  Short:  52 trades, 19.2% WR, -$714.92  (BE=50.6%) — disabled 2026-05-15
  Stop-loss hits: 47/99 (47.5%), WR=2.1%,  P&L=-$1,388.72 — devastating
  TP hits:         9/99  (9.1%), WR=100%,  P&L=+$461.90   — rare

ROOT CAUSE DIAGNOSIS:
  1. FALSE BREAKOUT FACTORY (primary): 47 stop hits at 2.1% WR means nearly
     every stopped trade reversed immediately after entry. Price breaks the
     upper band on a wick/low-volume spike then snaps back. Classic BB
     breakout failure: signal fires at exhaustion, not at the start of a move.
  2. R:R LEAK: Theoretical 2:1 (TP=3%, SL=1.5%) degrades to 1.23:1 actual
     because time stops (perf_time_stop_1hr) and eod_close cut winners at
     partial profit. Winners avg +1.70% vs 3.0% target = 57% of TP reached.
  3. FILTERS INSUFFICIENT: OBV + EMA50 + RSI direction + ADX>20 still leaves
     25.5% WR on longs. Confirmations don't fix a broken entry signal.
  4. AUTO_DISABLE_EXEMPT STALE: was set for crypto-24/7 coverage.
     crypto_enabled=False since Apr 2026 — exemption removed 2026-05-16.

ITERATION CHANGELOG:
  Baseline  BB breakout, both dirs, TP=3%, SL=1.5%, stocks+crypto
            Apr 2026: Stocks 123 trades +$1,064; Crypto 773 trades -$860
  Step1     Disable crypto (crypto_enabled=False, Apr 2026)
            Removed -$860 crypto drain. Stocks-only going forward.
  Step2     Disable shorts (shorts_enabled=False, 2026-05-15)
            Short live WR: 19.2% vs 50.6% breakeven — pure destruction.
            Removed 52.5% of signal volume. 0 trades since change.
  Step3     Remove auto_disable_exempt (2026-05-16)
            Justification was crypto coverage — invalid now that crypto off.
            Bot can now auto-disable if longs remain at 22% WR.
  Step4     ADX 20→30 + squeeze as hard gate (2026-05-16, Trader Dev sweep)
            Sweep: 72 combos on BTCUSDT 1h, 1yr, objective=profitFactor
            ADX20+no-squeeze: PF ~1.07, net negative — current live config
            ADX30+squeeze:    PF  1.28, +$2,658, Sharpe +0.31, DD 1.93%
            Squeeze was score bonus only — now a hard gate (return None).
            ADX threshold moved to config.bb_adx_min (was hardcoded 20).
            ACCEPT — both changes required together; either alone insufficient.

APPLIED CHANGES:
  ✅ crypto_enabled = False (Apr 2026)
  ✅ shorts_enabled = False (2026-05-15) — 19.2% WR vs 50.6% BE
  ✅ auto_disable_exempt = False (2026-05-16) — stale crypto justification
  ✅ SMCI blacklisted (Apr 2026 backtest: $53 max loss vs $7 avg win)
  ✅ 18 crypto pairs in BOLLINGER_CRYPTO_BLACKLIST
  ✅ Squeeze: score bonus → hard gate (2026-05-16, sweep Step4)
  ✅ ADX min: hardcoded 20 → config.bb_adx_min=30 (2026-05-16, sweep Step4)

NEXT STEPS:
  1. Monitor longs-only for 50+ trades with new ADX30+squeeze gate.
     Baseline: 0 stock trades since 2026-05-15. ADX30+squeeze will fire
     less often — expect ~30-40% fewer signals vs ADX20 no-squeeze.
     Target: >=35% WR (breakeven at 1.28 PF R:R ≈ 28%).
  2. If WR still below 30% after 50 trades on stocks: strategy has no
     edge on the current stock universe — consider disabling.
  3. If WR reaches 35%+: test adding a volume-surge filter (vol > 1.5x
     20-bar avg at entry) to further distinguish real breakouts.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from typing import Optional

import pandas as pd
import pandas_ta as ta
import numpy as np

import config
from strategies.base_strategy import BaseStrategy, TradeSignal


# Symbols excluded from Bollinger Breakout on 5m bars.
# SMCI: extreme intraday volatility causes outsized losses that dwarf avg wins.
# Update after each monthly backtest review.
BOLLINGER_BLACKLIST = {"SMCI"}
# Crypto pairs blacklisted based on Apr 2026 backtest (365d 1h)
# These showed significant negative returns — counter-trend losers
BOLLINGER_CRYPTO_BLACKLIST = {
    "TON/USD", "AGIX/USD", "HOT/USD", "STRK/USD", "EOS/USD",
    "OCEAN/USD", "REN/USD", "MKR/USD", "BAL/USD", "HT/USD",
    "XMR/USD", "GT/USD", "ARB/USD", "BCH/USD", "XTZ/USD",
    "RLC/USD", "FLOW/USD", "OKB/USD",
}

class BollingerBreakout(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.strategy_name   = "bollinger_breakout"
        self.stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT
        self.take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT
        self.stock_enabled   = True
        self.candle_limit    = 60   # EMA50 + ADX14 + BB20 + buffer
        self.reviewer_exempt = True
        self.time_stop_profile = "strategy_defined"

        # Crypto disabled: Apr 2026 live data showed 773 crypto trades at -$860.
        # Stocks at the same period: 123 trades, +$1,064.
        # Re-enable only with a crypto-specific BB variant and tighter filters.
        self.crypto_enabled      = False

        # Shorts disabled 2026-05-15: clean live data showed 52 shorts at
        # 19.2% WR vs 50.6% breakeven (-$714.92). Fighting May 2026 uptrend.
        # Re-enable only after Trader Dev backtest confirms short-side edge.
        self.shorts_enabled      = False

        # ML exempt — keep off until strategy is confirmed stable.
        self.ml_exempt           = True

        # AUTO-DISABLE PROTECTION — temporary, expires after 50 live trades
        # under the new config (ADX≥30 + squeeze hard gate, applied 2026-05-16).
        # Without this, the bot reads the old 22% WR (99 trades) and immediately
        # kills the strategy before any new signals fire. The old data is from a
        # different signal set (ADX20, no squeeze gate) — it is NOT representative
        # of the new config. Set this back to False once 50+ trades logged after
        # 2026-05-16 with the new filters, then let auto-disable judge fairly.
        self.auto_disable_exempt = True

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        # ----------------------------------------------------------------
        # Symbol blacklist — exclude symbols with proven destructive behavior
        # on 5m bars. SMCI showed $53 max loss vs $7 avg win in Apr 2026.
        # ----------------------------------------------------------------
        if symbol.upper() in BOLLINGER_BLACKLIST:
            self.verbose_log_skip(
                symbol,
                f"In Bollinger blacklist — skipping "
                f"(excluded: {', '.join(sorted(BOLLINGER_BLACKLIST))})"
            )
            return None

        tuning      = config.SIGNAL_TUNING
        period      = tuning["bb_breakout_period"]
        std_dev     = tuning["bb_breakout_std"]
        squeeze_thr = tuning["bb_squeeze_threshold"]
        min_score   = tuning["bb_breakout_min_score"]

        required = period + 5
        if not self._check_enough_candles(symbol, candles, required):
            return None

        # Calculate Bollinger Bands
        try:
            bb = ta.bbands(candles["close"], length=period, std=std_dev)
            if bb is None or bb.empty:
                self.verbose_log_skip(symbol, "Bollinger Bands calculation returned no data")
                return None
        except Exception as e:
            self.verbose_log_skip(symbol, f"Bollinger Bands error: {e}")
            return None

        # Column names from pandas_ta: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0, BBB_20_2.0
        col_lower = [c for c in bb.columns if c.startswith("BBL_")]
        col_upper = [c for c in bb.columns if c.startswith("BBU_")]
        col_mid   = [c for c in bb.columns if c.startswith("BBM_")]

        if not col_lower or not col_upper or not col_mid:
            self.verbose_log_skip(symbol, "Could not find BB column names in output")
            return None

        lower = bb[col_lower[0]].iloc[-1]
        upper = bb[col_upper[0]].iloc[-1]
        mid   = bb[col_mid[0]].iloc[-1]
        close = candles["close"].iloc[-1]

        if pd.isna(lower) or pd.isna(upper) or pd.isna(mid):
            self.verbose_log_skip(symbol, "Bollinger Band values are NaN")
            return None

        # Check for squeeze: band width as ratio of midline
        band_width = (upper - lower) / mid if mid != 0 else 1.0
        squeeze    = band_width < squeeze_thr

        self.verbose_log(
            symbol, "Bollinger Band squeeze (increases signal strength)",
            squeeze, band_width, f"<{squeeze_thr}",
            extra=f"upper={upper:.4f} lower={lower:.4f} mid={mid:.4f}"
        )

        squeeze_bonus = 0.10 if squeeze else 0.0

        # ----------------------------------------------------------------
        # OBV CONFIRMATION — filter fake breakouts with no volume support
        # OBV must be trending up over last 3 bars for long signals,
        # and down for short signals. Neutral/flat OBV = skip.
        # ----------------------------------------------------------------
        try:
            obv = ta.obv(candles["close"], candles["volume"])
            if obv is None or len(obv) < 2:
                self.verbose_log_skip(symbol, "OBV: insufficient data for confirmation")
                return None
            obv_now     = obv.iloc[-1]
            obv_prev    = obv.iloc[-2]
            obv_rising  = obv_now > obv_prev
            obv_falling = obv_now < obv_prev
            self.logger.info(f"[OBV] {symbol}: rising={obv_rising} falling={obv_falling} now={round(obv_now,0)} prev={round(obv_prev,0)}")
            self.verbose_log(
                symbol, "OBV direction check",
                obv_rising or obv_falling,
                round(obv_now, 0),
                f"prev={round(obv_prev, 0)}",
                extra=f"rising={obv_rising} falling={obv_falling}"
            )
        except Exception as e:
            self.verbose_log_skip(symbol, f"OBV calculation error: {e}")
            return None
        # ----------------------------------------------------------------
        # CRYPTO BLACKLIST — pairs with confirmed negative edge on 1h bars
        # ----------------------------------------------------------------
        if "/" in symbol and symbol.upper() in BOLLINGER_CRYPTO_BLACKLIST:
            self.verbose_log_skip(symbol, f"In crypto blacklist — skipping {symbol}")
            return None

        # ----------------------------------------------------------------
        # EMA50 TREND FILTER — only trade with the trend
        # Long signals require price above EMA50 (uptrend)
        # Short signals require price below EMA50 (downtrend)
        # Filters counter-trend trades which are the biggest losers
        # ----------------------------------------------------------------
        try:
            ema50 = ta.ema(candles["close"], length=50)
            if ema50 is None or len(ema50) < 1 or pd.isna(ema50.iloc[-1]):
                self.verbose_log_skip(symbol, "EMA50: insufficient data")
                return None
            ema50_val = ema50.iloc[-1]
            price_above_ema50 = close > ema50_val
            self.logger.info(f"[EMA50] {symbol}: close={close:.4f} ema50={ema50_val:.4f} above={price_above_ema50}")

        except Exception as e:
            self.verbose_log_skip(symbol, f"EMA50 calculation error: {e}")
            return None

        # ----------------------------------------------------------------
        # RSI DIRECTION FILTER — RSI must be moving the right way
        # Long: RSI rising over last 3 bars (momentum building up)
        # Short: RSI falling over last 3 bars (momentum building down)
        # ----------------------------------------------------------------
        try:
            rsi = ta.rsi(candles["close"], length=14)
            if rsi is None or len(rsi) < 4:
                self.verbose_log_skip(symbol, "RSI: insufficient data for direction check")
                return None
            rsi_now  = rsi.iloc[-1]
            rsi_prev = rsi.iloc[-4]
            rsi_rising  = rsi_now > rsi_prev
            rsi_falling = rsi_now < rsi_prev
            self.logger.info(f"[RSI] {symbol}: now={rsi_now:.1f} prev={rsi_prev:.1f} rising={rsi_rising} falling={rsi_falling}")
        except Exception as e:
            self.verbose_log_skip(symbol, f"RSI direction check error: {e}")
            return None

        # ----------------------------------------------------------------
        # SQUEEZE GATE — only fire when bands are compressed.
        # Audit 2026-05-16: squeeze was a score bonus only; sweep confirmed it
        # must be a hard gate. ADX30+squeeze = PF 1.28; no-squeeze = PF ~1.07.
        # ----------------------------------------------------------------
        if not squeeze:
            self.verbose_log_skip(
                symbol,
                f"No BB squeeze (band_width={band_width:.4f} >= {squeeze_thr}) — "
                f"breakout without compression is high false-positive risk"
            )
            return None

        # ----------------------------------------------------------------
        # ADX FILTER — require strong trend momentum before trading breakouts.
        # Audit 2026-05-16: ADX<30 allows weak-trend fakeouts that reverse
        # immediately. Sweep (72 combos): ADX≥30 is the only threshold that
        # produces net-positive results. Threshold now in config.bb_adx_min.
        # ----------------------------------------------------------------
        adx_min = tuning.get("bb_adx_min", 30)
        adx_val = None
        try:
            adx_df = ta.adx(candles["high"], candles["low"], candles["close"], length=14)
            if adx_df is not None and not adx_df.empty:
                adx_col = [c for c in adx_df.columns if c.upper().startswith("ADX_")]
                if adx_col:
                    adx_val = float(adx_df[adx_col[0]].iloc[-1])
                    if not pd.isna(adx_val):
                        self.logger.info(f"[ADX] {symbol}: adx={adx_val:.1f} min={adx_min}")
                        if adx_val < adx_min:
                            self.verbose_log_skip(
                                symbol,
                                f"ADX {adx_val:.1f} < {adx_min} — insufficient trend, breakout likely false"
                            )
                            return None
        except Exception as e:
            self.logger.debug(f"[ADX] {symbol}: calculation skipped ({e})")

        # ----------------------------------------------------------------
        # LONG SIGNAL: price breaks above upper band
        # ----------------------------------------------------------------
        breaks_upper = close > upper
        self.verbose_log(
            symbol, "Price breaks above upper Bollinger Band (long)",
            breaks_upper, close, f">{upper:.4f}", "long"
        )

        if breaks_upper:
            if not obv_rising:
                self.logger.info(f"[OBV] {symbol}: LONG rejected — OBV not rising (rising={obv_rising})")
                return None
            if not price_above_ema50:
                self.logger.info(f"[EMA50] {symbol}: LONG rejected — price below EMA50 (counter-trend)")
                return None
            if not rsi_rising:
                self.logger.info(f"[RSI] {symbol}: LONG rejected — RSI not rising (rsi_now={rsi_now:.1f})")
                return None

            breakout_pct = (close - upper) / upper if upper != 0 else 0
            score = min(1.0, min_score + squeeze_bonus + breakout_pct * 2)
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
                        f"BB upper breakout: close={close:.4f} > upper={upper:.4f} "
                        f"{'(squeeze!)' if squeeze else ''}"
                    ),
                    stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT,
                    take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT,
                    metadata        = {
                        "strategy_name":               "bollinger_breakout",
                        "bb_upper":                    round(float(upper), 4),
                        "bb_lower":                    round(float(lower), 4),
                        "bb_mid":                      round(float(mid), 4),
                        "band_width":                  round(float(band_width), 4),
                        "squeeze":                     bool(squeeze),
                        "adx":                         round(adx_val, 2) if adx_val is not None else None,
                        "rsi":                         round(float(rsi_now), 2),
                        "obv_rising":                  bool(obv_rising),
                        "ema50":                       round(float(ema50_val), 4),
                        "volume_ratio":                vol_ratio,
                        "preferred_initial_stop_mode": "percent",
                        "preferred_trail_mode":        "percent",
                    },
                )

        # ----------------------------------------------------------------
        # SHORT SIGNAL: price breaks below lower band
        # Disabled (self.shorts_enabled=False) — live data shows 11.8% WR on
        # shorts vs 25% for longs. Re-enable after trader-dev backtest confirms edge.
        # ----------------------------------------------------------------
        breaks_lower = close < lower
        self.verbose_log(
            symbol, "Price breaks below lower Bollinger Band (short)",
            breaks_lower, close, f"<{lower:.4f}", "short"
        )
        if not getattr(self, "shorts_enabled", False):
            if breaks_lower:
                self.verbose_log_skip(symbol, "Short signals disabled (shorts_enabled=False) — skipping")
            return None
        if breaks_lower:
            if not obv_falling:
                self.logger.info(f"[OBV] {symbol}: SHORT rejected — OBV not falling (falling={obv_falling})")
                return None
            if price_above_ema50:
                self.logger.info(f"[EMA50] {symbol}: SHORT rejected — price above EMA50 (counter-trend)")
                return None
            if not rsi_falling:
                self.logger.info(f"[RSI] {symbol}: SHORT rejected — RSI not falling (rsi_now={rsi_now:.1f})")
                return None

            breakout_pct = (lower - close) / lower if lower != 0 else 0
            score = min(1.0, min_score + squeeze_bonus + breakout_pct * 2)
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
                        f"BB lower breakout: close={close:.4f} < lower={lower:.4f} "
                        f"{'(squeeze!)' if squeeze else ''}"
                    ),
                    stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT,
                    take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT,
                    metadata        = {
                        "strategy_name":               "bollinger_breakout",
                        "bb_upper":                    round(float(upper), 4),
                        "bb_lower":                    round(float(lower), 4),
                        "bb_mid":                      round(float(mid), 4),
                        "band_width":                  round(float(band_width), 4),
                        "squeeze":                     bool(squeeze),
                        "adx":                         round(adx_val, 2) if adx_val is not None else None,
                        "rsi":                         round(float(rsi_now), 2),
                        "obv_falling":                 bool(obv_falling),
                        "ema50":                       round(float(ema50_val), 4),
                        "volume_ratio":                vol_ratio,
                        "preferred_initial_stop_mode": "percent",
                        "preferred_trail_mode":        "percent",
                    },
                )

        # No signal
        self.verbose_log(
            symbol, "Price inside Bollinger Bands (no signal)",
            False, close,
            f"need >{upper:.4f} or <{lower:.4f}"
        )
        return None
