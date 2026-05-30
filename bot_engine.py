"""
bot_engine.py — Main entry point for the Autonomous Day Trading Bot
Trading Bot v2

Usage:
    python bot_engine.py            # Normal operation
    python bot_engine.py --verbose  # Verbose signal debugging (large log files!)
    python bot_engine.py -v         # Same as --verbose

IMPORTANT — TIMEZONE NOTE:
    This bot runs on a Windows machine set to Pacific Time (Spokane, WA).
    The schedule library has NO timezone awareness — it fires at whatever
    the system clock says. All schedule.at() times below are therefore in
    PACIFIC TIME. The actual ET times are noted in comments.

    Market hours gate inside _run_stock_scan() uses ZoneInfo("America/New_York")
    and checks real ET time regardless of machine timezone — this is the
    authoritative gate for stock scanning.

    Schedule times (PT = machine time, ET = actual market time):
      Stock close:   12:45 PT = 15:45 ET (15 min before market close)
      EOD report:    14:00 PT = 17:00 ET
      Daily scan:    13:30 PT = 16:30 ET (30 min after market close)
      ML retrain:    15:00 PT = 18:00 ET
      Weekly log:    00:00 PT Sunday (midnight, covers prev Sun-Sat)
      Midnight reset:00:01 PT daily (resets session counters for new day)
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

import schedule

import config
from api_server import start_api_server
from core.breakout_receiver import breakout_receiver
from core.risk_manager import RiskManager
from core.trade_executor import TradeExecutor
from core.position_monitor import PositionMonitor
from core.broker_manager import BrokerManager
from data.database import Database
from scanners.market_scanner import MarketScanner
from strategies.strategy_engine import StrategyEngine
from intelligence.ml_scorer import MLScorer
from intelligence.condition_detector import ConditionDetector
from reporting.report_generator import ReportGenerator
from reporting.weekly_tradelog import run_weekly_export
from scanners.daily_market_scanner import (
    run_daily_scan,
    inject_scan_results_into_config,
)
from scanners.scanner_engine import ScannerEngine


def parse_args():
    parser = argparse.ArgumentParser(
        description="Autonomous Day Trading Bot v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bot_engine.py                 Normal operation
  python bot_engine.py --verbose       Debug mode: logs every signal condition
  python bot_engine.py -v              Same as --verbose

WARNING: Verbose mode writes a log entry for every signal condition checked
on every symbol every scan cycle. Log files will grow very quickly.
Use verbose mode for short diagnostic sessions only.
        """
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose signal logging for debugging"
    )
    return parser.parse_args()


def setup_logging(verbose: bool) -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    log_level = logging.DEBUG if verbose else logging.INFO

    file_handler = logging.FileHandler("logs/bot.log", encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    ))

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Suppress noisy third-party HTTP/network loggers regardless of verbose mode.
    # These dump full request headers, cookies, and raw OHLC payloads at DEBUG —
    # completely swamping strategy verbose output. Our own code still logs freely.
    _NOISY = (
        "urllib3",
        "urllib3.connectionpool",
        "urllib3.util.retry",
        "ccxt",
        "ccxt.base",
        "ccxt.base.exchange",
        "ccxt.pro",
        "asyncio",
        "httpx",
        "httpcore",
        "httpcore.http11",
        "httpcore.connection",
        "websockets",
        "websockets.client",
        "websockets.protocol",
        "aiohttp",
        "aiohttp.client",
        "requests",
        "requests.packages.urllib3",
        "yfinance",
        "yfinance.base",
        "yfinance.utils",
        "peewee",
        "PIL",
    )
    for _name in _NOISY:
        logging.getLogger(_name).setLevel(logging.WARNING)

    return logging.getLogger("BotEngine")


def _is_market_open() -> bool:
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    now_et  = datetime.now(ZoneInfo("America/New_York"))
    weekday = now_et.weekday()
    hour    = now_et.hour
    minute  = now_et.minute

    if weekday > 4:
        return False

    after_open  = hour > 10 or (hour == 10 and minute >= 0)
    before_close= hour < 15 or (hour == 15 and minute < 45)

    return after_open and before_close


def _et_now_str() -> str:
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%a %H:%M ET")


class BotEngine:

    def __init__(self):
        self.logger = logging.getLogger("BotEngine")
        self.logger.info("=" * 60)
        self.logger.info("  Autonomous Day Trading Bot v2 - Starting Up")
        self.logger.info("=" * 60)

        if config.VERBOSE_MODE:
            self.logger.info("*** VERBOSE MODE ACTIVE - Signal details will be logged ***")

        self.db                = Database(config.DB_PATH)
        self.risk_manager      = RiskManager()
        self.broker_manager    = BrokerManager()
        self.trade_executor    = TradeExecutor()
        self.position_monitor  = PositionMonitor()
        self.strategy_engine   = StrategyEngine()
        self.ml_scorer         = MLScorer()
        self.condition_detector= ConditionDetector()
        self.market_scanner    = MarketScanner()
        self.report_generator  = ReportGenerator()

        self.logger.info("All components initialized successfully.")
        # Wire breakout receiver
        breakout_receiver.set_executor(self.trade_executor)
        breakout_receiver.set_scanner(self.market_scanner)

        # Wire re-entry path: market_scanner -> receiver, position_monitor -> callback
        self.market_scanner.set_reentry_receiver(breakout_receiver)

        # Wire and start event-driven scanners (gap, NR7, volume surge, funding)
        self.scanner_engine = ScannerEngine()
        self.scanner_engine.wire(
            receiver       = breakout_receiver,
            market_scanner = self.market_scanner,
        )
        self.scanner_engine.start()

        def _on_breakout_win_closed(
            symbol: str, won: bool, exit_price: float, direction: str
        ) -> None:
            if won:
                try:
                    self.market_scanner.schedule_reentry(symbol, exit_price, direction)
                except Exception as e:
                    self.logger.error(
                        f"[REENTRY HOOK] schedule failed for {symbol}: {e}"
                    )

        self.position_monitor.set_breakout_win_callback(_on_breakout_win_closed)
        start_api_server(port=getattr(config, 'BOT_API_PORT', 8181))
        self.logger.info("[API] Breakout receiver active on port "
                         f"{getattr(config, 'BOT_API_PORT', 8181)}")
        self._log_strategy_status()

        self._load_scan_results_on_startup()
        self._clear_stuck_flags_on_startup()
        self._reconcile_positions()
        self._register_scheduled_jobs()
        self._start_candle_refresh()

    def _clear_stuck_flags_on_startup(self):
        try:
            open_trades = self.db.get_open_trades()
            if not open_trades:
                return
            cleared = 0
            for trade in open_trades:
                tid = trade["trade_id"]
                sym = trade["symbol"]
                was_stuck = int(self.db.get_state(f"close_stuck_{tid}", default=0))
                if was_stuck:
                    self.db.set_state(f"close_stuck_{tid}", 0)
                    self.db.set_state(f"close_attempts_{tid}", 0)
                    self.db.set_state(f"close_last_attempt_{tid}", 0)
                    self.logger.info(
                        f"[STARTUP] Cleared STUCK flag for {sym} — "
                        f"position monitor will retry close"
                    )
                    cleared += 1
            if cleared:
                self.logger.info(
                    f"[STARTUP] Auto-cleared {cleared} STUCK trade(s). "
                    f"Position monitor will retry closes within 30s."
                )
            else:
                self.logger.info("[STARTUP] No STUCK trades found — clean start.")
        except Exception as e:
            self.logger.error(f"Startup STUCK clear error: {e}")

    def _load_scan_results_on_startup(self):
        try:
            inject_scan_results_into_config()
        except Exception as e:
            self.logger.warning(f"Startup scan inject error: {e}")

    def _log_strategy_status(self):
        strategies = self.strategy_engine.get_all_strategies()
        enabled    = [s for s in strategies if s.enabled]
        disabled   = [s for s in strategies if not s.enabled]
        self.logger.info(
            f"Strategy engine loaded: {len(enabled)} enabled, {len(disabled)} disabled"
        )
        for s in enabled:
            self.logger.info(f"  [ENABLED]  {s.name}")
        for s in disabled:
            self.logger.info(f"  [DISABLED] {s.name}")

    def _reconcile_positions(self):
        try:
            import alpaca_trade_api as tradeapi
            api = tradeapi.REST(
                config.ALPACA_API_KEY,
                config.ALPACA_SECRET_KEY,
                config.ALPACA_BASE_URL
            )
            try:
                alpaca_positions = api.list_positions()
                alpaca_symbols   = {p.symbol for p in alpaca_positions}
            except Exception as e:
                self.logger.warning(f"Reconciliation: could not fetch Alpaca positions: {e}")
                return

            open_trades   = self.db.get_open_trades()
            stock_trades  = [t for t in open_trades if t.get("asset_class") == "stock"
                             and t.get("broker", "alpaca") in ("alpaca", "")]

            if not stock_trades:
                return

            ghost_count = 0
            for trade in stock_trades:
                symbol = trade["symbol"]
                if symbol not in alpaca_symbols:
                    self.logger.warning(
                        f"Reconciliation: {symbol} in DB but not in Alpaca — "
                        f"marking as ghost (trade_id={trade['trade_id']})"
                    )
                    try:
                        self.db.close_trade(
                            trade_id    = trade["trade_id"],
                            exit_price  = trade["entry_price"],
                            exit_reason = "reconciled_ghost",
                            pnl         = 0.0,
                            pnl_pct     = 0.0
                        )
                        ghost_count += 1
                    except Exception as e:
                        self.logger.error(f"Reconciliation: failed to close ghost {symbol}: {e}")

            if ghost_count > 0:
                self.logger.info(f"Reconciliation: {ghost_count} ghost position(s) cleaned.")
            else:
                self.logger.info("Reconciliation: all DB stock positions confirmed in Alpaca.")

            # ── IBKR reconcile pass (Opus 2026-05-29) ─────────────────────
            # IBKR is the preferred stock broker; its DB trades were excluded
            # from the Alpaca check above. Reconcile them against live IBKR
            # positions so server-side bracket fills don't leave DB ghosts.
            try:
                from core.trade_executor import executor as _exec
                ibkr = getattr(_exec, "_ibkr", None)
                if ibkr is not None and ibkr.is_available():
                    ibkr_symbols = {p.contract.symbol for p in ibkr._ib.positions()}
                    ibkr_trades  = [
                        t for t in open_trades
                        if t.get("asset_class") == "stock"
                        and t.get("broker") == "ibkr"
                    ]
                    ibkr_ghosts = 0
                    for trade in ibkr_trades:
                        if trade["symbol"] not in ibkr_symbols:
                            self.logger.warning(
                                f"Reconciliation: {trade['symbol']} in DB but not "
                                f"in IBKR — marking ghost (trade_id={trade['trade_id']})"
                            )
                            try:
                                self.db.close_trade(
                                    trade_id    = trade["trade_id"],
                                    exit_price  = trade["entry_price"],
                                    exit_reason = "reconciled_ghost",
                                    pnl         = 0.0,
                                    pnl_pct     = 0.0,
                                )
                                ibkr_ghosts += 1
                            except Exception as e:
                                self.logger.error(
                                    f"Reconciliation: failed to close IBKR ghost "
                                    f"{trade['symbol']}: {e}"
                                )
                    if ibkr_ghosts:
                        self.logger.info(
                            f"Reconciliation: {ibkr_ghosts} IBKR ghost(s) cleaned."
                        )
            except Exception as e:
                self.logger.warning(f"IBKR reconciliation error: {e}")
        except Exception as e:
            self.logger.error(f"Reconciliation error: {e}", exc_info=True)

    def _register_scheduled_jobs(self):
        self.logger.info("Registering scheduled jobs...")

        schedule.every(config.CRYPTO_SCAN_INTERVAL_SEC).seconds.do(self._run_crypto_scan)
        schedule.every(config.STOCK_SCAN_INTERVAL_SEC).seconds.do(self._run_stock_scan)
        schedule.every(30).seconds.do(self.position_monitor.check_positions)
        schedule.every(config.CAPITAL_SYNC_MIN).minutes.do(self._sync_capital)
        schedule.every(1).minutes.do(self._flush_alert_digest)

        for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
            getattr(schedule.every(), day).at("12:45").do(self._close_stocks_eod)

        for day in ["monday", "tuesday", "wednesday", "thursday", "friday",
                    "saturday", "sunday"]:
            getattr(schedule.every(), day).at("14:00").do(self._end_of_day)

        for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
            getattr(schedule.every(), day).at("13:30").do(self._run_daily_scan)

        schedule.every().day.at("15:00").do(self._retrain_ml)
        schedule.every().sunday.at("00:00").do(run_weekly_export)
        schedule.every().day.at("00:01").do(self._midnight_reset)

        self.logger.info("All scheduled jobs registered.")
        self.logger.info("  - Crypto scan:      every 5 min (24/7)")
        self.logger.info("  - Stock scan:       every 60s (gated: Mon-Fri 10:00-15:44 ET)")
        self.logger.info("  - Stock EOD close:  15:45 ET (12:45 PT) weekdays")
        self.logger.info("  - Position monitor: every 30s (24/7)")
        self.logger.info("  - Capital sync:     every 15 min")
        self.logger.info("  - EOD report:       17:00 ET (14:00 PT) daily")
        self.logger.info("  - Daily scan:       16:30 ET (13:30 PT) weekdays")
        self.logger.info("  - ML retrain:       18:00 ET (15:00 PT) daily")
        self.logger.info("  - Midnight reset:   00:01 PT daily (session counters)")
        self.logger.info("  - Weekly trade log: Sunday 00:00 PT (covers prev Sun-Sat)")

    def _start_candle_refresh(self):
        try:
            from core.candle_manager import candle_manager
            crypto_syms = list(getattr(config, "CRYPTO_WATCHLIST", []))
            stock_syms  = list(getattr(config, "STOCK_WATCHLIST",  []))
            candle_manager.start_refresh_loop(
                crypto_symbols = crypto_syms,
                stock_symbols  = stock_syms,
                crypto_tfs     = ["5m", "15m", "1h"],
                stock_tfs      = ["5Min", "15Min", "1Hour"],
                interval_sec   = 300,   # 5 minutes — matches crypto scan interval
            )
        except Exception as e:
            self.logger.warning(f"CandleManager refresh loop failed to start: {e}")

    def _midnight_reset(self):
        try:
            self.db.reset_session_state()
            self.logger.info(
                f"[MIDNIGHT RESET] Session counters reset for new day "
                f"({datetime.now().strftime('%Y-%m-%d')})"
            )
        except Exception as e:
            self.logger.error(f"Midnight reset error: {e}")
        try:
            self.scanner_engine.reset_daily_cooldowns()
        except Exception as e:
            self.logger.error(f"[MIDNIGHT RESET] Scanner cooldown reset error: {e}")

    def _run_stock_scan(self):
        import threading
        threading.Thread(target=self._do_stock_scan, daemon=True).start()

    def _do_stock_scan(self):
        try:
            if not _is_market_open():
                return
            self.market_scanner.scan_stocks()
        except Exception as e:
            self.logger.error(f"Stock scan error: {e}", exc_info=True)

    def _run_crypto_scan(self):
        import threading
        t = threading.Thread(target=self._do_crypto_scan, daemon=True)
        t.start()

    def _do_crypto_scan(self):
        try:
            self.logger.info("[SCAN] Crypto scan starting...")
            self.market_scanner.scan_crypto()
            self.logger.info("[SCAN] Crypto scan complete.")
        except Exception as e:
            self.logger.error(f"Crypto scan error: {e}", exc_info=True)

    def _run_daily_scan(self):
        try:
            self.logger.info("[DAILY SCAN] Running post-market daily scan...")
            run_daily_scan(top_n=10)
        except Exception as e:
            self.logger.error(f"Daily scan error: {e}", exc_info=True)

    def _close_stocks_eod(self):
        """
        Close all open stock positions before market close.
        Two-pass approach:
          Pass 1: Close all stock trades in the DB via their recorded broker.
          Pass 2: Sweep actual Alpaca positions and close anything still open
                  that isn't in the DB (manually opened positions, ghosts, etc).
        """
        try:
            self.logger.info(f"15:45 ET stock EOD close starting ({_et_now_str()})...")
            from data.database import db as _db
            import alpaca_trade_api as tradeapi

            closed_count  = 0
            skipped_count = 0
            failed_syms   = []

            # ── Pass 1: Close DB-tracked stock trades ─────────────────────
            open_trades  = _db.get_open_trades()
            stock_trades = [t for t in open_trades if t.get("asset_class") == "stock"]

            for trade in stock_trades:
                if trade.get("is_overnight"):
                    skipped_count += 1
                    continue
                try:
                    current_price = self.market_scanner.get_current_price(
                        trade["symbol"], "stock"
                    )
                    if not current_price:
                        current_price = trade["entry_price"]
                except Exception:
                    current_price = trade["entry_price"]

                success = self.trade_executor.close_trade(
                    trade, current_price, "eod_close"
                )
                if success:
                    closed_count += 1
                else:
                    self.logger.error(f"EOD close FAILED for {trade['symbol']}")
                    failed_syms.append(trade["symbol"])

            # ── Pass 2: Sweep actual Alpaca positions ─────────────────────
            # Close anything Alpaca shows as open that isn't in our DB.
            # This catches manually opened positions and any DB misses.
            try:
                api = tradeapi.REST(
                    config.ALPACA_API_KEY,
                    config.ALPACA_SECRET_KEY,
                    config.ALPACA_BASE_URL
                )
                alpaca_positions = api.list_positions()
                db_symbols = {t["symbol"] for t in stock_trades}

                for pos in alpaca_positions:
                    sym = pos.symbol
                    if sym in db_symbols:
                        continue   # already handled in pass 1
                    try:
                        # Close via Alpaca market order
                        api.close_position(sym)
                        self.logger.info(
                            f"[EOD ALPACA SWEEP] Closed {sym} "
                            f"(not in DB — manual or ghost position)"
                        )
                        closed_count += 1
                    except Exception as e:
                        self.logger.error(
                            f"[EOD ALPACA SWEEP] Failed to close {sym}: {e}"
                        )
            except Exception as e:
                self.logger.warning(f"EOD Alpaca sweep error: {e}")

            # ── Pass 3: Sweep actual IBKR positions ───────────────────────
            # Catches positions opened on IBKR that weren't in the DB,
            # or that failed to close in Pass 1 because IBKR was briefly down.
            try:
                from core.trade_executor import executor as _exec
                ibkr = getattr(_exec, "_ibkr", None)
                if ibkr is not None and ibkr.is_available():
                    ibkr_positions = ibkr._ib.positions()
                    for pos in ibkr_positions:
                        sym = pos.contract.symbol
                        if sym in db_symbols:
                            continue   # already handled in pass 1
                        try:
                            success = ibkr.close_position(sym)
                            if success:
                                self.logger.info(
                                    f"[EOD IBKR SWEEP] Closed {sym} "
                                    f"(not in DB — manual or ghost position)"
                                )
                                closed_count += 1
                            else:
                                self.logger.error(
                                    f"[EOD IBKR SWEEP] Failed to close {sym}"
                                )
                        except Exception as e:
                            self.logger.error(
                                f"[EOD IBKR SWEEP] Error closing {sym}: {e}"
                            )
            except Exception as e:
                self.logger.warning(f"EOD IBKR sweep error: {e}")

            self.logger.info(
                f"Stock EOD close: {closed_count} closed, "
                f"{skipped_count} overnight skipped"
                + (f", {len(failed_syms)} failed: {failed_syms}" if failed_syms else "")
            )
        except Exception as e:
            self.logger.error(f"Stock EOD close error: {e}", exc_info=True)

    def _end_of_day(self):
        try:
            self.logger.info("Running end-of-day routine...")
            self.report_generator.generate_and_send_daily_report()
            self.logger.info(
                "[EOD] Daily report complete. Session counters remain active "
                "until midnight reset."
            )
        except Exception as e:
            self.logger.error(f"End-of-day routine error: {e}", exc_info=True)

    def _sync_capital(self):
        try:
            try:
                from core.settlement_tracker import settlement_tracker
                settlement_tracker.process_settlements()
            except Exception:
                pass
            self.broker_manager.sync_all_balances()
            breakdown = self.broker_manager.get_capital_breakdown()
            total     = breakdown.get("total_balance", 0.0)
            if total > 0:
                self.db.record_capital_snapshot(total)
            self._reconcile_positions()
        except Exception as e:
            self.logger.error(f"Capital sync error: {e}", exc_info=True)

    def _flush_alert_digest(self):
        try:
            from reporting.alerts import alert_manager
            alert_manager.flush_digest_if_due()
        except Exception as e:
            self.logger.error(f"Alert digest flush error: {e}")

    def _retrain_ml(self):
        try:
            self.logger.info("Retraining ML model...")
            result = self.ml_scorer.retrain()
            if result:
                self.logger.info("ML model retrained successfully.")
        except Exception as e:
            self.logger.error(f"ML retrain error: {e}", exc_info=True)

    def run(self):
        self.logger.info("Bot is running. Press Ctrl+C to stop.")
        self._sync_capital()
        try:
            while True:
                try:
                    schedule.run_pending()
                except Exception as _sched_err:
                    # A scheduled job threw an unhandled exception.
                    # Log it as CRITICAL (so the log scan catches it) then
                    # continue — do NOT let it kill the main loop.
                    self.logger.critical(
                        f"[SCHEDULER CRASH CAUGHT] Unhandled exception in scheduled "
                        f"job — bot continuing: {_sched_err}",
                        exc_info=True
                    )
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("Shutdown requested. Stopping bot.")


if __name__ == "__main__":
    args   = parse_args()
    config.VERBOSE_MODE = args.verbose
    logger = setup_logging(args.verbose)

    if args.verbose:
        print("\n" + "=" * 60)
        print("  VERBOSE MODE ENABLED")
        print("=" * 60 + "\n")

    bot = BotEngine()
    bot.run()
