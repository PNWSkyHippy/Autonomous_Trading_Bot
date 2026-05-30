"""
scanners/base_scanner.py
========================
Base class for all event-driven scanners.

Event scanners are fundamentally different from the periodic strategy engine:
  - Strategy engine: polls every symbol every N minutes, runs math, fires if conditions met
  - Event scanners: sleep until a specific event occurs, fire once, go back to sleep

Each scanner runs in its own daemon thread. When an event fires it pushes a
signal payload directly to breakout_receiver.receive_signal() -- the same
injection path used by the external BreakoutScanner. No new receiver needed.

Cooldown tracker prevents the same symbol from firing more than once per
cooldown window (default 24h for daily events, shorter for intraday).
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class BaseEventScanner:
    """
    Base class for event-driven scanners.

    Subclasses implement:
      run_once()  -- called once per check interval; return True if scanner
                     did work (for logging), False if nothing to do yet
    """

    def __init__(self, name: str, cooldown_hours: float = 24.0, check_interval_sec: int = 60):
        self.name               = name
        self.cooldown_hours     = cooldown_hours
        self.check_interval_sec = check_interval_sec

        self._cooldowns: Dict[str, datetime] = {}   # symbol -> last fire time (UTC)
        self._lock          = threading.Lock()
        self._receiver      = None   # set by scanner_engine / bot_engine
        self._scanner_ref   = None   # market_scanner for price lookups
        self._running       = False
        self._thread: Optional[threading.Thread] = None

        self.signals_fired  = 0
        self.signals_accepted = 0

    # ------------------------------------------------------------------
    # Wiring (called by scanner_engine)
    # ------------------------------------------------------------------

    def set_receiver(self, receiver) -> None:
        """Wire to breakout_receiver (or any receiver with .receive_signal())."""
        self._receiver = receiver

    def set_market_scanner(self, market_scanner) -> None:
        """Wire to market_scanner for live price lookups."""
        self._scanner_ref = market_scanner

    # ------------------------------------------------------------------
    # Cooldown management
    # ------------------------------------------------------------------

    def is_on_cooldown(self, symbol: str) -> bool:
        with self._lock:
            last = self._cooldowns.get(symbol)
            if last is None:
                return False
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            return elapsed < self.cooldown_hours * 3600

    def mark_fired(self, symbol: str) -> None:
        with self._lock:
            self._cooldowns[symbol] = datetime.now(timezone.utc)

    def reset_cooldown(self, symbol: str) -> None:
        with self._lock:
            self._cooldowns.pop(symbol, None)

    def reset_all_cooldowns(self) -> None:
        """Call at midnight to clear daily-event cooldowns for new session."""
        with self._lock:
            self._cooldowns.clear()
        logger.info("[%s] All cooldowns reset for new session", self.name)

    # ------------------------------------------------------------------
    # Signal injection
    # ------------------------------------------------------------------

    def push_signal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Push a signal to breakout_receiver. Returns the receiver's response dict."""
        if self._receiver is None:
            logger.warning("[%s] push_signal called but no receiver wired", self.name)
            return {"accepted": False, "reason": "no_receiver_wired"}

        sym = payload.get("symbol", "?")
        self.signals_fired += 1

        try:
            result = self._receiver.receive_signal(payload)
            if result.get("accepted"):
                self.signals_accepted += 1
                logger.info(
                    "[%s] Signal ACCEPTED  %s %s  trade_id=%s",
                    self.name, sym, payload.get("direction", "?"),
                    result.get("trade_id", "?"),
                )
            else:
                logger.info(
                    "[%s] Signal REJECTED  %s  reason=%s",
                    self.name, sym, result.get("reason", "?"),
                )
            return result
        except Exception as e:
            logger.error("[%s] push_signal exception for %s: %s", self.name, sym, e, exc_info=True)
            return {"accepted": False, "reason": str(e)}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the scanner in a background daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=self.name,
        )
        self._thread.start()
        logger.info("[%s] Scanner started (check interval %ds)", self.name, self.check_interval_sec)

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        while self._running:
            try:
                self.run_once()
            except Exception as e:
                logger.error("[%s] run_once() error: %s", self.name, e, exc_info=True)
            time.sleep(self.check_interval_sec)

    def run_once(self) -> None:
        """Override in subclass. Called every check_interval_sec."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Helpers subclasses can use
    # ------------------------------------------------------------------

    def get_live_price(self, symbol: str, asset_class: str = "stock") -> Optional[float]:
        """Get current price via the wired market_scanner."""
        if self._scanner_ref is None:
            return None
        try:
            return self._scanner_ref.get_current_price(symbol, asset_class)
        except Exception as e:
            logger.warning("[%s] get_live_price(%s) failed: %s", self.name, symbol, e)
            return None

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def et_now() -> datetime:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
