"""
scanners/volume_surge_scanner.py
=================================
Volume Surge Scanner -- intraday 5-minute bar detector.

What is a Volume Surge?
-----------------------
When a 5-minute candle's volume is >= VOLUME_MULT × the rolling average volume
of the prior N bars on the same symbol. This signals:
    - Institution / smart-money entering a position
    - News catalyst hitting mid-session
    - Breakout from consolidation with conviction

This fires INTRADAY throughout the trading session (not just at open).

Architecture note:
------------------
BreakoutScanner already fetches 5m OHLCV bars every scan cycle for all symbols.
Rather than running a separate daemon thread (which would re-fetch the same data),
this class is a HELPER called by BreakoutScanner._process_symbol() after
each price update. It tracks rolling volume in-process and emits a signal
when a surge is detected.

Usage inside BreakoutScanner:
    # In __init__:
    self.vol_surge = VolumeSurgeTracker(cooldown_hours=4.0)

    # In _process_symbol(), after fetching bar data:
    surge = self.vol_surge.update(symbol, asset_class, bars_df)
    if surge:
        self.sender.send_raw_payload(surge)

Design:
    - Keeps a rolling deque of recent bar volumes per symbol (lookback window)
    - On each new bar close, compares bar volume to rolling mean
    - Requires price move in the same bar to confirm (not just volume noise)
    - Cooldown per symbol prevents re-firing on the same surge cluster

Parameters (tunable):
    LOOKBACK_BARS  = 20    bars to average (100 minutes at 5m = ~1.5h)
    VOLUME_MULT    = 3.0   surge threshold multiplier
    MIN_MOVE_PCT   = 0.3   minimum price move % in the bar (filter dead vol)
    COOLDOWN_HOURS = 4.0   hours between signals per symbol
"""

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

LOOKBACK_BARS   = 20     # rolling average window (bars)
VOLUME_MULT     = 3.0    # vol >= N × avg to count as surge
MIN_MOVE_PCT    = 0.3    # bar must move at least this % (H-L / close)
COOLDOWN_HOURS  = 4.0    # no re-fire within N hours per symbol

SCORE_BASE      = 0.65
SL_PCT          = 1.5    # stop loss % (tight -- momentum entry)
TP_PCT          = 3.0    # take profit %


# ---------------------------------------------------------------------------
# Per-symbol state
# ---------------------------------------------------------------------------

@dataclass
class SymbolVolumeState:
    vol_history: Deque[float] = field(default_factory=lambda: deque(maxlen=LOOKBACK_BARS))
    last_bar_time: Optional[datetime] = None
    last_fired_ts: float = 0.0     # epoch seconds


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class VolumeSurgeTracker:
    """
    Called by BreakoutScanner once per price-update cycle per symbol.
    Tracks rolling 5m volume and detects surges.
    """

    def __init__(self, cooldown_hours: float = COOLDOWN_HOURS):
        self._cooldown_sec = cooldown_hours * 3600
        self._state: Dict[str, SymbolVolumeState] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def update_from_pricedata(
        self,
        pd,          # PriceData instance (from BreakoutScanner)
        asset_class: str,
    ) -> Optional[dict]:
        """
        Feed a PriceData snapshot. Each scan tick accumulates volume.
        Fires when current volume is >= VOLUME_MULT × rolling average AND
        the intraday price move is meaningful.

        Called by BreakoutScanner._process_symbol() on every scan tick.
        """
        symbol = pd.symbol

        try:
            cur_vol   = float(pd.volume)   if pd.volume    is not None else 0.0
            cur_price = float(pd.price)    if pd.price     is not None else 0.0
            cur_open  = float(pd.open_price) if pd.open_price is not None else 0.0
            _high     = getattr(pd, "high", None)
            _low      = getattr(pd, "low",  None)
            cur_high  = float(_high) if _high is not None else cur_price
            cur_low   = float(_low)  if _low  is not None else cur_price
        except (TypeError, ValueError):
            return None

        if cur_price <= 0 or cur_vol <= 0:
            return None

        state = self._state.setdefault(symbol, SymbolVolumeState())

        # Bucket ticks by clock-minute so we don't add the same bar N times
        import time as _time
        now_ts   = _time.time()
        tick_min = int(now_ts // 60)

        if state.last_bar_time != tick_min:
            state.last_bar_time = tick_min
            state.vol_history.append(cur_vol)

        # Need enough history
        if len(state.vol_history) < 5:
            return None

        # Rolling average of the previous N buckets (exclude current)
        history_list = list(state.vol_history)[:-1]
        avg_vol = sum(history_list) / len(history_list)

        if avg_vol <= 0:
            return None

        vol_ratio = cur_vol / avg_vol
        if vol_ratio < VOLUME_MULT:
            return None

        # Require meaningful intraday move
        move_pct = abs(cur_price - cur_open) / cur_open * 100 if cur_open > 0 else 0.0
        if move_pct < MIN_MOVE_PCT:
            logger.debug("[VOL] %s vol surge %.1fx but price move too small (%.2f%%) -- skip",
                         symbol, vol_ratio, move_pct)
            return None

        # Cooldown check
        if now_ts - state.last_fired_ts < self._cooldown_sec:
            return None
        state.last_fired_ts = now_ts

        direction = "long" if cur_price >= cur_open else "short"
        score     = min(0.90, SCORE_BASE + (vol_ratio - VOLUME_MULT) * 0.05)

        logger.info(
            "[VOL] %s  %s  vol=%.0f  avg=%.0f  mult=%.1fx  move=%.2f%%  score=%.2f",
            symbol, direction.upper(), cur_vol, avg_vol,
            vol_ratio, move_pct, score,
        )

        now_utc = datetime.now(timezone.utc)

        return {
            "symbol":                symbol,
            "asset_class":           asset_class,
            "direction":             direction,
            "entry_price":           cur_price,
            "current_price":         cur_price,
            "move_pct":              round(move_pct, 3),
            "volume_spike":          round(vol_ratio, 3),
            "confidence":            round(score, 3),
            "escalation":            1,
            "timestamp":             now_utc.isoformat(),
            "signal_source":         "volume_surge",
            "strategy_name":         "volume_surge",
            "broker":                "alpaca" if asset_class == "stock" else "coinbase",
            "stop_loss_pct":         SL_PCT,
            "take_profit_pct":       TP_PCT,
            "structural_stop_price": round(cur_low  if direction == "long" else cur_high, 6),
            "preferred_trail_mode":  "none",
            "reason":                "Volume surge %.1fx avg (%.2f%% intraday move, %s)" % (
                                         vol_ratio, move_pct,
                                         "bullish" if direction == "long" else "bearish"),
            "bars_since_breakout":   0,
            "current_volume":        cur_vol,
            "avg_volume":            round(avg_vol, 0),
            "vol_mult":              round(vol_ratio, 3),
            "intraday_move_pct":     round(move_pct, 3),
        }

    def update(
        self,
        symbol:      str,
        asset_class: str,
        bars,                    # pandas DataFrame with OHLCV, DatetimeIndex
    ) -> Optional[dict]:
        """
        Alternative entry: feed a full OHLCV DataFrame (e.g. yfinance 5m bars).
        Uses the second-to-last bar (most recently *closed* bar).
        """
        if bars is None or len(bars) < 3:
            return None

        state = self._state.setdefault(symbol, SymbolVolumeState())

        try:
            bar        = bars.iloc[-2]
            bar_time   = bars.index[-2]
            bar_open   = float(bar["Open"])
            bar_high   = float(bar["High"])
            bar_low    = float(bar["Low"])
            bar_close  = float(bar["Close"])
            bar_volume = float(bar["Volume"])
        except (IndexError, KeyError):
            return None

        if bar_close <= 0 or bar_volume <= 0:
            return None

        if state.last_bar_time == bar_time:
            return None
        state.last_bar_time = bar_time

        state.vol_history.append(bar_volume)

        if len(state.vol_history) < 5:
            return None

        history_list = list(state.vol_history)[:-1]
        avg_vol = sum(history_list) / len(history_list)

        if avg_vol <= 0:
            return None

        vol_ratio = bar_volume / avg_vol
        if vol_ratio < VOLUME_MULT:
            return None

        move_pct = (bar_high - bar_low) / bar_close * 100
        if move_pct < MIN_MOVE_PCT:
            return None

        import time as _time
        now_ts = _time.time()
        if now_ts - state.last_fired_ts < self._cooldown_sec:
            return None
        state.last_fired_ts = now_ts

        direction = "long" if bar_close >= bar_open else "short"
        score     = min(0.90, SCORE_BASE + (vol_ratio - VOLUME_MULT) * 0.05)

        logger.info(
            "[VOL] %s  %s  vol=%.0f  avg=%.0f  mult=%.1fx  move=%.2f%%  score=%.2f",
            symbol, direction.upper(), bar_volume, avg_vol,
            vol_ratio, move_pct, score,
        )

        now_utc = datetime.now(timezone.utc)

        return {
            "symbol":                symbol,
            "asset_class":           asset_class,
            "direction":             direction,
            "entry_price":           bar_close,
            "current_price":         bar_close,
            "move_pct":              round(move_pct, 3),
            "volume_spike":          round(vol_ratio, 3),
            "confidence":            round(score, 3),
            "escalation":            1,
            "timestamp":             now_utc.isoformat(),
            "signal_source":         "volume_surge",
            "strategy_name":         "volume_surge",
            "broker":                "alpaca" if asset_class == "stock" else "coinbase",
            "stop_loss_pct":         SL_PCT,
            "take_profit_pct":       TP_PCT,
            "structural_stop_price": round(bar_low  if direction == "long" else bar_high, 6),
            "preferred_trail_mode":  "none",
            "reason":                "Volume surge %.1fx avg (%s bar, move %.2f%%)" % (
                                         vol_ratio, "bullish" if direction == "long" else "bearish",
                                         move_pct),
            "bars_since_breakout":   0,
            "bar_volume":            bar_volume,
            "avg_volume":            round(avg_vol, 0),
            "vol_mult":              round(vol_ratio, 3),
            "bar_move_pct":          round(move_pct, 3),
        }

    def reset_symbol(self, symbol: str) -> None:
        """Clear history for a symbol (e.g. at midnight reset)."""
        self._state.pop(symbol, None)

    def reset_all(self) -> None:
        """Clear all symbol history (midnight reset)."""
        self._state.clear()
