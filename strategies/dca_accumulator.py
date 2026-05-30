"""
dca_accumulator.py — Strategy 8: DCA Accumulator
Trading Bot v2

Dollar-cost averaging strategy for blue-chip stocks and major crypto.
LONG ONLY — buys dips below the EMA50 when RSI confirms oversold conditions.
Uses a wider stop (2%) and larger target (5%) to hold through volatility.

Only trades symbols in the blue-chip/major-crypto whitelist.
Thresholds configurable in config.SIGNAL_TUNING.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AUDIT STATUS (2026-05-16) — quant-strategy-auditor-refiner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STATUS: HEALTHY — best-performing strategy audited to date; needs more data

LIVE PERFORMANCE — CLEAN (17 strategy-only exits, May 7-13, crypto only):
  Total P&L:  +$36.30
  WR:          47.06%  (breakeven = 33.4% at live R:R 1.990:1)
  WR margin:  +13.7 pp above breakeven — healthy buffer
  AvgWin:     +$10.44 / +0.52%    AvgLoss:  -$5.24 / -0.26%
  No TP hits  — 5% target too far for avg 2.25h hold on 1h candles
  All profits: stale_trend_reversed (+$57.95) + stale_no_trend (+$16.05)
  All losses:  pivot_break_s1 (-$21.60) + stop_loss (-$16.10)
  Stocks:      0 trades — dip condition (2% below EMA50 + RSI<45) not
               triggering in May 2026 uptrend environment

ROOT CAUSE DIAGNOSIS:
  1. PIVOT EXIT STRUCTURAL CONFLICT (main issue):
     8/17 trades exit via pivot_break_s1, avg -0.134% (loss).
     DCA enters when price is already 2%+ below EMA50 (near recent lows).
     Pivot S1 (config.PIVOT_EXIT_BARS=20 bars of 1h data) sits just below
     the entry price. Entry ≈ pivot support → any further weakness fires
     pivot exit within 1-2 bars, well before the 2% SL.
     Effective stop becomes ~0.13% instead of designed 2.0% — 15x tighter.
  2. TP TOO FAR: 5% target with avg 2.25h hold = no TP hits. Profits come
     entirely from stale exits acting as adaptive TPs. This is acceptable
     behavior but TP could be tightened to 3-4% to capture more wins before
     trend reversal.
  3. SAMPLE TOO SMALL: 17 trades / 9 days insufficient for conclusions.
     47% WR could be noise. Need 50+ trades for statistical confidence.
  4. STOCKS DORMANT: The dip filter (2% below EMA50, RSI<45) hasn't fired
     for any stock in May 2026 uptrend. Strategy is 100% crypto exposure.

ITERATION CHANGELOG:
  Baseline  EMA50 dip 2%, RSI<45, SL=2%, TP=5%, whitelist only
            Initial design — no live audit data yet.
  Audit     2026-05-16: 17 clean live trades reviewed.
            Finding: pivot_break_s1 exits conflict with DCA design.
            Finding: stale exits are the actual profit mechanism (+$74).
            Finding: stocks inactive — dip 2% + RSI<45 not firing in uptrend.
  Step1     RSI max 45→35 (2026-05-16, Trader Dev sweep)
            Sweep: 92 combos on BTCUSDT 1h, 1yr, objective=profitFactor
            RSI≤45: best PF ~1.01-1.02 (barely above 1.0)
            RSI≤35: best PF  1.115 — consistent top-slot domination
            Tighter RSI = genuinely oversold entries, not just mild pullbacks.
            ACCEPT — RSI35 consistently outperforms across all dip_pct values.
            NOTE: Raw PF 1.115 is modest. Live stale exits outperform raw
            SL/TP — the position monitor is the real edge enhancer for DCA.

APPLIED CHANGES:
  ✅ config.SIGNAL_TUNING["dca_rsi_max"] 45→35 (audit sweep 2026-05-16)

NEXT STEPS:
  1. Monitor for 50+ trades under RSI35 — expect fewer but higher-quality
     entries. RSI<35 fires less often than RSI<45.
  2. If WR improves toward 50%+: investigate pivot exit conflict next.
     Option A: Increase PIVOT_EXIT_BARS 20→40 (wider pivots, less sensitive)
     Option B: Add pivot_exempt flag in position monitor for DCA strategy
  3. If WR holds above 45% for 50+ trades: tighten TP to 3.5% (stale exits
     capture avg +0.52% — a 5% TP is never hit; 3.5% captures more wins)
  4. Consider relaxing dca_dip_pct for stocks from 2%→1% — blue chips
     rarely dip 2% intraday in an uptrend; 1% may be more realistic
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from typing import Optional

import pandas as pd
import pandas_ta as ta

import config
from strategies.base_strategy import BaseStrategy, TradeSignal


# Blue-chip stocks and major crypto appropriate for DCA accumulation.
# Crypto symbols must be in the 1hr/365 day backtest GO list —
# DCA works best on established coins with real volume and liquidity.
# Avoid meme coins and low-cap tokens — accumulation requires sustained demand.
DCA_WHITELIST = {
    # Blue-chip stocks
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "SPY", "QQQ", "JPM", "BAC", "XOM",
    # Extended tech — confirmed positive PF on mean_reversion 1h/90d backtest
    # (all appeared in mean_reversion's 50-stock basket with trades)
    "AMD", "INTC", "MRVL", "QCOM", "MU", "AMAT", "LRCX", "KLAC",
    "PLTR", "CRM", "CRWD", "PANW", "COIN", "PYPL", "SOFI", "HOOD",
    # Major crypto — confirmed GO on 1hr/365 day backtest with 2-bar trailing stop
    "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD",
    "XRP/USD", "LINK/USD", "DOT/USD", "AVAX/USD",
    "MATIC/USD", "UNI/USD", "ATOM/USD", "LTC/USD",
    "BCH/USD", "ALGO/USD", "XLM/USD", "ETC/USD",
    "BTC-USD", "ETH-USD", "SOL-USD",  # Coinbase format fallback
}


class DCAAccumulator(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.strategy_name   = "dca_accumulator"
        self.stop_loss_pct   = 2.0     # Wider stop — willing to hold through dips
        self.take_profit_pct = 5.0     # Larger target

        # DCA accumulator is a slow-moving strategy — 1hr bars are the right
        # timeframe. Backtested at 1hr/365 days with all GO results.
        # Needs more bars (200) to see EMA50 and RSI context clearly.
        self.stock_candle_timeframe  = "1Hour"
        self.crypto_candle_timeframe = "1h"
        self.candle_limit            = 200

        # ML model is dominated by grid_bot (1507 trades); DCA has ~0 historical
        # trades in the training set so ML blending produces noise, not signal.
        # Raw strategy score is the correct confidence measure here.
        self.ml_exempt       = True
        self.auto_disable_exempt = True

        # DCA is a systematic rules-based accumulator — no discretionary review
        # needed. Claude reviewer adds 12-60s latency per signal and can hang
        # the scan thread on Massive API delays after reboot.
        self.reviewer_exempt = True

        self.stock_enabled   = True
        self.crypto_enabled  = True

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        tuning      = config.SIGNAL_TUNING
        ema_period  = tuning["dca_ema_period"]
        dip_pct     = tuning["dca_dip_pct"]
        rsi_max     = tuning["dca_rsi_max"]
        min_score   = tuning["dca_min_score"]

        # Only trade blue-chip/major assets
        in_whitelist = self._passes_symbol_whitelist(
            symbol, DCA_WHITELIST, "DCA whitelist"
        )
        self.verbose_log(
            symbol, "Symbol in DCA whitelist (blue-chip/major crypto only)",
            in_whitelist, symbol, "must be in whitelist"
        )
        if not in_whitelist:
            return None

        required = ema_period + 5
        if not self._check_enough_candles(symbol, candles, required):
            return None

        close = candles["close"]

        # Calculate EMA50
        try:
            ema = ta.ema(close, length=ema_period)
            if ema is None or ema.isna().all():
                self.verbose_log_skip(symbol, "EMA returned no data")
                return None
        except Exception as e:
            self.verbose_log_skip(symbol, f"EMA error: {e}")
            return None

        ema_value     = ema.iloc[-1]
        current_price = close.iloc[-1]

        if pd.isna(ema_value):
            self.verbose_log_skip(symbol, "EMA value is NaN")
            return None

        # Check price is below EMA50 by at least dip_pct
        if ema_value == 0:
            self.verbose_log_skip(symbol, "EMA value is zero")
            return None

        price_vs_ema  = (current_price - ema_value) / ema_value  # negative = below EMA
        required_dip  = -dip_pct  # e.g. -0.02 means 2% below EMA

        below_ema = price_vs_ema <= required_dip
        self.verbose_log(
            symbol, f"Price is {dip_pct*100:.1f}%+ below EMA{ema_period} (dip condition)",
            below_ema,
            price_vs_ema,
            f"<={required_dip:.4f}",
            "long",
            extra=f"price={current_price:.4f} EMA{ema_period}={ema_value:.4f}"
        )

        if not below_ema:
            return None

        # Calculate RSI to confirm oversold
        try:
            rsi_series = ta.rsi(close, length=14)
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

        rsi_oversold = current_rsi <= rsi_max
        self.verbose_log(
            symbol, f"RSI confirms oversold (RSI <= {rsi_max})",
            rsi_oversold, current_rsi, f"<={rsi_max}", "long"
        )

        if not rsi_oversold:
            return None

        # Both conditions met — compute score
        dip_depth   = abs(price_vs_ema) / dip_pct  # 1.0 = exactly at threshold
        rsi_depth   = (rsi_max - current_rsi) / rsi_max
        score = min(1.0, min_score + (dip_depth * 0.1) + (rsi_depth * 0.1))

        self.verbose_log_score(symbol, score, min_score)

        if score >= min_score:
            vol_series  = candles["volume"]
            vol_ma      = vol_series.rolling(20).mean().iloc[-1]
            vol_ratio   = round(float(vol_series.iloc[-1] / vol_ma), 3) if vol_ma > 0 else None
            stop_price  = float(current_price) * (1 - self.stop_loss_pct / 100)
            return self._make_signal(
                symbol          = symbol,
                direction       = "long",
                score           = score,
                stop_loss_pct   = self.stop_loss_pct,
                take_profit_pct = self.take_profit_pct,
                reason          = (
                    f"DCA accumulate: price {abs(price_vs_ema)*100:.1f}% below "
                    f"EMA{ema_period}, RSI={current_rsi:.1f}"
                ),
                metadata        = {
                    "strategy_name":               "dca_accumulator",
                    "ema_value":                   round(float(ema_value), 4),
                    "price_vs_ema_pct":            round(float(price_vs_ema) * 100, 3),
                    "rsi":                         round(float(current_rsi), 2),
                    "dip_depth":                   round(float(dip_depth), 3),
                    "rsi_depth":                   round(float(rsi_depth), 3),
                    "volume_ratio":                vol_ratio,
                    "structural_stop_price":       round(stop_price, 4),
                    "preferred_initial_stop_mode": "percent",
                    "preferred_trail_mode":        "percent",
                },
            )

        return None
