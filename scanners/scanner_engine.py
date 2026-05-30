"""
scanners/scanner_engine.py
==========================
Orchestrates all event-driven scanners.

Wires each scanner to the breakout_receiver and market_scanner, then starts
them in their own daemon threads. The main bot_engine calls start() once
after all components are initialized.

Adding a new scanner:
  1. Import it here
  2. Instantiate it in _build_scanners()
  3. It inherits all wiring and lifecycle from BaseEventScanner

Current scanners:
  GapScanner     -- overnight gaps (moved into BreakoutScanner, disabled here)
  NR7Scanner     -- narrowest range in 7 days (daily close, stocks+crypto)
  FundingScanner -- crypto funding rate extreme (Bybit/Binance, 8h cooldown)

BreakoutScanner also runs:
  GapWatchlist       -- gap detect + confirm using already-fetched price data
  VolumeSurgeTracker -- volume > 3x avg (intraday, tick-based, 4h cooldown)
"""

import logging
from typing import List, Optional

import config
from scanners.base_scanner import BaseEventScanner
from scanners.gap_scanner import GapScanner
from scanners.nr7_scanner import NR7Scanner

logger = logging.getLogger(__name__)


class ScannerEngine:
    """
    Manages all event-driven scanners.

    Usage in bot_engine.py:
        self.scanner_engine = ScannerEngine()
        self.scanner_engine.wire(
            receiver       = breakout_receiver,
            market_scanner = self.market_scanner,
        )
        self.scanner_engine.start()
    """

    def __init__(self):
        self.scanners: List[BaseEventScanner] = self._build_scanners()
        logger.info(
            "[ScannerEngine] Initialized with %d event scanner(s): %s",
            len(self.scanners),
            ", ".join(s.name for s in self.scanners),
        )

    def _build_scanners(self) -> List[BaseEventScanner]:
        scanners = []

        # ── Gap Scanner ───────────────────────────────────────────────────
        # Re-enabled: the planned migration to BreakoutScanner was never completed.
        # GapWatchlist exists but is not wired anywhere -- standalone GapScanner
        # is the only active gap detection path.
        gap = GapScanner(
            stock_symbols  = list(config.STOCK_WATCHLIST),
            crypto_symbols = list(config.CRYPTO_WATCHLIST),
        )
        scanners.append(gap)

        # ── NR7 Scanner ───────────────────────────────────────────────────
        nr7 = NR7Scanner(
            stock_symbols  = list(config.STOCK_WATCHLIST),
            crypto_symbols = list(config.CRYPTO_WATCHLIST),
        )
        scanners.append(nr7)

        # ── Volume Surge Scanner ──────────────────────────────────────────
        # Runs inside BreakoutScanner (reuses 5m bar data already being fetched).
        # No separate thread needed here.

        # ── Funding Rate Scanner ──────────────────────────────────────────
        from scanners.funding_scanner import FundingScanner
        scanners.append(FundingScanner())

        return scanners

    def wire(self, receiver, market_scanner) -> None:
        """Wire receiver and market_scanner into all managed scanners."""
        for scanner in self.scanners:
            scanner.set_receiver(receiver)
            scanner.set_market_scanner(market_scanner)
        logger.info("[ScannerEngine] All scanners wired to receiver + market_scanner")

    def start(self) -> None:
        """Start all scanner threads."""
        for scanner in self.scanners:
            scanner.start()
        logger.info("[ScannerEngine] All %d scanner(s) started", len(self.scanners))

    def stop(self) -> None:
        """Signal all scanners to stop (graceful shutdown)."""
        for scanner in self.scanners:
            scanner.stop()

    def reset_daily_cooldowns(self) -> None:
        """
        Call at midnight to reset per-day cooldowns.
        Hook this into bot_engine's midnight_reset if needed.
        """
        for scanner in self.scanners:
            scanner.reset_all_cooldowns()

    def get_stats(self) -> dict:
        """Return signal stats for all scanners (for dashboard/reporting)."""
        return {
            s.name: {
                "signals_fired":    s.signals_fired,
                "signals_accepted": s.signals_accepted,
            }
            for s in self.scanners
        }
