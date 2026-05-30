"""
scanners/gap_watchlist.py
=========================
GapWatchlist -- queue of overnight gap setups pending intraday confirmation.

Designed to live inside BreakoutScanner (same process), fed by gap detection
at market open, drained by the hot-symbol scan loop as it fetches live prices.

Confirmation uses PriceData (already fetched by scanner) -- no duplicate
yfinance calls.

Usage:
    wl = GapWatchlist()
    wl.register(setup)                          # called at 9:30 ET
    result = wl.check(symbol, pd, vol_spike)    # called from hot scan
    summaries = wl.summary()                    # called by GUI refresh
    wl.expire_stale(now_utc, now_et)            # called each scan loop
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple, Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

GAP_GO_SL_PCT           = 1.5
GAP_GO_TP_MULT          = 2.0
GAP_FILL_TP_FILL_RATIO  = 0.65
GAP_FILL_SL_BUFFER_PCT  = 0.5

GAP_GO_SCORE_BASE       = 0.72
GAP_FILL_SCORE_BASE     = 0.65

GAP_GO_MAX_WAIT_HOURS   = 2.0
GAP_FILL_MAX_WAIT_HOURS = 3.0
GAP_FILL_TOO_LATE_RATIO = 0.75

STOCK_EOD_HOUR          = 15
STOCK_EOD_MINUTE        = 45


# ---------------------------------------------------------------------------
# GapSetup
# ---------------------------------------------------------------------------

@dataclass
class GapSetup:
    symbol:        str
    asset_class:   str       # "stock" or "crypto"
    gap_type:      str       # "gap_and_go" or "gap_fill"
    direction:     str       # "long" or "short"
    gap_pct:       float     # signed: +4.2 or -2.8
    prev_close:    float
    gap_open:      float     # today's open
    vol_spike:     float     # volume spike ratio at open
    registered_at: datetime  # UTC

    attempts:      int  = 0
    fired:         bool = False
    status:        str  = "WATCHING"   # WATCHING / CONFIRMED / EXPIRED / FAILED

    @property
    def abs_gap(self) -> float:
        return abs(self.gap_pct)

    def age_hours(self, now_utc: datetime) -> float:
        return (now_utc - self.registered_at).total_seconds() / 3600.0

    def fill_pct(self, live_price: float) -> float:
        """How far price has retraced back toward prev_close (0-100%)."""
        gap_size = abs(self.gap_open - self.prev_close)
        if gap_size == 0:
            return 0.0
        retraced = abs(live_price - self.gap_open)
        return min(100.0, retraced / gap_size * 100.0)


# ---------------------------------------------------------------------------
# GapWatchlist
# ---------------------------------------------------------------------------

class GapWatchlist:
    """
    Queue of gap setups waiting for intraday confirmation.
    Thread-safe for reads; register/expire are single-threaded (scan loop).
    """

    def __init__(self):
        self._queue: Dict[str, GapSetup] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, setup: GapSetup) -> None:
        self._queue[setup.symbol] = setup
        logger.info(
            "[GAP QUEUE] REGISTERED %s  %s %s  gap=%.2f%%  vol=%.2fx",
            setup.symbol, setup.gap_type.upper(), setup.direction.upper(),
            setup.gap_pct, setup.vol_spike,
        )

    def is_pending(self, symbol: str) -> bool:
        return symbol in self._queue and not self._queue[symbol].fired

    def pending_symbols(self) -> List[str]:
        return [s for s, g in self._queue.items() if not g.fired]

    def size(self) -> int:
        return len([g for g in self._queue.values() if not g.fired])

    # ------------------------------------------------------------------
    # Confirmation check -- called by scanner hot loop
    # ------------------------------------------------------------------

    def check(
        self,
        symbol:    str,
        live_price: float,
        open_price: float,   # today's open from PriceData
        high:      float,
        low:       float,
        volume:    float,
        avg_volume: float,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check whether a pending gap setup has confirmed.

        Returns (confirmed, payload) or (False, None).
        Marks setup as fired / failed internally.
        """
        setup = self._queue.get(symbol)
        if setup is None or setup.fired:
            return False, None

        setup.attempts += 1
        now_utc = datetime.now(timezone.utc)

        vol_spike = volume / avg_volume if avg_volume > 0 else setup.vol_spike

        # --- Failure / too-late checks ---
        if setup.gap_type == "gap_and_go":
            # Gap reversed -- price filled back below open
            if setup.gap_pct > 0 and live_price < setup.gap_open * 0.995:
                self._mark(setup, "FAILED")
                return False, None
            if setup.gap_pct < 0 and live_price > setup.gap_open * 1.005:
                self._mark(setup, "FAILED")
                return False, None

        elif setup.gap_type == "gap_fill":
            # Already too late -- 75%+ retraced
            if setup.fill_pct(live_price) >= GAP_FILL_TOO_LATE_RATIO * 100:
                self._mark(setup, "FAILED")
                return False, None

        # --- Confirmation logic ---
        confirmed = False

        if setup.gap_type == "gap_and_go":
            confirmed = self._confirm_gap_and_go(setup, live_price, open_price, high, low, vol_spike)
        else:
            confirmed = self._confirm_gap_fill(setup, live_price, open_price, high, low, vol_spike)

        if confirmed:
            self._mark(setup, "CONFIRMED")
            payload = self._build_payload(setup, live_price, vol_spike, now_utc)
            logger.info(
                "[GAP QUEUE] CONFIRMED %s %s after %.1fh (%d checks) -- firing signal",
                symbol, setup.gap_type.upper(), setup.age_hours(now_utc), setup.attempts,
            )
            return True, payload

        return False, None

    def _confirm_gap_and_go(
        self, setup: GapSetup,
        live_price: float, open_price: float, high: float, low: float, vol_spike: float
    ) -> bool:
        """
        Gap & Go: price holding above gap open, continuing in gap direction,
        volume still elevated.
        """
        # Price must still be on gap side of open
        if setup.gap_pct > 0 and live_price < open_price:
            return False
        if setup.gap_pct < 0 and live_price > open_price:
            return False

        # Price moving further in gap direction (above open for long)
        if setup.direction == "long" and live_price <= open_price * 1.001:
            return False
        if setup.direction == "short" and live_price >= open_price * 0.999:
            return False

        # Volume still elevated
        if vol_spike < 1.2:
            return False

        return True

    def _confirm_gap_fill(
        self, setup: GapSetup,
        live_price: float, open_price: float, high: float, low: float, vol_spike: float
    ) -> bool:
        """
        Gap Fill: price still in gap zone, showing reversal sign,
        volume not surging further in gap direction.
        """
        # Price still in gap zone (hasn't already filled)
        if setup.gap_pct > 0 and live_price <= setup.prev_close:
            return False
        if setup.gap_pct < 0 and live_price >= setup.prev_close:
            return False

        # Reversal signal: price moved back from open or wick shows rejection
        reversal = False
        if setup.gap_pct > 0:
            # Gap up -- want to see price below open or wick at high
            if live_price < open_price:
                reversal = True
            wick_up = (high - max(open_price, live_price))
            body = abs(live_price - open_price)
            if body > 0 and wick_up / (body + wick_up) > 0.4:
                reversal = True
        else:
            # Gap down -- want to see price above open or wick at low
            if live_price > open_price:
                reversal = True
            wick_dn = (min(open_price, live_price) - low)
            body = abs(live_price - open_price)
            if body > 0 and wick_dn / (body + wick_dn) > 0.4:
                reversal = True

        if not reversal:
            return False

        # Don't fade if volume is surging hard (gap may be real momentum)
        if vol_spike > 2.5:
            return False

        return True

    # ------------------------------------------------------------------
    # Expiry
    # ------------------------------------------------------------------

    def expire_stale(self, now_utc: datetime, now_et: datetime) -> None:
        """Remove expired and EOD entries. Call once per scan loop."""
        to_remove = []

        for symbol, setup in self._queue.items():
            if setup.fired:
                to_remove.append(symbol)
                continue

            age_h = setup.age_hours(now_utc)
            max_wait = GAP_GO_MAX_WAIT_HOURS if setup.gap_type == "gap_and_go" else GAP_FILL_MAX_WAIT_HOURS

            if age_h > max_wait:
                logger.info("[GAP QUEUE] %s expired after %.1fh -- removed", symbol, age_h)
                setup.status = "EXPIRED"
                to_remove.append(symbol)
                continue

            # Stock EOD cleanup
            if (setup.asset_class == "stock"
                    and (now_et.hour, now_et.minute) >= (STOCK_EOD_HOUR, STOCK_EOD_MINUTE)):
                logger.info("[GAP QUEUE] %s EOD cleanup", symbol)
                to_remove.append(symbol)

        for s in to_remove:
            self._queue.pop(s, None)

    # ------------------------------------------------------------------
    # GUI summary
    # ------------------------------------------------------------------

    def summary(self) -> List[Dict]:
        """Return list of dicts for GUI refresh."""
        now_utc = datetime.now(timezone.utc)
        rows = []
        for s in self._queue.values():
            rows.append({
                "symbol":    s.symbol,
                "gap_type":  "G&GO" if s.gap_type == "gap_and_go" else "FILL",
                "direction": s.direction.upper(),
                "gap_pct":   s.gap_pct,
                "gap_open":  s.gap_open,
                "prev_close": s.prev_close,
                "age_min":   round(s.age_hours(now_utc) * 60),
                "attempts":  s.attempts,
                "status":    s.status,
                "fired":     s.fired,
            })
        return rows

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mark(self, setup: GapSetup, status: str) -> None:
        setup.status = status
        if status in ("CONFIRMED", "FAILED"):
            setup.fired = True

    def _build_payload(
        self, setup: GapSetup, live_price: float, vol_spike: float, now_utc: datetime
    ) -> Dict[str, Any]:
        abs_gap = setup.abs_gap

        if setup.gap_type == "gap_and_go":
            sl_pct  = GAP_GO_SL_PCT
            tp_pct  = max(round(abs_gap * GAP_GO_TP_MULT, 2), 1.5)
            score   = min(0.92, GAP_GO_SCORE_BASE
                          + min(0.15, (abs_gap - 3.0) * 0.03)
                          + min(0.07, (vol_spike - 1.0) * 0.05))
            reason  = "GAP-GO: %.1f%% gap %s vol=%.1fx -- confirmed continuation" % (
                setup.gap_pct, "up" if setup.gap_pct > 0 else "down", vol_spike)
            trail   = "two_bar"
        else:
            fill_target = abs((setup.prev_close - live_price) * GAP_FILL_TP_FILL_RATIO)
            tp_pct      = max(round(fill_target / live_price * 100, 2), 1.0)
            if setup.gap_pct > 0:
                sl_price = setup.gap_open * (1 + GAP_FILL_SL_BUFFER_PCT / 100)
            else:
                sl_price = setup.gap_open * (1 - GAP_FILL_SL_BUFFER_PCT / 100)
            sl_pct  = max(round(abs(sl_price - live_price) / live_price * 100, 2), 0.5)
            score   = min(0.88, GAP_FILL_SCORE_BASE
                          + min(0.15, abs_gap * 0.015)
                          - min(0.10, (vol_spike - 1.0) * 0.08))
            reason  = "GAP-FILL: %.1f%% gap %s vol=%.1fx -- reversal confirmed" % (
                setup.gap_pct, "up" if setup.gap_pct > 0 else "down", vol_spike)
            trail   = "none"

        if setup.direction == "long":
            structural_stop = live_price * (1 - sl_pct / 100)
        else:
            structural_stop = live_price * (1 + sl_pct / 100)

        return {
            "symbol":                setup.symbol,
            "asset_class":           setup.asset_class,
            "direction":             setup.direction,
            "entry_price":           live_price,
            "current_price":         live_price,
            "move_pct":              setup.gap_pct,
            "volume_spike":          round(vol_spike, 3),
            "confidence":            round(score, 3),
            "escalation":            1,
            "timestamp":             now_utc.isoformat(),
            "signal_source":         "gap_scanner",
            "strategy_name":         "gap_scanner",
            "broker":                "alpaca" if setup.asset_class == "stock" else "coinbase",
            "stop_loss_pct":         sl_pct,
            "take_profit_pct":       tp_pct,
            "structural_stop_price": round(structural_stop, 6),
            "gap_pct":               round(setup.gap_pct, 3),
            "gap_type":              setup.gap_type,
            "prev_close":            round(setup.prev_close, 6),
            "today_open":            round(setup.gap_open, 6),
            "preferred_trail_mode":  trail,
            "reason":                reason,
            "bars_since_breakout":   0,
        }
