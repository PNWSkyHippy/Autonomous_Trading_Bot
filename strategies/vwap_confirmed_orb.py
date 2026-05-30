"""
vwap_confirmed_orb.py
---------------------
Standalone VWAP-confirmed Opening Range Breakout strategy.

Design intent:
- Stock-first intraday ORB with VWAP confirmation
- Crypto supported with a UTC-session opening range
- Closed-bar only
- Initial stop = opposite side of opening range
- Take profit = fixed R multiple
- VWAP cross exit handled by existing ORB monitor/backtester hook
  (patch strategy-name checks if using a new strategy_name)

Stocks preset:
- 5Min bars
- first 6 bars = 30-minute opening range

Crypto preset:
- 15m bars
- first 4 bars = 1-hour opening range from UTC session open
"""

import logging
from datetime import date
from typing import Optional

import pandas as pd
import pandas_ta as ta

import config
from strategies.base_strategy import BaseStrategy, TradeSignal

logger = logging.getLogger(__name__)

# ── Core parameters ──────────────────────────────────────────────────────
FAST_EMA    = 20
SLOW_EMA    = 50
ATR_PERIOD  = 14
RSI_PERIOD  = 14
VOL_PERIOD  = 20

STOCK_ORB_BARS   = 6   # 6 x 5m = first 30 min
CRYPTO_ORB_BARS  = 4   # 4 x 15m = first 60 min

STOCK_VOL_MULT   = 1.20
CRYPTO_VOL_MULT  = 1.10

STOCK_MIN_OR_ATR  = 0.35
CRYPTO_MIN_OR_ATR = 0.50
STOCK_MAX_OR_ATR  = 2.00   # reject ORB wider than 2x ATR — stops too large
CRYPTO_MAX_OR_ATR = 3.00   # crypto needs more room

# Stop is pinned to orb_low/orb_high BUT capped so max loss is bounded
# even if ORB slips through the max-width filter (e.g. ATR mis-priced)
STOCK_MAX_STOP_PCT  = 1.50  # never risk more than 1.5% per stock ORB trade
CRYPTO_MAX_STOP_PCT = 2.50

# Price must clear ORB level by this % before we call it a real breakout
STOCK_BREAKOUT_BUFFER_PCT  = 0.10  # 0.1% above orb_high for longs
CRYPTO_BREAKOUT_BUFFER_PCT = 0.15

STOCK_R_MULT     = 2.0
CRYPTO_R_MULT    = 2.5

STOCK_MAX_CHASE_PCT  = 0.75
CRYPTO_MAX_CHASE_PCT = 1.25

MAX_LONG_ENTRY_RSI   = 75
MIN_SHORT_ENTRY_RSI  = 25

MIN_BARS = max(SLOW_EMA, ATR_PERIOD, RSI_PERIOD, VOL_PERIOD) + 30

class VwapConfirmedOrb(BaseStrategy):
    def __init__(self):
        super().__init__()
        self.strategy_name   = "vwap_confirmed_orb"
        self.stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT
        self.take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT

        self.stock_candle_timeframe  = "5Min"
        self.crypto_candle_timeframe = "15m"
        self.candle_limit            = 220

        self.stock_enabled  = True
        self.crypto_enabled  = False   # VWAP has no reliable anchor on 24/7 crypto
        self.enabled         = True
        self.reviewer_exempt = True

        # ML model is dominated by grid_bot (1507 trades); vwap_confirmed_orb has
        # ~0 representation so ML blending collapses valid scores below threshold.
        self.ml_exempt = True

        logger.info(
            f"[{self.strategy_name}] Initialized — stock 5Min ORB + crypto 15m ORB"
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _asset_class(self, symbol: str) -> str:
        return "crypto" if "/" in symbol else "stock"

    def _normalise_index(self, candles: pd.DataFrame, asset_class: str) -> pd.DataFrame:
        """
        Normalize index for session calculations.
        - Stocks: convert tz-aware bars to America/New_York, then strip tz
        - Crypto: convert tz-aware bars to UTC, then strip tz
        - Naive timestamps are left as-is
        """
        out = candles.copy()
        idx = pd.DatetimeIndex(out.index)

        try:
            if idx.tz is not None:
                if asset_class == "stock":
                    from zoneinfo import ZoneInfo
                    idx = idx.tz_convert(ZoneInfo("America/New_York")).tz_localize(None)
                else:
                    idx = idx.tz_convert("UTC").tz_localize(None)
        except Exception:
            pass

        out.index = idx
        return out.sort_index()

    def _session_bars(self, candles: pd.DataFrame, asset_class: str) -> Optional[pd.DataFrame]:
        df = self._normalise_index(candles, asset_class)
        if df.empty:
            return None

        if asset_class == "stock":
            # Cash session only
            try:
                df = df.between_time("09:30", "16:00")
            except Exception:
                pass

        if df.empty:
            return None

        session_date = df.index[-1].date()
        df = df[df.index.date == session_date]
        return df if not df.empty else None

    def _session_vwap(self, df: pd.DataFrame) -> pd.Series:
        typical = (
            df["high"].astype(float) +
            df["low"].astype(float) +
            df["close"].astype(float)
        ) / 3.0
        volume = df["volume"].astype(float)
        cum_vol = volume.cumsum().replace(0, float("nan"))
        return (typical * volume).cumsum() / cum_vol

    def _calc_atr(self, high: pd.Series, low: pd.Series, close: pd.Series) -> Optional[float]:
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

    def _calc_ema_pair(self, close: pd.Series):
        try:
            ema_fast_s = ta.ema(close, length=FAST_EMA)
            ema_slow_s = ta.ema(close, length=SLOW_EMA)
            if ema_fast_s is None or ema_slow_s is None:
                return None, None
            return float(ema_fast_s.iloc[-1]), float(ema_slow_s.iloc[-1])
        except Exception:
            return None, None

    # ── Main analysis ────────────────────────────────────────────────────

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None

        asset_class = self._asset_class(symbol)
        close  = candles["close"].astype(float)
        high   = candles["high"].astype(float)
        low    = candles["low"].astype(float)
        volume = candles["volume"].astype(float)

        current_close = float(close.iloc[-1])

        session = self._session_bars(candles, asset_class)
        if session is None or session.empty:
            self.verbose_log_skip(symbol, "No current session bars available")
            return None

        orb_bars         = STOCK_ORB_BARS if asset_class == "stock" else CRYPTO_ORB_BARS
        vol_mult         = STOCK_VOL_MULT if asset_class == "stock" else CRYPTO_VOL_MULT
        min_or_atr       = STOCK_MIN_OR_ATR if asset_class == "stock" else CRYPTO_MIN_OR_ATR
        max_or_atr       = STOCK_MAX_OR_ATR if asset_class == "stock" else CRYPTO_MAX_OR_ATR
        r_mult           = STOCK_R_MULT if asset_class == "stock" else CRYPTO_R_MULT
        max_chase_pct    = STOCK_MAX_CHASE_PCT if asset_class == "stock" else CRYPTO_MAX_CHASE_PCT
        max_stop_pct     = STOCK_MAX_STOP_PCT if asset_class == "stock" else CRYPTO_MAX_STOP_PCT
        bo_buffer_pct    = STOCK_BREAKOUT_BUFFER_PCT if asset_class == "stock" else CRYPTO_BREAKOUT_BUFFER_PCT

        # ── Stale session guard ──────────────────────────────────────────────
        # Pre-market: no today session bars exist yet, so _session_bars() returns
        # yesterday's session. An ORB from yesterday is stale — skip entirely.
        session_date = session.index[-1].date()
        today = date.today()
        if session_date < today:
            self.verbose_log_skip(
                symbol,
                f"Session date {session_date} is not today ({today}) — stale ORB, skip"
            )
            return None

        if len(session) <= orb_bars:
            self.verbose_log_skip(
                symbol,
                f"Opening range not complete yet ({len(session)}/{orb_bars} bars)"
            )
            return None

        opening = session.iloc[:orb_bars]
        orb_high = float(opening["high"].max())
        orb_low  = float(opening["low"].min())
        or_width = orb_high - orb_low

        atr = self._calc_atr(high, low, close)
        if atr is None or atr <= 0:
            self.verbose_log_skip(symbol, "ATR unavailable")
            return None

        if or_width < atr * min_or_atr:
            self.verbose_log_skip(
                symbol,
                f"Opening range too small ({or_width:.4f} < {min_or_atr:.2f} ATR)"
            )
            return None

        if or_width > atr * max_or_atr:
            self.verbose_log_skip(
                symbol,
                f"Opening range too wide ({or_width:.4f} > {max_or_atr:.2f}x ATR={atr:.4f}) "
                f"— stop would be {or_width / current_close * 100:.2f}% risk, skipping"
            )
            return None

        ema_fast, ema_slow = self._calc_ema_pair(close)
        if ema_fast is None or ema_slow is None:
            self.verbose_log_skip(symbol, "EMA calculation failed")
            return None

        rsi = self._calc_rsi(close)
        if rsi is None:
            self.verbose_log_skip(symbol, "RSI unavailable")
            return None

        session_vwap_s = self._session_vwap(session)
        if session_vwap_s is None or session_vwap_s.empty or pd.isna(session_vwap_s.iloc[-1]):
            self.verbose_log_skip(symbol, "VWAP unavailable")
            return None

        vwap_now    = float(session_vwap_s.iloc[-1])
        prev_close  = float(session["close"].iloc[-2])
        current_vol = float(session["volume"].iloc[-1])

        vol_avg   = float(volume.iloc[-VOL_PERIOD:].mean()) if len(volume) >= VOL_PERIOD else float(volume.mean())
        vol_ratio = (current_vol / vol_avg) if vol_avg > 0 else 1.0

        long_chase_pct  = ((current_close - orb_high) / orb_high * 100) if orb_high > 0 else 0.0
        short_chase_pct = ((orb_low - current_close) / orb_low * 100) if orb_low > 0 else 0.0

        # ── LONG breakout ────────────────────────────────────────────────
        long_bo_threshold = orb_high * (1 + bo_buffer_pct / 100)   # must clear by buffer %
        long_breakout = (
            current_close >= long_bo_threshold and
            prev_close < long_bo_threshold and
            current_close > vwap_now and
            current_close > ema_fast and
            ema_fast > ema_slow and
            vol_ratio >= vol_mult and
            rsi <= MAX_LONG_ENTRY_RSI and
            long_chase_pct <= max_chase_pct
        )

        if long_breakout:
            # Structural stop at orb_low, capped so risk never exceeds max_stop_pct
            raw_stop    = orb_low
            capped_stop = current_close * (1 - max_stop_pct / 100)
            stop_price  = max(raw_stop, capped_stop)   # higher price = tighter for long
            if stop_price >= current_close:
                self.verbose_log_skip(symbol, "Long stop invalid — OR low above entry")
                return None

            risk = current_close - stop_price
            if risk <= 0:
                self.verbose_log_skip(symbol, "Long risk <= 0")
                return None

            target_price = current_close + (risk * r_mult)
            sl_pct = (risk / current_close) * 100
            tp_pct = ((target_price - current_close) / current_close) * 100

            score = 0.72
            score += min(0.12, max(0.0, vol_ratio - vol_mult) * 0.12)
            if or_width >= atr:
                score += 0.06
            score = min(1.0, score)

            self.verbose_log_score(symbol, score, 0.65)

            return self._make_signal(
                symbol          = symbol,
                direction       = "long",
                score           = round(score, 3),
                reason          = (
                    f"VWAP ORB long: close {current_close:.4f} > OR high {orb_high:.4f} | "
                    f"VWAP={vwap_now:.4f} | EMA{FAST_EMA}>{SLOW_EMA} | "
                    f"vol={vol_ratio:.2f}x | RSI={rsi:.1f}"
                ),
                stop_loss_pct   = round(sl_pct, 3),
                take_profit_pct = round(tp_pct, 3),
                metadata        = {
                    "strategy_name":         self.strategy_name,
                    "entry_timeframe":       "5Min" if asset_class == "stock" else "15m",
                    "entry_type":            "raw_orb_breakout",
                    "structural_stop_price": round(stop_price, 6),
                    "orb_high":              round(orb_high, 6),
                    "orb_low":               round(orb_low, 6),
                    "orb_width":             round(or_width, 6),
                    "orb_width_atr":         round(or_width / atr, 4),
                    "session_vwap":          round(vwap_now, 6),
                    "ema_fast":              round(ema_fast, 6),
                    "ema_slow":              round(ema_slow, 6),
                    "atr":                   round(atr, 6),
                    "rsi":                   round(rsi, 2),
                    "volume_ratio":          round(vol_ratio, 3),
                    "distance_from_breakout_pct": round(long_chase_pct, 3),
                    "bars_since_breakout":        0,
                    "pattern":                    "vwap_confirmed_orb_long",
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                }
            )

        # ── SHORT breakout ───────────────────────────────────────────────
        short_bo_threshold = orb_low * (1 - bo_buffer_pct / 100)   # must clear by buffer %
        short_breakout = (
            current_close <= short_bo_threshold and
            prev_close > short_bo_threshold and
            current_close < vwap_now and
            current_close < ema_fast and
            ema_fast < ema_slow and
            vol_ratio >= vol_mult and
            rsi >= MIN_SHORT_ENTRY_RSI and
            short_chase_pct <= max_chase_pct
        )

        if short_breakout:
            # Structural stop at orb_high, capped so risk never exceeds max_stop_pct
            raw_stop    = orb_high
            capped_stop = current_close * (1 + max_stop_pct / 100)
            stop_price  = min(raw_stop, capped_stop)   # lower price = tighter for short
            if stop_price <= current_close:
                self.verbose_log_skip(symbol, "Short stop invalid — OR high below entry")
                return None

            risk = stop_price - current_close
            if risk <= 0:
                self.verbose_log_skip(symbol, "Short risk <= 0")
                return None

            target_price = current_close - (risk * r_mult)
            sl_pct = (risk / current_close) * 100
            tp_pct = ((current_close - target_price) / current_close) * 100

            score = 0.72
            score += min(0.12, max(0.0, vol_ratio - vol_mult) * 0.12)
            if or_width >= atr:
                score += 0.06
            score = min(1.0, score)

            self.verbose_log_score(symbol, score, 0.65)

            return self._make_signal(
                symbol          = symbol,
                direction       = "short",
                score           = round(score, 3),
                reason          = (
                    f"VWAP ORB short: close {current_close:.4f} < OR low {orb_low:.4f} | "
                    f"VWAP={vwap_now:.4f} | EMA{FAST_EMA}<{SLOW_EMA} | "
                    f"vol={vol_ratio:.2f}x | RSI={rsi:.1f}"
                ),
                stop_loss_pct   = round(sl_pct, 3),
                take_profit_pct = round(tp_pct, 3),
                metadata        = {
                    "strategy_name":         self.strategy_name,
                    "entry_timeframe":       "5Min" if asset_class == "stock" else "15m",
                    "entry_type":            "raw_orb_breakout",
                    "structural_stop_price": round(stop_price, 6),
                    "orb_high":              round(orb_high, 6),
                    "orb_low":               round(orb_low, 6),
                    "orb_width":             round(or_width, 6),
                    "orb_width_atr":         round(or_width / atr, 4),
                    "session_vwap":          round(vwap_now, 6),
                    "ema_fast":              round(ema_fast, 6),
                    "ema_slow":              round(ema_slow, 6),
                    "atr":                   round(atr, 6),
                    "rsi":                   round(rsi, 2),
                    "volume_ratio":          round(vol_ratio, 3),
                    "distance_from_breakout_pct": round(short_chase_pct, 3),
                    "bars_since_breakout":        0,
                    "pattern":                    "vwap_confirmed_orb_short",
                    "preferred_initial_stop_mode": "signal_structural",
                    "preferred_trail_mode":        "none",
                }
            )

        logger.debug(
            f"[{self.strategy_name}] {symbol}: no signal | "
            f"close={current_close:.4f} OR=({orb_low:.4f}-{orb_high:.4f}) "
            f"VWAP={vwap_now:.4f} EMAf={ema_fast:.4f} EMAs={ema_slow:.4f} "
            f"vol={vol_ratio:.2f}x RSI={rsi:.1f}"
        )
        return None