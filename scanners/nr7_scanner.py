"""
scanners/nr7_scanner.py
=======================
NR7 Scanner -- Narrow Range 7 pattern on daily close.

What is NR7?
------------
When today's candle range (High - Low) is the narrowest of the last 7 days.
Signals price compression -- the market is coiling. A breakout (in either
direction) typically follows within 1-3 days.

Trading logic:
    Direction bias comes from trend:
        price > 10d MA  AND  10d MA > 20d MA  → long bias (expect upward breakout)
        price < 10d MA  AND  10d MA < 20d MA  → short bias (expect downward breakout)
        mixed                                 → skip (no clear bias, too risky)

    Entry:  next day's open OR break of NR7 high/low
    Stop:   below NR7 low (long) or above NR7 high (short)
    Target: 2x the NR7 range projected from entry

Firing rules:
    Stocks:  fires at 16:05-16:30 ET (after daily close)
    Crypto:  fires at 00:05-00:20 UTC (after daily candle close)
    Cooldown: 22h (once per symbol per day)

References:
    Toby Crabel - "Day Trading With Short Term Price Patterns and Opening Range Breakout"
    NR7 is one of the most reliable compression patterns in technical analysis.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, List

from scanners.base_scanner import BaseEventScanner

try:
    import yfinance as yf
    import numpy as np
    _YF_OK = True
except ImportError:
    _YF_OK = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

NR7_LOOKBACK        = 7      # days to compare range against
MA_FAST             = 10     # fast MA for trend direction
MA_SLOW             = 20     # slow MA for trend direction
MIN_RANGE_PCT       = 0.3    # minimum NR7 range as % of price (filter micro ranges)
MAX_RANGE_PCT       = 8.0    # maximum range -- if this wide it's not really "narrow"

SL_BUFFER_PCT       = 0.1    # stop buffer beyond NR7 high/low
TP_RANGE_MULT       = 2.0    # TP = 2x NR7 range
SCORE_BASE          = 0.68

COOLDOWN_HOURS      = 22.0

# Firing windows
STOCK_CLOSE_START   = (16,  5)
STOCK_CLOSE_END     = (16, 30)
CRYPTO_CLOSE_START  = ( 0,  5)
CRYPTO_CLOSE_END    = ( 0, 20)


class NR7Scanner(BaseEventScanner):
    """
    Detects NR7 compression pattern on daily candles.
    Fires after market close when today's range is the narrowest in 7 days.
    """

    def __init__(
        self,
        stock_symbols:  Optional[List[str]] = None,
        crypto_symbols: Optional[List[str]] = None,
        enable_stocks:  bool = True,
        enable_crypto:  bool = True,
    ):
        super().__init__(
            name               = "NR7Scanner",
            cooldown_hours     = COOLDOWN_HOURS,
            check_interval_sec = 60,
        )
        self.stock_symbols  = stock_symbols  or []
        self.crypto_symbols = crypto_symbols or []
        self.enable_stocks  = enable_stocks
        self.enable_crypto  = enable_crypto

        self._stock_session_scanned:  Optional[str] = None
        self._crypto_session_scanned: Optional[str] = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_once(self) -> None:
        now_et  = self.et_now()
        now_utc = self.utc_now()

        if self.enable_stocks and self.stock_symbols:
            in_window = STOCK_CLOSE_START <= (now_et.hour, now_et.minute) < STOCK_CLOSE_END
            today     = now_et.strftime("%Y-%m-%d")
            if in_window and self._stock_session_scanned != today:
                self._stock_session_scanned = today
                logger.info("[NR7] Stock close window -- scanning %d symbols", len(self.stock_symbols))
                self._scan_symbols(self.stock_symbols, "stock")

        if self.enable_crypto and self.crypto_symbols:
            in_window = CRYPTO_CLOSE_START <= (now_utc.hour, now_utc.minute) < CRYPTO_CLOSE_END
            today     = now_utc.strftime("%Y-%m-%d")
            if in_window and self._crypto_session_scanned != today:
                self._crypto_session_scanned = today
                logger.info("[NR7] Crypto close window -- scanning %d symbols", len(self.crypto_symbols))
                self._scan_symbols(self.crypto_symbols, "crypto")

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def _scan_symbols(self, symbols: List[str], asset_class: str) -> None:
        if not _YF_OK:
            logger.warning("[NR7] yfinance not available -- scan skipped")
            return

        for symbol in symbols:
            if self.is_on_cooldown(symbol):
                continue
            try:
                self._check_nr7(symbol, asset_class)
                time.sleep(0.3)
            except Exception as e:
                logger.debug("[NR7] %s error: %s", symbol, e)

    def _check_nr7(self, symbol: str, asset_class: str) -> None:
        yf_sym = symbol.replace("/", "-")
        ticker = yf.Ticker(yf_sym)
        hist   = ticker.history(period="30d", interval="1d", auto_adjust=True)

        if hist is None or len(hist) < NR7_LOOKBACK + MA_SLOW:
            return

        # Today's candle
        today_high  = float(hist["High"].iloc[-1])
        today_low   = float(hist["Low"].iloc[-1])
        today_close = float(hist["Close"].iloc[-1])
        today_range = today_high - today_low

        if today_close <= 0:
            return

        # Range as % of price
        range_pct = today_range / today_close * 100
        if range_pct < MIN_RANGE_PCT or range_pct > MAX_RANGE_PCT:
            return

        # Compare to previous 6 days (total 7 including today)
        recent_ranges = [
            float(hist["High"].iloc[-(i+1)]) - float(hist["Low"].iloc[-(i+1)])
            for i in range(1, NR7_LOOKBACK)
        ]

        if today_range >= min(recent_ranges):
            # Not the narrowest range
            return

        # NR7 confirmed -- determine direction from trend
        closes = [float(hist["Close"].iloc[-i]) for i in range(1, MA_SLOW + 2)]
        closes.reverse()

        ma_fast = sum(closes[-MA_FAST:]) / MA_FAST
        ma_slow = sum(closes[-MA_SLOW:]) / MA_SLOW

        trending_up   = today_close > ma_fast and ma_fast > ma_slow
        trending_down = today_close < ma_fast and ma_fast < ma_slow

        if not trending_up and not trending_down:
            logger.debug("[NR7] %s NR7 found but no clear trend -- skipping", symbol)
            return

        direction = "long" if trending_up else "short"

        # Build stop and target from NR7 range
        if direction == "long":
            sl_price = today_low  * (1 - SL_BUFFER_PCT / 100)
            tp_price = today_close + (today_range * TP_RANGE_MULT)
        else:
            sl_price = today_high * (1 + SL_BUFFER_PCT / 100)
            tp_price = today_close - (today_range * TP_RANGE_MULT)

        sl_pct = abs(today_close - sl_price) / today_close * 100
        tp_pct = abs(tp_price - today_close) / today_close * 100

        # Score: tighter range relative to recent = higher score
        compression = 1.0 - (today_range / max(recent_ranges))
        score = min(0.90, SCORE_BASE + compression * 0.15)

        # Volume context
        avg_vol   = float(hist["Volume"].iloc[-8:-1].mean())
        today_vol = float(hist["Volume"].iloc[-1])
        vol_spike = today_vol / avg_vol if avg_vol > 0 else 1.0

        logger.info(
            "[NR7] %s  %s  range=%.2f%%  compression=%.0f%%  trend=%s  score=%.2f",
            symbol, direction.upper(), range_pct,
            compression * 100, "UP" if trending_up else "DOWN", score,
        )

        now_utc = self.utc_now()
        payload = {
            "symbol":                symbol,
            "asset_class":           asset_class,
            "direction":             direction,
            "entry_price":           today_close,
            "current_price":         today_close,
            "move_pct":              range_pct,
            "volume_spike":          round(vol_spike, 3),
            "confidence":            round(score, 3),
            "escalation":            1,
            "timestamp":             now_utc.isoformat(),
            "signal_source":         "nr7_scanner",
            "strategy_name":         "nr7_scanner",
            "broker":                "alpaca" if asset_class == "stock" else "coinbase",
            "stop_loss_pct":         round(sl_pct, 2),
            "take_profit_pct":       round(tp_pct, 2),
            "structural_stop_price": round(sl_price, 6),
            "preferred_trail_mode":  "none",
            "reason": "NR7: %.2f%% range (narrowest of 7d) trend=%s compression=%.0f%%" % (
                range_pct, "UP" if trending_up else "DOWN", compression * 100),
            "bars_since_breakout":   0,
            "nr7_high":              round(today_high, 6),
            "nr7_low":               round(today_low, 6),
            "nr7_range_pct":         round(range_pct, 3),
            "ma_fast":               round(ma_fast, 4),
            "ma_slow":               round(ma_slow, 4),
        }

        result = self.push_signal(payload)
        if result.get("accepted"):
            self.mark_fired(symbol)
