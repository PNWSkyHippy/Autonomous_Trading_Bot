"""
=============================================================
  POSITION MONITOR
  Runs every 10 seconds. Watches all open positions for:
  - Stop-loss triggers
  - Take-profit triggers
  - Trailing stop adjustments (with logging)
  - Extended take-profit on continued momentum (with logging)
  - Performance-contingent time stops:
      < 0.2% profit after 2 hours → close (going nowhere)
      < 1.0% profit after 5 hours → close (too slow)
      grid_bot and dca_accumulator are EXEMPT from all time stops
  - Early loss identification:
      If trade is negative AND moving further wrong at 30 min → close
  - Hard time stop: 8 hours regardless of profit (grid_bot EXEMPT)
  - Stale position sell-off (3hr ADX/DI trend check)
  - Pivot point break exit (NEW):
      Long  breaks below S1 → close (structure failed)
      Short breaks above R1 → close (structure failed)
  - VWAP cross exit for ORB trades
  - Adaptive regime exit (EMA cross for trend; BB midline/RSI for mean rev)
  - EOD close for all stock positions

  Exit priority order:
    1.  STUCK check
    2.  EOD close (15:45 ET)
    3.  Stop-loss
    4.  ORB VWAP cross exit
    4b. Adaptive regime exit (adaptive_regime strategy only)
    5.  Early loss identification (30 min check, -0.5% threshold)
    6.  Performance-contingent time stop (2hr check, needs 0.2%)
    7.  Performance-contingent time stop (5hr check, needs 1.0%)
    8.  Hard time stop (8hr — grid_bot/dca_accumulator EXEMPT)
    9.  Take-profit + momentum rider
    10. Pivot point break
    11. Stale position ADX/DI check (3hr)
    12. Trailing stop update

  Close attempt throttle:
    If a close attempt fails the trade stays open in DB and would
    retry every 10s forever (monitor interval). Close attempts are
    throttled by MIN_CLOSE_RETRY_SEC so the broker is not hammered.
    We track close attempts in bot_state.
    After MAX_CLOSE_ATTEMPTS the trade is flagged STUCK and skipped
    until human intervenes. Dashboard shows STUCK trades.

  Stock positions are only monitored during market hours
  (Mon-Fri 09:30-16:00 ET). Crypto is monitored 24/7.
=============================================================
"""

import json as _json
import logging
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Callable

import numpy as np
import pandas as pd

import config
from data.database import db
from core.risk_manager import risk_manager

logger = logging.getLogger(__name__)

# ── Stale position check ─────────────────────────────────────────────────────
STALE_THRESHOLD_SEC = 3 * 60 * 60       # 3 hours before ADX/DI stale check fires

# ADX threshold below which trend is considered stalled
ADX_STALE_THRESHOLD = 25.0

# ── NEW: Time-based exit thresholds ──────────────────────────────────────────
# Performance-contingent: close if profit is below threshold after N hours.
# Prevents capital being locked in trades that never got going.
# NOTE: grid_bot trades are EXEMPT from all time stops (see _check_performance_time_stop)
# Grid strategies need time to oscillate — time stops kill them prematurely.
PERF_CHECK_2HR_SEC     = 2 * 60 * 60    # 2 hours — free up capital if going nowhere
PERF_CHECK_2HR_MIN_PCT = 0.2            # need at least 0.2% profit by 2hr

PERF_CHECK_5HR_SEC     = 5 * 60 * 60    # 5 hours — too slow for a day trade
PERF_CHECK_5HR_MIN_PCT = 1.0            # need at least 1.0% profit by 5hr

# Legacy aliases — kept so any external references don't break during migration
PERF_CHECK_1HR_SEC     = PERF_CHECK_2HR_SEC
PERF_CHECK_1HR_MIN_PCT = PERF_CHECK_2HR_MIN_PCT
PERF_CHECK_3HR_SEC     = PERF_CHECK_5HR_SEC
PERF_CHECK_3HR_MIN_PCT = PERF_CHECK_5HR_MIN_PCT

HARD_TIME_STOP_SEC     = 8 * 60 * 60    # 8 hours — hard kill (was 3.5hr)

# Early loss check: if trade is negative AND worsening at 30 minutes, kill it.
# Catches trades where thesis was wrong from the start.
EARLY_LOSS_CHECK_SEC   = 30 * 60        # 30 minutes (was 20min — too tight)
EARLY_LOSS_MIN_PCT     = -0.5           # if worse than -0.5% AND still falling (was -0.3%)

# Breakout fast-fail: real breakouts move immediately. If flat or negative at
# 10 minutes (2 bars on 5m), the setup failed — take the tiny loss and move on.
# This keeps losses at $5-15 instead of riding to the full 2-4.5% stop.
BREAKOUT_FAST_FAIL_SEC      = 10 * 60   # 10 minutes — 2 bars on 5m
BREAKOUT_FAST_FAIL_MAX_SEC  = 20 * 60   # 20 minutes — window closes, early_loss takes over
BREAKOUT_FAST_FAIL_MIN_PCT  = 0.10      # must be up at least 0.1% to NOT fast-fail

# Pivot point break exit
# Uses last N bars to calculate classic floor trader pivots (PP, S1, S2, R1, R2)
PIVOT_BARS             = 20             # bars to use for pivot calculation

# ── EOD close ────────────────────────────────────────────────────────────────
EOD_CLOSE_HOUR   = 15
EOD_CLOSE_MINUTE = 45

# ── Max close attempts ────────────────────────────────────────────────────────
# Raised from 3 to 8 — 3 was too low, a brief API hiccup caused permanent STUCK.
# With 30s monitor interval this gives 4 minutes of retries before flagging STUCK.
MAX_CLOSE_ATTEMPTS = 8

# Minimum seconds between close attempts — prevents hammering the broker API
# on repeated failures and gives TWS/Alpaca time to recover from transient errors.
MIN_CLOSE_RETRY_SEC = 60   # wait at least 60s between failed close attempts


def _is_breakout_trade(trade: dict) -> bool:
    if trade.get("strategy_name") == "breakout_scanner":
        return True
    try:
        meta = _json.loads(trade.get("indicators_json") or "{}")
        return meta.get("strategy_name") == "breakout_scanner"
    except Exception:
        return False


def _stock_market_open() -> bool:
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

    after_open   = hour > 9 or (hour == 9 and minute >= 30)
    before_close = hour < 16
    return after_open and before_close


def _approaching_eod() -> bool:
    """Returns True if it's time to close all stock positions (15:45 ET)."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    now_et  = datetime.now(ZoneInfo("America/New_York"))
    weekday = now_et.weekday()
    if weekday > 4:
        return False

    at_or_after = (
        now_et.hour > EOD_CLOSE_HOUR or
        (now_et.hour == EOD_CLOSE_HOUR and now_et.minute >= EOD_CLOSE_MINUTE)
    )
    before_close = now_et.hour < 16
    return at_or_after and before_close


def _parse_entry_time_local(trade: Dict) -> Optional[datetime]:
    """Parse trade entry time under the DB local-time policy."""
    try:
        entry_raw = trade.get("entry_time", "")
        if not entry_raw:
            return None
        entry_dt = pd.to_datetime(entry_raw)
        if hasattr(entry_dt, "to_pydatetime"):
            entry_dt = entry_dt.to_pydatetime()

        now_local = datetime.now()
        if entry_dt.tzinfo is not None:
            entry_dt = entry_dt.astimezone().replace(tzinfo=None)
        elif entry_dt > now_local and (entry_dt - now_local).total_seconds() <= 8.5 * 3600:
            entry_dt = entry_dt - timedelta(hours=7)
        return entry_dt
    except Exception:
        return None


def _get_position_age_seconds(trade: Dict) -> float:
    """Return how many seconds a position has been open."""
    try:
        entry_dt = _parse_entry_time_local(trade)
        if entry_dt is None:
            return 0.0
        return max(0.0, (datetime.now() - entry_dt).total_seconds())
    except Exception:
        return 0.0


class PositionMonitor:
    """
    Continuously monitors all open positions.
    Runs in its own background thread so it never blocks the scanner.
    """

    def __init__(self, db_ref=None, trade_executor=None):
        self._db            = db_ref or db
        self._executor      = trade_executor
        self.running        = False
        self.check_interval = 10   # reduced from 30s — faster SL detection
        self._thread        = None
        self._on_breakout_win_closed: Optional[Callable] = None

    def set_breakout_win_callback(
        self, cb: Callable[[str, bool, float, str], None]
    ) -> None:
        self._on_breakout_win_closed = cb

    def _get_executor(self):
        if self._executor:
            return self._executor
        from core.trade_executor import executor
        return executor

    def _get_scanner(self):
        from scanners.market_scanner import scanner
        return scanner

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("Position monitor started.")

    def stop(self):
        self.running = False
        logger.info("Position monitor stopped.")

    def _monitor_loop(self):
        while self.running:
            try:
                self.check_positions()
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
            time.sleep(self.check_interval)

    # ------------------------------------------------------------------
    #  CLOSE ATTEMPT THROTTLE
    # ------------------------------------------------------------------

    def _get_close_attempts(self, trade_id: str) -> int:
        return int(self._db.get_state(f"close_attempts_{trade_id}", default=0))

    def _get_last_attempt_time(self, trade_id: str) -> float:
        """Return timestamp of last close attempt (0 if never attempted)."""
        return float(self._db.get_state(f"close_last_attempt_{trade_id}", default=0))

    def _increment_close_attempts(self, trade_id: str, symbol: str) -> int:
        count = self._get_close_attempts(trade_id) + 1
        self._db.set_state(f"close_attempts_{trade_id}", count)
        self._db.set_state(f"close_last_attempt_{trade_id}",
                           datetime.now(timezone.utc).timestamp())
        if count >= MAX_CLOSE_ATTEMPTS:
            self._db.set_state(f"close_stuck_{trade_id}", 1)
            logger.error(
                f"[STUCK] {symbol}: {count} failed close attempts. "
                f"Manual intervention required. Use dashboard Close Now "
                f"button or Scripts/clear_stuck_trades.py to resolve."
            )
        return count

    def _reset_close_attempts(self, trade_id: str):
        self._db.set_state(f"close_attempts_{trade_id}", 0)
        self._db.set_state(f"close_stuck_{trade_id}", 0)

    def _is_stuck(self, trade_id: str) -> bool:
        return int(self._db.get_state(f"close_stuck_{trade_id}", default=0)) == 1

    def _attempt_close(self, trade: Dict, current_price: float,
                       reason: str) -> bool:
        """
        Wrapper around close_trade that tracks failed attempts.
        - Enforces MIN_CLOSE_RETRY_SEC cooldown between attempts
        - After MAX_CLOSE_ATTEMPTS failures, flags trade as STUCK
        - If broker=ibkr and close fails, automatically retries via Alpaca
        """
        trade_id = trade["trade_id"]
        symbol   = trade["symbol"]
        broker   = trade.get("broker", "")

        # ── Paper mode price clamp ─────────────────────────────────────────
        # In paper trading the monitor may catch a price AFTER it has blown
        # through the SL or overshot the TP. A real stop/limit order would
        # fill AT those levels (with only minor slippage). Clamp here so every
        # exit path gets accurate P&L — wins capped at TP, losses floored at SL.
        try:
            import config as _cfg
            asset_class = trade.get("asset_class", "")
            _is_paper = (
                (asset_class == "crypto" and getattr(_cfg, "KRAKEN_PAPER_MODE", False)) or
                (asset_class == "stock"  and getattr(_cfg, "ALPACA_PAPER_MODE", False))
            )
            if _is_paper:
                direction = trade.get("direction", "long")
                _sl = trade.get("stop_loss")
                _tp = trade.get("take_profit")
                if _sl and _tp:
                    _sl, _tp = float(_sl), float(_tp)
                    if direction == "long":
                        current_price = min(current_price, _tp)   # cap wins at TP
                        current_price = max(current_price, _sl)   # floor losses at SL
                    else:  # short: TP is lower, SL is higher
                        current_price = max(current_price, _tp)   # cap wins at TP
                        current_price = min(current_price, _sl)   # floor losses at SL
        except Exception:
            pass  # never block a close over a clamp error

        # ── Cooldown: don't hammer broker on repeated failures ─────────────
        last_attempt = self._get_last_attempt_time(trade_id)
        if last_attempt > 0:
            elapsed = datetime.now(timezone.utc).timestamp() - last_attempt
            if elapsed < MIN_CLOSE_RETRY_SEC:
                logger.debug(
                    f"[CLOSE COOLDOWN] {symbol}: last attempt {elapsed:.0f}s ago, "
                    f"waiting {MIN_CLOSE_RETRY_SEC - elapsed:.0f}s more before retry"
                )
                return False

        success = self._get_executor().close_trade(trade, current_price, reason)

        if success:
            self._reset_close_attempts(trade_id)
            if self._on_breakout_win_closed and _is_breakout_trade(trade):
                direction = trade.get("direction", "long")
                entry     = trade.get("entry_price", 0)
                won = (
                    (direction == "long"  and current_price > entry) or
                    (direction == "short" and current_price < entry)
                )
                try:
                    self._on_breakout_win_closed(symbol, won, current_price, direction)
                except Exception:
                    pass
            return True

        # ── Check if symbol simply doesn't exist on the broker ────────────
        # Non-existent pairs (e.g. CC/USD from bad scanner injection) will
        # never close successfully. Detect this early and force-close in DB
        # to stop the infinite retry loop.
        if trade.get("asset_class") == "crypto":
            try:
                import ccxt
                exchange = ccxt.kraken({"enableRateLimit": True, "timeout": 5000})
                markets  = exchange.load_markets()
                if symbol not in markets:
                    logger.error(
                        f"[INVALID PAIR] {symbol} does not exist on Kraken — "
                        f"force-closing in DB to stop retry loop. "
                        f"Remove from watchlist to prevent re-entry."
                    )
                    from data.database import db as _db_ref
                    _db_ref.close_trade(
                        trade_id    = trade_id,
                        exit_price  = current_price or trade["entry_price"],
                        exit_reason = "invalid_pair",
                        pnl         = 0.0,
                        pnl_pct     = 0.0,
                    )
                    self._reset_close_attempts(trade_id)
                    return True
            except Exception:
                pass  # If market check fails, fall through to normal retry logic

        # ── IBKR fallback: if IBKR close fails try Alpaca directly ────────
        # This handles the common case where TWS is not running but Alpaca is.
        if broker == "ibkr" and trade.get("asset_class") == "stock":
            logger.warning(
                f"[IBKR FALLBACK] {symbol}: IBKR close failed, "
                f"attempting Alpaca fallback close..."
            )
            try:
                from core.trade_executor import AlpacaExecutor
                alpaca = AlpacaExecutor()
                # close_position only takes symbol — no price or reason args
                fallback_success = alpaca.close_position(symbol)
                if fallback_success:
                    logger.info(
                        f"[IBKR FALLBACK] {symbol}: Alpaca fallback close succeeded"
                    )
                    # executor.close_trade() failed earlier so the DB trade is still
                    # open.  Record the close here to prevent a ghost trade; calling
                    # _record_close_in_db() also resets close attempts and updates the
                    # risk_manager session so daily P&L and loss counters stay correct.
                    self._record_close_in_db(trade, current_price, "ibkr_fallback_close")
                    return True
                else:
                    logger.warning(
                        f"[IBKR FALLBACK] {symbol}: Alpaca fallback also failed"
                    )
            except Exception as e:
                logger.error(f"[IBKR FALLBACK] {symbol}: fallback error: {e}")

        count = self._increment_close_attempts(trade_id, symbol)
        logger.warning(
            f"Close attempt {count}/{MAX_CLOSE_ATTEMPTS} failed for "
            f"{symbol} | reason={reason} | "
            f"next retry in {MIN_CLOSE_RETRY_SEC}s"
        )
        return False

    # ------------------------------------------------------------------
    #  DB-ONLY CLOSE HELPER
    # ------------------------------------------------------------------

    def _record_close_in_db(self, trade: Dict, exit_price: float,
                             exit_reason: str) -> None:
        """
        Record a trade close directly in the DB without submitting a broker order.

        Use when the position is already confirmed closed on the exchange
        (broker reconciliation) or when a broker fallback close succeeded outside
        the normal executor flow (IBKR → Alpaca fallback).  Going through
        executor.close_trade() in these cases would re-submit a close order to
        a broker that no longer holds the position, which would fail and
        loop into the STUCK retry logic.

        Handles: DB close, risk_manager session update, tax event.
        """
        trade_id    = trade["trade_id"]
        direction   = trade["direction"]
        entry_price = trade["entry_price"]
        qty         = trade["quantity"]
        symbol      = trade["symbol"]

        asset_class = trade.get("asset_class", "crypto")

        if direction == "long":
            pnl     = (exit_price - entry_price) * qty
            pnl_pct = ((exit_price / entry_price) - 1) * 100 if entry_price else 0.0
        else:
            pnl     = (entry_price - exit_price) * qty
            pnl_pct = ((entry_price / exit_price) - 1) * 100 if exit_price else 0.0

        # Fee simulation — matches backtester rates
        if asset_class == "crypto":
            fee_rt_pct = (config.BT_COMMISSION_CRYPTO_PCT + config.BT_SLIPPAGE_CRYPTO_PCT) / 100 * 2
        else:
            fee_rt_pct = (config.BT_COMMISSION_STOCK_PCT + config.BT_SLIPPAGE_STOCK_PCT) / 100 * 2
        fees_paid = round(entry_price * qty * fee_rt_pct, 4)
        pnl       = pnl - fees_paid

        pnl     = round(pnl, 4)
        pnl_pct = round(pnl_pct, 4)

        self._db.close_trade(
            trade_id    = trade_id,
            exit_price  = exit_price,
            exit_reason = exit_reason,
            pnl         = pnl,
            pnl_pct     = pnl_pct,
            fees_paid   = fees_paid,
        )

        try:
            full_trade = {**trade, "exit_time": datetime.now().isoformat(),
                          "exit_price": exit_price, "status": "closed",
                          "fees_paid": fees_paid}
            self._db.record_tax_event(full_trade)
        except Exception:
            pass

        risk_manager.record_trade_result(pnl=pnl, trade_won=(pnl > 0), symbol=symbol)
        self._reset_close_attempts(trade_id)
        if self._on_breakout_win_closed and _is_breakout_trade(trade):
            try:
                self._on_breakout_win_closed(
                    symbol, pnl > 0, exit_price, trade.get("direction", "long")
                )
            except Exception:
                pass
        # Clean up per-trade state keys so they don't accumulate in bot_state
        for _key in (f"sl_raise_count_{trade_id}", f"early_loss_checked_{trade_id}"):
            try:
                self._db.set_state(_key, None)
            except Exception:
                pass

        logger.info(
            f"[DB CLOSE] {symbol}: recorded close @ ${exit_price:.4f} | "
            f"reason={exit_reason} | pnl=${pnl:.2f} ({pnl_pct:+.2f}%)"
        )

    # ------------------------------------------------------------------
    #  NEW: PERFORMANCE-CONTINGENT TIME STOPS
    # ------------------------------------------------------------------

    def _check_performance_time_stop(
        self, trade: Dict, pnl_pct: float, age_seconds: float
    ) -> Optional[str]:
        """
        Kill trades that aren't performing within expected timeframes.

        Logic:
          - After 30 min: if pnl < -0.5% AND still moving wrong → early exit
          - After 2 hours: if pnl < 0.2% → going nowhere, free up capital
          - After 5 hours: if pnl < 1.0% → too slow for a day trade
          - After 8 hours: hard kill regardless of profit

        Returns exit reason string or None if trade should continue.
        """
        symbol    = trade["symbol"]
        direction = trade["direction"]
        trade_id  = trade["trade_id"]
        strategy  = trade.get("strategy_name", "")

        # ── Fee-hurdle floor (Opus audit 2026-05-29) ───────────────────
        # A "perf time stop" that closes a crypto trade for being below
        # 0.2% booked a guaranteed net loss (round-trip fee = 0.62%) AND
        # truncated winners that simply needed more time. Raise the floor
        # above the asset's fee hurdle, and on crypto only cut when the
        # trade is genuinely NEGATIVE — let slow-but-green trades run.
        _asset = trade.get("asset_class", "crypto")
        if _asset == "crypto":
            _hurdle    = getattr(config, "FEE_RT_CRYPTO_PCT", 0.62)
            _floor_2hr = 0.0                           # crypto: only cut if losing
            _floor_5hr = round(_hurdle + 0.40, 2)      # ~1.02 % net target
        else:
            _hurdle    = getattr(config, "FEE_RT_STOCK_PCT", 0.02)
            _floor_2hr = max(PERF_CHECK_2HR_MIN_PCT, round(_hurdle + 0.10, 2))
            _floor_5hr = max(PERF_CHECK_5HR_MIN_PCT, round(_hurdle + 0.30, 2))

        # ── Strategy-defined time stop exemption ──────────────────────
        # Strategies that manage their own exit timing declare this via
        # time_stop_profile = "strategy_defined" on their class.
        # The position monitor must not override their thesis with generic
        # intraday time stops.
        #
        # ECB (ecb_strategy): 24-bar hold. 2hr stop fires at bar 2 — kills thesis.
        # DCA accumulator: slow oscillation strategy; time stops inappropriate.
        # Grid bot: profits from range oscillations across many hours.
        # MR mean-reversion 1h strategies: 24/36/48-bar holds are intentional;
        # the 8hr generic stop would cut trades before the thesis plays out.
        # ── Opus live-vs-paper audit 2026-05-29 ──────────────────────────────
        # This set MUST mirror intelligence/backtester.py TIME_STOP_EXEMPT.
        # Previously live exempted only 6 strategies while the backtester
        # exempted ~30. Result: ~16 strategies were backtested WITHOUT generic
        # 30m/2h/5h/8h stops (they ran to their own thesis), but traded LIVE
        # WITH those stops firing early — so live exit-reason distributions and
        # expectancy did not match the backtest the params were tuned on. Keep
        # the two lists identical or the divergence returns.
        TIME_STOP_EXEMPT = {
            "grid_bot", "dca_accumulator", "ecb_strategy",
            "rsi_dip_spike_v4", "adaptive_regime",
            "hammer_reversal",
            "mr_02_vef", "mr_03_fbs", "mr_04_fvg",
            "map_strategy",
            "pll_cycle", "pll_cycle_martingale",
            "bollinger_squeeze",
            "scalp_master",
            "rsi_dip_simple",
            "kds_mean_reversion",
            "rcr_mean_reversion",
            "btc_v6_chandelier",
            "ema_ribbon_breakout",
            "orb_breakout",
            "vwap_confirmed_orb",
            "ema_crossover",
            "rsi_momentum",
            "bollinger_breakout",
            "vdmr_strategy",
            "vwap_momentum",
            "cbae_strategy",
            "rare_strategy",
            "fels_strategy",
            "swing_trader",
            "mean_reversion",
        }
        if strategy in TIME_STOP_EXEMPT:
            logger.debug(
                f"[TIME STOP EXEMPT] {symbol}: strategy={strategy} "
                f"manages its own exit timing — skipping performance time stops"
            )
            return None

        # ── Hard time stop: 8 hours ───────────────────────────────────
        if age_seconds >= HARD_TIME_STOP_SEC:
            logger.info(
                f"[HARD TIME STOP] {symbol}: open {age_seconds/3600:.1f}hrs — "
                f"8hr max reached | P&L {pnl_pct:+.2f}% | closing"
            )
            return "hard_time_stop"

        # ── 5-hour performance check ──────────────────────────────────
        if age_seconds >= PERF_CHECK_5HR_SEC:
            if pnl_pct < _floor_5hr:
                logger.info(
                    f"[PERF TIME STOP 5HR] {symbol}: open {age_seconds/3600:.1f}hrs | "
                    f"P&L {pnl_pct:+.2f}% < {_floor_5hr}% target — closing"
                )
                return "perf_time_stop_3hr"   # reason code unchanged for DB compatibility

        # ── 2-hour performance check ──────────────────────────────────
        if age_seconds >= PERF_CHECK_2HR_SEC:
            if pnl_pct < _floor_2hr:
                logger.info(
                    f"[PERF TIME STOP 2HR] {symbol}: open {age_seconds/3600:.1f}hrs | "
                    f"P&L {pnl_pct:+.2f}% < {_floor_2hr}% target — closing"
                )
                return "perf_time_stop_1hr"   # reason code unchanged for DB compatibility

        return None

    # ------------------------------------------------------------------
    #  NEW: EARLY LOSS IDENTIFICATION (30 MIN CHECK)
    # ------------------------------------------------------------------

    def _check_early_loss(
        self, trade: Dict, current_price: float, pnl_pct: float, age_seconds: float
    ) -> Optional[str]:
        """
        Catch trades where the thesis was wrong from the start.
        At 30 minutes: if trade is negative AND still moving against us → close.
        This limits losses on trades that immediately went the wrong way
        rather than waiting for the full stop loss to be hit.

        Uses last 3 bars to confirm the move is still going against us,
        not just a brief wick.
        """
        symbol    = trade["symbol"]
        direction = trade["direction"]
        trade_id  = trade["trade_id"]

        # Only check in the 30-60 minute window (don't re-check after)
        if age_seconds < EARLY_LOSS_CHECK_SEC:
            return None
        if age_seconds > EARLY_LOSS_CHECK_SEC * 2:  # 60 minutes max window
            return None

        # Already checked this trade at 30 min?
        already_checked = int(
            self._db.get_state(f"early_loss_checked_{trade_id}", default=0)
        )
        if already_checked:
            return None

        # Mark as checked so we don't re-evaluate
        self._db.set_state(f"early_loss_checked_{trade_id}", 1)

        if pnl_pct >= EARLY_LOSS_MIN_PCT:
            # Trade is flat or positive — no early exit needed
            logger.debug(
                f"[EARLY CHECK] {symbol}: P&L {pnl_pct:+.2f}% at 30min — OK"
            )
            return None

        # Trade is negative — check if still moving against us
        bars = self._get_bars(symbol, trade["asset_class"], limit=5)
        if bars is None or len(bars) < 3:
            logger.info(
                f"[EARLY LOSS EXIT] {symbol}: P&L {pnl_pct:+.2f}% at 30min, "
                f"no bar data to confirm — closing on loss threshold"
            )
            return "early_loss_no_data"

        closes = bars["close"].values
        still_wrong = (
            direction == "long"  and closes[-1] < closes[-2] < closes[-3]
        ) or (
            direction == "short" and closes[-1] > closes[-2] > closes[-3]
        )

        if still_wrong:
            logger.info(
                f"[EARLY LOSS EXIT] {symbol}: P&L {pnl_pct:+.2f}% at 30min "
                f"AND still moving wrong direction — thesis failed, closing"
            )
            return "early_loss"
        else:
            logger.info(
                f"[EARLY CHECK] {symbol}: P&L {pnl_pct:+.2f}% at 30min but "
                f"price stabilizing — holding, watching stop loss"
            )
            return None

    # ------------------------------------------------------------------
    #  REVERSAL CANDLE EXIT  (breakout_scanner + vwap_confirmed_orb)
    # ------------------------------------------------------------------

    def _check_reversal_candles(
        self, trade: Dict, age_seconds: float, pnl_pct: float = 0.0
    ) -> Optional[str]:
        """
        If 2+ consecutive CLOSED candles move against the trade direction
        within the first 30 minutes, the breakout has reversed — exit
        immediately rather than riding to the stop loss.

        Logic:
          Long:  last 2 closed closes are BOTH lower than their open (bearish body)
                 AND both lower than the candle before them → confirmed reversal
          Short: last 2 closed closes are BOTH higher than their open (bullish body)
                 AND both higher than the candle before them → confirmed reversal

        Only fires for breakout_scanner and vwap_confirmed_orb trades.
        Only within the first 30 minutes (after that, fast-fail / early-loss takes over).
        Only fires once per trade.
        """
        strategy = trade.get("strategy_name", "")
        if strategy not in ("breakout_scanner", "vwap_confirmed_orb"):
            return None

        # Opus diff 08: only fire when trade is losing — don't cut profitable breakouts early
        if pnl_pct > 0:
            return None

        # Only in first 30 minutes
        if age_seconds > 30 * 60:
            return None

        # Need at least 2 closed bars after entry — wait at least 2 bars (10 min on 5m)
        if age_seconds < 10 * 60:
            return None

        trade_id  = trade["trade_id"]
        symbol    = trade["symbol"]
        direction = trade["direction"]

        # Only fire once per trade
        already = int(self._db.get_state(f"reversal_check_{trade_id}", default=0))
        if already:
            return None

        bars = self._get_bars(symbol, trade["asset_class"], limit=6)
        if bars is None or len(bars) < 3:
            return None

        self._db.set_state(f"reversal_check_{trade_id}", 1)

        # Last 2 fully closed candles (bars are closed-only from _get_bars)
        c1 = bars.iloc[-2]   # older of the two
        c2 = bars.iloc[-1]   # most recent closed

        if direction == "long":
            # Both candles bearish body AND c2 closed lower than c1
            c1_bearish = float(c1["close"]) < float(c1["open"])
            c2_bearish = float(c2["close"]) < float(c2["open"])
            c2_lower   = float(c2["close"]) < float(c1["close"])
            if c1_bearish and c2_bearish and c2_lower:
                logger.info(
                    f"[REVERSAL EXIT] {symbol}: 2 consecutive bearish candles after long entry "
                    f"— c1 close={c1['close']:.4f} c2 close={c2['close']:.4f} — exiting"
                )
                return "reversal_candles"
        else:  # short
            c1_bullish = float(c1["close"]) > float(c1["open"])
            c2_bullish = float(c2["close"]) > float(c2["open"])
            c2_higher  = float(c2["close"]) > float(c1["close"])
            if c1_bullish and c2_bullish and c2_higher:
                logger.info(
                    f"[REVERSAL EXIT] {symbol}: 2 consecutive bullish candles after short entry "
                    f"— c1 close={c1['close']:.4f} c2 close={c2['close']:.4f} — exiting"
                )
                return "reversal_candles"

        logger.debug(
            f"[REVERSAL EXIT] {symbol}: no reversal pattern — holding "
            f"(c1 {'bear' if float(c1['close']) < float(c1['open']) else 'bull'} "
            f"c2 {'bear' if float(c2['close']) < float(c2['open']) else 'bull'})"
        )
        return None

    # ------------------------------------------------------------------
    #  BREAKOUT FAST-FAIL (10-20 min window, breakout_scanner only)
    # ------------------------------------------------------------------

    def _check_breakout_fast_fail(
        self, trade: Dict, current_price: float, pnl_pct: float, age_seconds: float
    ) -> Optional[str]:
        """
        Breakout-specific fast-fail: real breakouts move immediately.
        If price is flat or negative at 10 minutes the thesis failed — exit
        with a tiny loss rather than riding to the full 2-4.5% stop.

        Window: 10-20 minutes after entry (2-4 bars on 5m).
        After 20 min the regular early_loss check takes over.

        Only fires for breakout_scanner trades.
        """
        if not _is_breakout_trade(trade):
            return None

        # TEMPORARILY DISABLED — 2026-05-29: cutting winners too early on dips
        # that recover. Chart shows price never near stop loss, would have hit TP.
        # Re-evaluate after observing trade outcomes without fast-fail.
        return None

        # Only in the 10-20 minute window
        if age_seconds < BREAKOUT_FAST_FAIL_SEC:
            return None
        if age_seconds > BREAKOUT_FAST_FAIL_MAX_SEC:
            return None

        trade_id = trade["trade_id"]
        symbol   = trade["symbol"]

        # Only fire once per trade
        already_checked = int(
            self._db.get_state(f"breakout_fast_fail_checked_{trade_id}", default=0)
        )
        if already_checked:
            return None
        self._db.set_state(f"breakout_fast_fail_checked_{trade_id}", 1)

        # If up by threshold — thesis working, let it run
        if pnl_pct >= BREAKOUT_FAST_FAIL_MIN_PCT:
            logger.info(
                f"[BREAKOUT FAST-FAIL] {symbol}: P&L {pnl_pct:+.2f}% at 10min "
                f">= {BREAKOUT_FAST_FAIL_MIN_PCT}% — breakout confirmed, holding"
            )
            return None

        # Flat or negative — thesis failed immediately, cut it clean
        logger.info(
            f"[BREAKOUT FAST-FAIL] {symbol}: P&L {pnl_pct:+.2f}% at 10min "
            f"< {BREAKOUT_FAST_FAIL_MIN_PCT}% threshold — breakout failed, "
            f"cutting before full stop | price=${current_price:.4f}"
        )
        return "breakout_fast_fail"

    # ------------------------------------------------------------------
    #  NEW: PIVOT POINT BREAK EXIT
    # ------------------------------------------------------------------

    def _check_pivot_break(
        self, trade: Dict, current_price: float
    ) -> Optional[str]:
        """
        Classic floor trader pivot points calculated from recent bars.
        PP  = (High + Low + Close) / 3
        S1  = 2*PP - High   (first support)
        S2  = PP - (High - Low)  (second support)
        R1  = 2*PP - Low    (first resistance)
        R2  = PP + (High - Low)  (second resistance)

        Exit logic:
          Long:  if price breaks below S1 → structural support failed → close
          Short: if price breaks above R1 → structural resistance failed → close

        Only fires after 30 minutes (gives trade time to breathe past entry noise).
        """
        symbol    = trade["symbol"]
        direction = trade["direction"]
        trade_id  = trade["trade_id"]

        bars = self._get_bars(symbol, trade["asset_class"], limit=PIVOT_BARS)
        if bars is None or len(bars) < 5:
            return None

        # Calculate pivots from the lookback window (excluding current bar)
        h   = float(bars["high"].iloc[:-1].max())
        l   = float(bars["low"].iloc[:-1].min())
        c   = float(bars["close"].iloc[-2])  # last closed bar

        pp  = (h + l + c) / 3
        s1  = 2 * pp - h
        s2  = pp - (h - l)
        r1  = 2 * pp - l
        r2  = pp + (h - l)

        logger.debug(
            f"[PIVOT] {symbol}: PP={pp:.4f} S1={s1:.4f} S2={s2:.4f} "
            f"R1={r1:.4f} R2={r2:.4f} | current={current_price:.4f}"
        )

        if direction == "long" and current_price < s1:
            logger.info(
                f"[PIVOT EXIT] {symbol}: LONG broke below S1={s1:.4f} "
                f"(current={current_price:.4f}) — structural support failed, closing"
            )
            return "pivot_break_s1"

        if direction == "short" and current_price > r1:
            logger.info(
                f"[PIVOT EXIT] {symbol}: SHORT broke above R1={r1:.4f} "
                f"(current={current_price:.4f}) — structural resistance failed, closing"
            )
            return "pivot_break_r1"

        return None

    # ------------------------------------------------------------------
    #  ADX / DI HELPERS FOR STALE POSITION CHECK
    # ------------------------------------------------------------------

    def _get_adx_di(self, symbol: str, asset_class: str) -> Optional[Dict]:
        try:
            import pandas_ta as ta
            bars = self._get_bars(symbol, asset_class, limit=50)
            if bars is None or len(bars) < 20:
                return None

            adx_df  = ta.adx(bars["high"], bars["low"], bars["close"], length=14)
            if adx_df is None or adx_df.empty:
                return None

            adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
            dmp_col = [c for c in adx_df.columns if c.startswith("DMP_")]
            dmn_col = [c for c in adx_df.columns if c.startswith("DMN_")]

            if not adx_col or not dmp_col or not dmn_col:
                return None

            return {
                "adx":      float(adx_df[adx_col[0]].iloc[-1]),
                "plus_di":  float(adx_df[dmp_col[0]].iloc[-1]),
                "minus_di": float(adx_df[dmn_col[0]].iloc[-1]),
            }
        except Exception as e:
            logger.debug(f"ADX/DI fetch error for {symbol}: {e}")
            return None

    def _check_stale_position(self, trade: Dict) -> Optional[str]:
        """
        Stale position sell-off logic:
          If ADX < 25 (trend stalled) → sell immediately
          Else:
            Long  + bearish (-DI > +DI) → sell
            Long  + bullish (+DI > -DI) → hold
            Short + bullish (+DI > -DI) → sell
            Short + bearish (-DI > +DI) → hold
        """
        symbol    = trade["symbol"]
        direction = trade["direction"]

        adx_data = self._get_adx_di(symbol, trade["asset_class"])
        if adx_data is None:
            logger.debug(f"[STALE] {symbol}: could not get ADX data — skipping")
            return None

        adx      = adx_data["adx"]
        plus_di  = adx_data["plus_di"]
        minus_di = adx_data["minus_di"]

        logger.info(
            f"[STALE CHECK] {symbol} {direction.upper()} | "
            f"Open 3+ hrs | ADX={adx:.1f} +DI={plus_di:.1f} -DI={minus_di:.1f}"
        )

        if adx < ADX_STALE_THRESHOLD:
            logger.info(
                f"[STALE EXIT] {symbol}: ADX={adx:.1f} < {ADX_STALE_THRESHOLD} "
                f"(trend stalled) — closing"
            )
            return "stale_no_trend"

        if direction == "long":
            if minus_di > plus_di:
                logger.info(
                    f"[STALE EXIT] {symbol}: LONG but -DI({minus_di:.1f}) > "
                    f"+DI({plus_di:.1f}) (bearish) — closing"
                )
                return "stale_trend_reversed"
            else:
                logger.info(
                    f"[STALE HOLD] {symbol}: LONG and +DI({plus_di:.1f}) > "
                    f"-DI({minus_di:.1f}) (bullish) — holding"
                )
                return None
        else:
            if plus_di > minus_di:
                logger.info(
                    f"[STALE EXIT] {symbol}: SHORT but +DI({plus_di:.1f}) > "
                    f"-DI({minus_di:.1f}) (bullish) — closing"
                )
                return "stale_trend_reversed"
            else:
                logger.info(
                    f"[STALE HOLD] {symbol}: SHORT and -DI({minus_di:.1f}) > "
                    f"+DI({plus_di:.1f}) (bearish) — holding"
                )
                return None

    # ------------------------------------------------------------------
    #  RSI DIP & SPIKE V4 EXIT (strategy-specific)
    # ------------------------------------------------------------------

    def _check_rsi_dip_spike_exit(
        self, trade: Dict, current_price: float, age_seconds: float
    ) -> Optional[str]:
        """
        Delegate to RSIDipSpikeV4Strategy.check_custom_exit() for live parity.

        Provides two exits that only the strategy knows about:
          - RSI neutral exit : RSI recovers past the exit threshold (55/45)
          - 48-bar max-hold  : time limit enforced by the strategy thesis

        Without this, the generic 8-hour hard stop fires first, producing
        different exit-reason distributions live vs backtested.

        _bars_held is derived from age_seconds using the 1h bar size (3600s).
        """
        import json

        symbol      = trade["symbol"]
        asset_class = trade.get("asset_class", "crypto")
        direction   = trade["direction"]

        try:
            meta = json.loads(trade.get("indicators_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}

        # Derive bars_held from wall-clock age — rsi_dip_spike_v4 is 1h only
        meta["_bars_held"] = int(age_seconds / 3600)

        bars = self._get_bars(symbol, asset_class, limit=60, timeframe="1h")
        if bars is None or len(bars) < 15:
            logger.debug(
                f"[RSI DIP EXIT] {symbol}: insufficient bars "
                f"(got {len(bars) if bars is not None else 0}, need 15+) — skip"
            )
            return None

        try:
            from strategies.rsi_dip_spike_v4 import RSIDipSpikeV4Strategy
            return RSIDipSpikeV4Strategy().check_custom_exit(
                symbol, bars, direction, meta
            )
        except Exception as e:
            logger.debug(f"[RSI DIP EXIT] {symbol}: check_custom_exit error: {e}")
            return None

    # ------------------------------------------------------------------
    #  ADAPTIVE REGIME EXIT (strategy-specific)
    # ------------------------------------------------------------------

    def _check_adaptive_regime_exit(
        self, trade: Dict, current_price: float
    ) -> Optional[str]:
        """
        Delegate to AdaptiveRegime.check_custom_exit() — the same code path
        used by the backtester — so live and backtest apply identical exit logic.

        Previously this method duplicated the EMA/BB/RSI exit rules inline.
        The canonical implementation lives in strategies/adaptive_regime.py;
        maintaining two copies causes live/backtest drift whenever one is
        updated without the other. Now there is exactly one source of truth.

        entry_timeframe and indicators_json (persisted at trade open time) are
        both confirmed present in the trades table — see database.py lines 99
        and 93 respectively.
        """
        import json

        symbol          = trade["symbol"]
        asset_class     = trade["asset_class"]
        direction       = trade["direction"]
        entry_timeframe = trade.get("entry_timeframe") or (
            "1Hour" if asset_class == "stock" else "1h"
        )

        # ── Parse entry metadata — fast-path skip if adaptive_exit_mode absent
        try:
            entry_metadata = json.loads(trade.get("indicators_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            entry_metadata = {}

        if not entry_metadata.get("adaptive_exit_mode"):
            # Not an adaptive_regime trade or missing metadata — skip quietly
            return None

        # ── Fetch closed bars on the entry timeframe ───────────────────────
        # 150 bars needed for EMA100 warm-up.  _get_bars() enforces closed-only
        # via _strip_forming_bar() so the decision is never based on an intrabar
        # wick (same guarantee the backtester gets from yfinance history).
        bars = self._get_bars(symbol, asset_class, limit=150, timeframe=entry_timeframe)
        if bars is None or len(bars) < 30:
            logger.debug(
                f"[ADAPTIVE EXIT] {symbol}: insufficient bars "
                f"(got {len(bars) if bars is not None else 0}, need 30+) — skip"
            )
            return None

        try:
            from strategies.adaptive_regime import AdaptiveRegime
            return AdaptiveRegime().check_custom_exit(
                symbol, bars, direction, entry_metadata
            )
        except Exception as e:
            logger.debug(f"[ADAPTIVE EXIT] {symbol}: check_custom_exit error: {e}")
            return None

    # ------------------------------------------------------------------
    #  GENERIC STRATEGY CUSTOM EXIT  (Opus live-vs-paper audit 2026-05-29)
    # ------------------------------------------------------------------
    def _check_generic_custom_exit(
        self, trade: Dict, age_seconds: float
    ) -> Optional[str]:
        """
        Call the trade's strategy object's check_custom_exit() — the SAME method
        the backtester calls on every bar. This gives live/backtest parity for
        every strategy that defines a custom exit, instead of hardcoding each one.

        Returns an exit-reason string to close, or None to keep holding.
        Fails safe (returns None) on any error so a strategy bug can never wedge
        the monitor — the protective structural stop is still the hard backstop.
        """
        import json

        symbol      = trade["symbol"]
        asset_class = trade.get("asset_class", "crypto")
        direction   = trade["direction"]
        strat_name  = trade.get("strategy_name", "")
        if not strat_name:
            return None

        try:
            from strategies.strategy_engine import strategy_engine
            from strategies.base_strategy import BaseStrategy
            strat = strategy_engine._get_strategy(strat_name)
        except Exception as e:
            logger.debug(f"[CUSTOM EXIT] {symbol}: engine lookup failed: {e}")
            return None
        if strat is None:
            return None

        # Only strategies that actually override the hook are worth fetching bars
        # for; the BaseStrategy stub returns None.
        if type(strat).check_custom_exit is BaseStrategy.check_custom_exit:
            return None

        # Pick the timeframe and bar size the strategy declares for this asset.
        if asset_class == "stock":
            tf = getattr(strat, "stock_candle_timeframe", "5Min")
            bar_sec = {"1Min": 60, "5Min": 300, "15Min": 900,
                       "1Hour": 3600, "1Day": 86400}.get(tf, 300)
        else:
            tf = getattr(strat, "crypto_candle_timeframe", "1h")
            bar_sec = {"1m": 60, "5m": 300, "15m": 900,
                       "1h": 3600, "1d": 86400}.get(tf, 3600)

        try:
            meta = json.loads(trade.get("indicators_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        meta["_bars_held"]   = int(age_seconds / bar_sec) if bar_sec else 0
        meta["_entry_price"] = trade.get("entry_price", 0)

        bars = self._get_bars(symbol, asset_class, limit=150, timeframe=tf)
        if bars is None or len(bars) < 30:
            return None

        try:
            return strat.check_custom_exit(symbol, bars, direction, meta)
        except Exception as e:
            logger.debug(f"[CUSTOM EXIT] {symbol}: {strat_name} check error: {e}")
            return None

    def _check_vwap_cross_exit(self, trade: Dict, current_price: float) -> bool:
        """VWAP cross exit for ORB trades."""
        try:
            bars = self._get_bars(trade["symbol"], trade["asset_class"], limit=50)
            if bars is None or len(bars) < 5:
                return False

            typical = (bars["high"] + bars["low"] + bars["close"]) / 3
            cum_tpv = (typical * bars["volume"]).cumsum()
            cum_vol = bars["volume"].cumsum()
            vwap    = (cum_tpv / cum_vol.replace(0, float("nan"))).iloc[-1]

            if pd.isna(vwap):
                return False

            direction = trade["direction"]
            if direction == "long" and current_price < vwap:
                logger.info(
                    f"[ORB VWAP EXIT] {trade['symbol']}: price {current_price:.4f} "
                    f"crossed below VWAP {vwap:.4f} — closing"
                )
                return True
            elif direction == "short" and current_price > vwap:
                logger.info(
                    f"[ORB VWAP EXIT] {trade['symbol']}: price {current_price:.4f} "
                    f"crossed above VWAP {vwap:.4f} — closing"
                )
                return True

            return False
        except Exception as e:
            logger.debug(f"VWAP cross check error for {trade['symbol']}: {e}")
            return False

    # ------------------------------------------------------------------
    #  MOMENTUM RIDER HELPERS
    # ------------------------------------------------------------------

    def _get_bars(self, symbol: str, asset_class: str,
                  limit: int = 25,
                  timeframe: str = None) -> Optional[pd.DataFrame]:
        """
        Fetch CLOSED bars for a symbol.

        Closed-bar guarantee: both underlying scanner methods (StockScanner.get_bars
        and CryptoScanner.get_ohlcv) call _strip_forming_bar() before returning.
        That function drops the last row when now_utc < bar_open + timeframe_duration,
        so all callers of _get_bars() receive only fully closed candles.  Exit logic
        based on EMA crosses, BB midlines, ADX/DI, and two-bar trailing stops is
        therefore never contaminated by a still-forming intrabar wick.

        `timeframe` controls which candle size is fetched:
          - Pass the trade's entry_timeframe for structural exit logic (trailing stop,
            adaptive regime exits).  A 1h-entry trade must trail on 1h bars.
          - Omit (or pass None) for monitoring overlay checks (early loss, pivot,
            ADX stale, VWAP, momentum) — those default to 5Min/5m regardless of
            entry timeframe, which is the intentional hybrid design documented in
            TIMEFRAME_LIFECYCLE_SPEC.md.

        THREADING NOTE: the scanner's ccxt exchange object is NOT thread-safe.
        The position monitor runs in its own daemon thread while the scanner loop
        runs in the main thread.  Concurrent fetch_ohlcv calls on the same ccxt
        object cause rate-limiter lock deadlocks that silently hang this thread
        (observed: monitor goes silent at 13:27, all SLs stop firing).
        Fix: use a private ccxt exchange instance that belongs only to the monitor
        thread, with an aggressive timeout so a stale connection can't hang forever.
        """
        try:
            # CandleManager cache first — eliminates ccxt thread-safety issue entirely.
            # The refresh loop pre-warms all watchlist symbols so this is usually
            # an instant SQLite read with no network call.
            from core.candle_manager import candle_manager
            tf = timeframe or ("5Min" if asset_class == "stock" else "5m")
            df = candle_manager.get(symbol, tf, limit=limit)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass

        # Fallback: direct fetch (candle_manager cold start or symbol not in watchlist)
        try:
            if asset_class == "stock":
                tf = timeframe or "5Min"
                sc = self._get_scanner()
                return sc.stock_scanner.get_bars(symbol, timeframe=tf, limit=limit)
            else:
                tf = timeframe or "5m"
                ex = self._get_monitor_exchange()
                ohlcv = ex.fetch_ohlcv(symbol, tf, limit=limit + 1)
                if not ohlcv:
                    return None
                df = pd.DataFrame(
                    ohlcv,
                    columns=["timestamp", "open", "high", "low", "close", "volume"]
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                df.set_index("timestamp", inplace=True)
                if len(df) > 1:
                    df = df.iloc[:-1]
                return df
        except Exception as e:
            logger.debug(f"Bar fetch error for {symbol}: {e}")
            return None

    def _get_monitor_exchange(self):
        """
        Returns a dedicated ccxt Kraken exchange for this monitor thread.
        Lazily created on first call — NOT shared with the scanner.

        Short timeout (8s) so a hung TCP connection times out within one
        monitor cycle (10s interval) rather than blocking forever.
        """
        if not hasattr(self, "_monitor_exchange") or self._monitor_exchange is None:
            import ccxt as _ccxt
            self._monitor_exchange = _ccxt.kraken({
                "apiKey":          config.KRAKEN_API_KEY,
                "secret":          config.KRAKEN_SECRET_KEY,
                "enableRateLimit": True,
                "timeout":         8000,   # 8s — dies inside one 10s monitor cycle
            })
            logger.info("[MONITOR] Private ccxt exchange created for monitor thread")
        return self._monitor_exchange

    def _score_momentum(self, symbol: str, asset_class: str) -> float:
        try:
            bars = self._get_bars(symbol, asset_class, limit=25)
            if bars is None or len(bars) < 15:
                return 0.0

            closes  = bars["close"]
            volumes = bars["volume"]
            hits    = 0

            delta    = closes.diff()
            gain     = delta.clip(lower=0)
            loss     = -delta.clip(upper=0)
            avg_gain = gain.ewm(com=13, adjust=False).mean()
            avg_loss = loss.ewm(com=13, adjust=False).mean()
            rs       = avg_gain / avg_loss.replace(0, np.nan)
            rsi      = 100 - (100 / (1 + rs))
            rsi_now  = rsi.iloc[-1]
            rsi_prev = rsi.iloc[-4]
            if rsi_now >= config.MOMENTUM_RIDER_RSI_MIN and rsi_now > rsi_prev:
                hits += 1

            c = closes.iloc[-3:].values
            if c[1] > c[0] and c[2] > c[1]:
                hits += 1

            vol_avg = volumes.iloc[-21:-1].mean()
            vol_now = volumes.iloc[-1]
            if vol_avg > 0 and vol_now >= vol_avg * config.MOMENTUM_RIDER_VOL_RATIO:
                hits += 1

            return hits / 3

        except Exception as e:
            logger.debug(f"Momentum score error for {symbol}: {e}")
            return 0.0

    # ------------------------------------------------------------------
    #  MAIN POSITION PROCESSOR
    # ------------------------------------------------------------------

    def _process_position(self, trade: Dict):
        """
        Process a single open position.

        Exit priority order:
          1.  STUCK check
          2.  EOD close (15:45 ET)
          3.  Stop-loss
          4.  ORB VWAP cross exit
          5.  Early loss identification (30 min)
          6.  Performance-contingent time stop (2hr)
          7.  Performance-contingent time stop (5hr)
          8.  Hard time stop (8hr)
          9.  Take-profit + momentum rider
          10. Pivot point break
          11. Stale position ADX/DI (3hr)
          12. Trailing stop update + logging
        """
        symbol           = trade["symbol"]
        asset_class      = trade["asset_class"]
        direction        = trade["direction"]
        trade_id         = trade["trade_id"]
        strategy         = trade.get("strategy_name", "")
        is_overnight     = bool(trade.get("is_overnight", False))
        entry_timeframe  = trade.get("entry_timeframe") or (
            "5Min" if asset_class == "stock" else "5m"
        )

        # ── 1. STUCK CHECK ───────────────────────────────────────────────
        if self._is_stuck(trade_id):
            logger.warning(
                f"[STUCK] {symbol}: skipping — exceeded {MAX_CLOSE_ATTEMPTS} "
                f"close attempts. Use dashboard Close Now to resolve."
            )
            return

        current_price = self._get_scanner().get_current_price(symbol, asset_class)
        if current_price is None:
            logger.warning(f"Could not get price for {symbol} — skipping check")
            return

        # ── Price sanity gate ─────────────────────────────────────────────────
        # Reject prices that are wildly wrong relative to the trade entry.
        # A bad price feed (e.g. Kraken returning a stale listing price for a
        # new token) can look like a -99% stop-loss and force-close a live trade.
        # 80% is conservative: real stops/moves are at most a few percent.
        _entry = trade.get("entry_price", 0)
        if _entry > 0 and current_price > 0:
            _deviation = abs(current_price - _entry) / _entry
            if _deviation > 0.80:
                logger.warning(
                    f"[PRICE SANITY] {symbol}: live=${current_price:.6f} is "
                    f"{_deviation*100:.1f}% from entry=${_entry:.4f} — "
                    f"likely bad price feed, skipping this cycle"
                )
                return

        qty = trade["quantity"]
        if direction == "long":
            pnl_pct = (current_price - trade["entry_price"]) / trade["entry_price"] * 100
        else:
            pnl_pct = (trade["entry_price"] - current_price) / trade["entry_price"] * 100

        age_seconds = _get_position_age_seconds(trade)
        age_hrs     = age_seconds / 3600

        logger.debug(
            f"{symbol} [{entry_timeframe}]: price=${current_price:.4f} | "
            f"SL=${trade['stop_loss']:.4f} | TP=${trade['take_profit']:.4f} | "
            f"P&L: {pnl_pct:+.2f}% | Age: {age_hrs:.1f}hr"
        )

        # ── 2. EOD CLOSE (15:45 ET) ──────────────────────────────────────
        if asset_class == "stock" and not is_overnight and _approaching_eod():
            logger.info(
                f"[EOD CLOSE] {symbol}: closing before market close @ 15:45 ET"
            )
            self._db.set_state(f"momentum_ext_{trade_id}", 0)
            self._attempt_close(trade, current_price, "eod_close")
            return

        # ── 2b. BROKER RECONCILIATION (live mode only) ───────────────────
        # Check if the position was already closed by the exchange (SL/TP order).
        # This handles the case where Kraken fired an SL order automatically
        # and we need to record the close in our DB without trying to close again.
        if asset_class == "crypto" and not getattr(
                __import__("config"), "KRAKEN_PAPER_MODE", True):
            try:
                kraken_ex = self._get_executor().kraken
                pos = kraken_ex.get_open_position(symbol)
                if pos is None:
                    # Position gone from exchange — find the fill price
                    import time as _t
                    entry_ts = None
                    try:
                        entry_dt = _parse_entry_time_local(trade)
                        entry_ts = entry_dt.timestamp() if entry_dt else None
                    except Exception:
                        pass

                    closed_order = kraken_ex.get_recent_closed_order(
                        symbol, since_timestamp=entry_ts
                    )
                    fill_price = (
                        closed_order["fill_price"]
                        if closed_order and closed_order["fill_price"] > 0
                        else current_price
                    )
                    fill_source = "exchange order" if closed_order else "current price"
                    logger.info(
                        f"[BROKER RECONCILE] {symbol}: position no longer on Kraken — "
                        f"recording close @ ${fill_price:.4f} (via {fill_source})"
                    )
                    self._db.set_state(f"momentum_ext_{trade_id}", 0)
                    # Use _record_close_in_db, NOT _attempt_close.
                    # The position is already gone from Kraken — submitting another
                    # close order via executor.close_trade() would fail and loop
                    # into the STUCK retry.  Record directly in DB instead.
                    self._record_close_in_db(trade, fill_price, "broker_closed_sl_tp")
                    return
            except Exception as _rec_err:
                logger.debug(f"Reconciliation check error for {symbol}: {_rec_err}")

        # ── 3. STOP-LOSS ──────────────────────────────────────────────────
        sl_exit = risk_manager.check_sl_exit(trade, current_price)
        if sl_exit:
            # Fill at stop_loss price, not current candle price.
            # Live brokers execute the stop order at stop price; paper must match.
            # current_price is logged for context but is not used as the fill.
            sl_fill_price = float(trade.get("stop_loss") or current_price)
            logger.info(
                f"[STOP LOSS] {symbol} @ ${current_price:.6f} | "
                f"fill @ ${sl_fill_price:.6f} | "
                f"P&L {pnl_pct:+.2f}% | age {age_hrs:.1f}hr"
            )
            self._db.set_state(f"momentum_ext_{trade_id}", 0)
            self._attempt_close(trade, sl_fill_price, sl_exit)
            return

        # ── 4. ORB VWAP CROSS EXIT ──────────────────────────────────────
        if strategy == "orb_breakout":
            if self._check_vwap_cross_exit(trade, current_price):
                self._db.set_state(f"momentum_ext_{trade_id}", 0)
                self._attempt_close(trade, current_price, "vwap_cross")
                return

        # ── 4b. ADAPTIVE REGIME EXIT ─────────────────────────────────────
        # Strategy-specific exit: EMA cross (trend) or BB midline / RSI (mean rev).
        # Checked before early loss so the strategy's own exit model takes priority
        # over generic time-based rules. Only fires for adaptive_regime trades.
        if strategy == "adaptive_regime":
            adaptive_exit = self._check_adaptive_regime_exit(trade, current_price)
            if adaptive_exit:
                self._db.set_state(f"momentum_ext_{trade_id}", 0)
                self._attempt_close(trade, current_price, adaptive_exit)
                return

        # ── 4c. RSI DIP & SPIKE EXIT ─────────────────────────────────────
        # Strategy-specific exit: RSI neutral exit + 48-bar max-hold.
        # Takes priority over generic time stops — rsi_dip_spike_v4 is
        # TIME_STOP_EXEMPT in the backtester; this block gives live parity.
        if strategy == "rsi_dip_spike_v4":
            rsi_dip_exit = self._check_rsi_dip_spike_exit(trade, current_price, age_seconds)
            if rsi_dip_exit:
                self._db.set_state(f"momentum_ext_{trade_id}", 0)
                self._attempt_close(trade, current_price, rsi_dip_exit)
                return

        # ── 4c-2. GENERIC STRATEGY CUSTOM EXIT (live/backtest parity) ────────
        # Opus live-vs-paper audit 2026-05-29. Every strategy overriding
        # BaseStrategy.check_custom_exit() has its exit applied by the backtester
        # on every bar. adaptive_regime and rsi_dip_spike_v4 are handled above;
        # this dispatch covers ALL remaining strategies (btc_v6_chandelier,
        # ema_ribbon_breakout, kds/rcr_mean_reversion, mr_02/03/04, ecb_strategy,
        # swing_trader, bollinger_squeeze, pll_cycle, etc.) so live exits match
        # the thesis the params were tuned on. Pairs with diff 09.
        if strategy not in ("adaptive_regime", "rsi_dip_spike_v4", "orb_breakout"):
            custom_exit = self._check_generic_custom_exit(trade, age_seconds)
            if custom_exit:
                self._db.set_state(f"momentum_ext_{trade_id}", 0)
                self._attempt_close(trade, current_price, custom_exit)
                return

        # ── 4d. REVERSAL CANDLE EXIT (10-30 min, breakout + vwap_orb) ───────
        # 2 consecutive candles closing against the trade = reversal confirmed.
        # Fires before fast-fail — gets out even earlier on clean reversals.
        reversal_exit = self._check_reversal_candles(trade, age_seconds, pnl_pct)
        if reversal_exit:
            self._db.set_state(f"momentum_ext_{trade_id}", 0)
            self._attempt_close(trade, current_price, reversal_exit)
            return

        # ── 4e. BREAKOUT FAST-FAIL (10-20 min) ──────────────────────────────
        # Breakout trades must move immediately — if flat/negative at 10 min
        # the setup failed. Cut clean for tiny loss rather than riding to
        # the full 2-4.5% stop. Fires before early_loss (30 min) on purpose.
        fast_fail_exit = self._check_breakout_fast_fail(
            trade, current_price, pnl_pct, age_seconds
        )
        if fast_fail_exit:
            self._db.set_state(f"momentum_ext_{trade_id}", 0)
            self._attempt_close(trade, current_price, fast_fail_exit)
            return

        # ── 5. EARLY LOSS IDENTIFICATION (30 min) ────────────────────────
        # Skip for ORB — it needs time to develop past the opening range
        if strategy != "orb_breakout":
            early_exit = self._check_early_loss(
                trade, current_price, pnl_pct, age_seconds
            )
            if early_exit:
                self._db.set_state(f"momentum_ext_{trade_id}", 0)
                self._attempt_close(trade, current_price, early_exit)
                return

        # ── 6-8. PERFORMANCE-CONTINGENT AND HARD TIME STOPS ──────────────
        # Skip for overnight positions — they're explicitly held longer
        if not is_overnight:
            time_exit = self._check_performance_time_stop(
                trade, pnl_pct, age_seconds
            )
            if time_exit:
                self._db.set_state(f"momentum_ext_{trade_id}", 0)
                self._attempt_close(trade, current_price, time_exit)
                return

        # ── 9. TAKE-PROFIT + MOMENTUM RIDER (non-ORB) ────────────────────
        if strategy != "orb_breakout":
            at_tp = (
                (direction == "long"  and current_price >= trade["take_profit"]) or
                (direction == "short" and current_price <= trade["take_profit"])
            )

            if at_tp:
                rider_enabled  = getattr(config, "MOMENTUM_RIDER_ENABLED", True)
                max_extensions = getattr(config, "MOMENTUM_RIDER_MAX_EXTENSIONS", 3)
                min_score      = getattr(config, "MOMENTUM_RIDER_MIN_SCORE", 0.67)
                ext_count      = int(self._db.get_state(
                    f"momentum_ext_{trade_id}", default=0
                ))

                ride = False
                if rider_enabled and ext_count < max_extensions:
                    mom_score = self._score_momentum(symbol, asset_class)
                    ride = mom_score >= min_score

                if ride:
                    import config as _cfg
                    tp_pct = _cfg.DEFAULT_TAKE_PROFIT_PCT / 100
                    new_tp = (
                        current_price * (1 + tp_pct)
                        if direction == "long"
                        else current_price * (1 - tp_pct)
                    )
                    new_tp = round(new_tp, 4)

                    if asset_class == "crypto" and not _cfg.KRAKEN_PAPER_MODE:
                        # ── LIVE MODE: close + reopen ─────────────────────
                        # Can't modify TP on a real exchange. Close the position
                        # at current price (booking the TP profit), then immediately
                        # re-enter with a fresh SL/TP if momentum still holds.
                        logger.info(
                            f"[LIVE TP RIDE] {symbol}: closing at TP ${current_price:.4f} "
                            f"and re-entering for momentum extension "
                            f"(ext {ext_count + 1}/{max_extensions})"
                        )
                        # Cancel existing stop order before closing
                        old_stop_id = trade.get("stop_order_id", "")
                        if old_stop_id:
                            self._get_executor().kraken.cancel_order(symbol, old_stop_id)
                        # Close the position (books the profit)
                        self._attempt_close(trade, current_price, "take_profit_ride")
                        # Re-enter immediately via scanner signal injection
                        # The scanner will pick it up on next cycle if momentum holds.
                        # We mark this so the cooldown knows it was a win-ride, not revenge.
                        try:
                            from core.risk_manager import risk_manager as _rm
                            _rm._last_closed[symbol] = {
                                "time": __import__("time").time() - 90,  # 90s ago = inside win cooldown
                                "won":  True,
                            }
                        except Exception:
                            pass
                        return
                    else:
                        # ── PAPER MODE: extend TP in DB (no real orders) ──
                        entry_price = float(trade.get("entry_price") or 0)
                        old_sl = float(trade.get("stop_loss") or 0)
                        lock_ratio = float(getattr(
                            _cfg, "MOMENTUM_RIDER_LOCK_PROFIT_RATIO", 0.50
                        ))
                        min_lock = float(getattr(
                            _cfg, "MOMENTUM_RIDER_MIN_LOCK_PCT", 0.25
                        ))
                        lock_pct = max(min_lock, pnl_pct * lock_ratio)
                        new_sl = old_sl

                        if entry_price > 0 and pnl_pct > 0:
                            if direction == "long":
                                candidate_sl = entry_price * (1 + lock_pct / 100)
                                candidate_sl = min(candidate_sl, current_price * 0.995)
                                new_sl = max(old_sl, candidate_sl) if old_sl else candidate_sl
                            else:
                                candidate_sl = entry_price * (1 - lock_pct / 100)
                                candidate_sl = max(candidate_sl, current_price * 1.005)
                                new_sl = min(old_sl, candidate_sl) if old_sl else candidate_sl
                            new_sl = round(new_sl, 6)

                        self._db.update_trade_levels(
                            trade_id, stop_loss=new_sl, take_profit=new_tp
                        )
                        self._db.set_state(f"momentum_ext_{trade_id}", ext_count + 1)
                        self._db.increment_tp_hit_count(trade_id)
                        logger.info(
                            f"[PAPER TP EXTENDED] {symbol} {direction.upper()} | "
                            f"TP raised {trade['take_profit']:.4f} → {new_tp:.4f} | "
                            f"SL raised {old_sl:.4f} → {new_sl:.4f} | "
                            f"locked ~{lock_pct:.2f}% | "
                            f"Extension {ext_count + 1}/{max_extensions} | "
                            f"Momentum score: {mom_score:.2f} | P&L {pnl_pct:+.2f}%"
                        )
                else:
                    logger.info(
                        f"[TAKE PROFIT] {symbol} @ ${current_price:.4f} | "
                        f"P&L {pnl_pct:+.2f}% | "
                        f"Extensions used: {ext_count} | "
                        f"Original TP was ${trade['take_profit']:.4f}"
                    )
                    self._db.set_state(f"momentum_ext_{trade_id}", 0)
                    with self._db._conn() as conn:
                        conn.execute(
                            "UPDATE trades SET tp_hit_count = 0 WHERE trade_id = ?",
                            (trade_id,)
                        )
                        conn.commit()
                    # Fill at take_profit price, not current_price.
                    # In paper mode the monitor catches TP after price may have
                    # overshot — record at the actual TP level like a real limit order.
                    # In live mode this path is rarely hit (broker reconciliation at
                    # step 2b catches broker-filled TP orders first).
                    tp_fill_price = float(trade.get("take_profit") or current_price)
                    self._attempt_close(trade, tp_fill_price, "take_profit")
                    return

        # ── 10. PIVOT POINT BREAK ─────────────────────────────────────────
        # Only check after 30 min to allow trade to breathe past entry noise
        if age_seconds >= 30 * 60 and strategy != "orb_breakout":
            pivot_exit = self._check_pivot_break(trade, current_price)
            if pivot_exit:
                logger.info(
                    f"[PIVOT BREAK] {symbol}: structural level broken | "
                    f"P&L {pnl_pct:+.2f}% | age {age_hrs:.1f}hr"
                )
                self._db.set_state(f"momentum_ext_{trade_id}", 0)
                self._attempt_close(trade, current_price, pivot_exit)
                return

        # ── 11. STALE POSITION CHECK (3hr ADX/DI) ────────────────────────
        # Note: by this point the performance time stop at 5hr has already
        # fired if profit was below 1.0%. This catches winning positions
        # that are starting to show trend exhaustion.
        if age_seconds >= STALE_THRESHOLD_SEC:
            stale_exit = self._check_stale_position(trade)
            if stale_exit:
                logger.info(
                    f"[STALE EXIT] {symbol}: closing after "
                    f"{age_hrs:.1f}hrs | reason={stale_exit}"
                )
                self._db.set_state(f"momentum_ext_{trade_id}", 0)
                self._attempt_close(trade, current_price, stale_exit)
                return

        # ── 12. TRAILING STOP UPDATE + LOGGING ───────────────────────────
        # Fetch bars on the trade's originating timeframe so the two-bar
        # structural stop trails on the same candle size that generated the entry.
        # A 1h trade must trail on 1h bars — not 5m bars — or the stop advances
        # on every minor 5m high/low instead of on structural 1h breaks.
        #
        # Strategy-preferred trail mode: strategies set preferred_trail_mode in
        # their signal metadata at signal creation time. We honour it here so that
        # mean-reversion strategies (rsi_dip_spike_v4: "none") don't get their
        # stops trailed before RSI can recover to the exit threshold, while
        # trend-follow strategies (adaptive_regime: "two_bar") get structural trails.
        _trade_meta   = _json.loads(trade.get("indicators_json") or "{}")
        _trail_pref   = _trade_meta.get("preferred_trail_mode", "two_bar").lower()
        if _trail_pref == "none":
            updates = None   # strategy manages its own exit — no trailing
        else:
            _trail_bars = self._get_bars(symbol, asset_class, limit=25, timeframe=entry_timeframe)
            updates = risk_manager.calculate_trailing_stop(trade, current_price, bars=_trail_bars)
        if updates:
            old_sl = trade.get("stop_loss", 0)
            old_tp = trade.get("take_profit", 0)
            self._db.update_trade_levels(
                trade_id,
                stop_loss   = updates.get("stop_loss"),
                take_profit = updates.get("take_profit"),
            )
            if "stop_loss" in updates and updates["stop_loss"] != old_sl:
                new_sl = updates["stop_loss"]
                # Track how many times the stop has been ratcheted for dashboard indicator
                _raise_key   = f"sl_raise_count_{trade_id}"
                _raise_count = int(self._db.get_state(_raise_key, default=0) or 0) + 1
                self._db.set_state(_raise_key, _raise_count)
                logger.info(
                    f"[TRAILING STOP] {symbol} {direction.upper()} | "
                    f"SL ratcheted #{_raise_count} ${old_sl:.4f} → ${new_sl:.4f} | "
                    f"Locked in {pnl_pct:+.2f}% gain"
                )
                # ── Live + Paper: cancel old stop order, place new one ────
                # Paper mode: updates fake order ID for tracking parity
                # Live mode:  cancels real Kraken stop, places new one
                if asset_class == "crypto":
                    try:
                        kraken_ex      = self._get_executor().kraken
                        old_stop_id    = trade.get("stop_order_id", "")
                        stop_side      = "buy" if direction == "short" else "sell"
                        new_stop_id    = kraken_ex.update_stop_order(
                            symbol, old_stop_id, qty, stop_side, new_sl
                        )
                        if new_stop_id:
                            self._db.update_trade_order_ids(
                                trade_id, stop_order_id=new_stop_id
                            )
                            logger.info(
                                f"[{'PAPER' if __import__('config').KRAKEN_PAPER_MODE else 'LIVE'}] "
                                f"Stop order updated for {symbol} "
                                f"${old_sl:.4f} → ${new_sl:.4f} | "
                                f"new_id={new_stop_id}"
                            )
                    except Exception as _trail_err:
                        logger.warning(
                            f"Stop order update failed for {symbol}: {_trail_err}"
                        )
            if "take_profit" in updates and updates["take_profit"] != old_tp:
                logger.info(
                    f"[DYNAMIC TP] {symbol} {direction.upper()} | "
                    f"TP adjusted ${old_tp:.4f} → ${updates['take_profit']:.4f}"
                )

    def check_positions(self):
        open_trades = self._db.get_open_trades()
        if not open_trades:
            return

        market_open = _stock_market_open()

        stock_trades  = [t for t in open_trades if t.get("asset_class") == "stock"]
        crypto_trades = [t for t in open_trades if t.get("asset_class") != "stock"]

        if stock_trades and not market_open:
            logger.debug(
                f"Position monitor: skipping {len(stock_trades)} stock position(s) "
                f"— market closed"
            )

        trades_to_check = crypto_trades
        if market_open:
            trades_to_check = trades_to_check + stock_trades

        if not trades_to_check:
            return

        logger.debug(f"Monitoring {len(trades_to_check)} open positions...")

        for trade in trades_to_check:
            try:
                self._process_position(trade)
            except Exception as e:
                logger.error(
                    f"Error processing position {trade['symbol']}: {e}"
                )

        # Price alerts
        try:
            from reporting.alerts import alert_manager
            from data.database import db as _db

            all_symbols = list(config.CRYPTO_WATCHLIST)
            if market_open:
                all_symbols += (
                    list(config.STOCK_WATCHLIST) +
                    ["SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXS",
                     "UVXY", "SVXY", "LABU", "LABD"]
                )

            for sym in all_symbols:
                alerts = _db.get_state(f"price_alerts_{sym}", default=[])
                if not alerts:
                    continue
                asset = "crypto" if "/" in sym else "stock"
                price = self._get_scanner().get_current_price(sym, asset)
                if price:
                    alert_manager.check_price_alerts(sym, price)
        except Exception as e:
            logger.debug(f"Price alert check error: {e}")

    def check_all_positions(self):
        """Alias for backward compatibility."""
        self.check_positions()

    def get_positions_summary(self) -> List[Dict]:
        open_trades = self._db.get_open_trades()
        enriched    = []
        market_open = _stock_market_open()

        for trade in open_trades:
            symbol      = trade["symbol"]
            asset_class = trade["asset_class"]

            if asset_class == "stock" and not market_open:
                current_price = trade["entry_price"]
            else:
                current_price = self._get_scanner().get_current_price(
                    symbol, asset_class
                )
                if current_price is None:
                    current_price = trade["entry_price"]

            qty       = trade["quantity"]
            direction = trade["direction"]

            if direction == "long":
                unrealized_pnl = (current_price - trade["entry_price"]) * qty
                pnl_pct = (current_price / trade["entry_price"] - 1) * 100
            else:
                unrealized_pnl = (trade["entry_price"] - current_price) * qty
                pnl_pct = (trade["entry_price"] / current_price - 1) * 100

            age_seconds = _get_position_age_seconds(trade)
            is_stuck    = self._is_stuck(trade["trade_id"])
            close_attempts = self._get_close_attempts(trade["trade_id"])

            enriched.append({
                **trade,
                "current_price":   round(current_price, 6),
                "unrealized_pnl":  round(unrealized_pnl, 4),
                "pnl_pct":         round(pnl_pct, 2),
                "market_value":    round(current_price * qty, 2),
                "distance_to_sl":  round(
                    abs(current_price - trade["stop_loss"]) / current_price * 100, 2
                ),
                "distance_to_tp":  round(
                    abs(trade["take_profit"] - current_price) / current_price * 100, 2
                ),
                "age_hours":       round(age_seconds / 3600, 2),
                "is_overnight":    bool(trade.get("is_overnight", False)),
                "tp_hit_count":    int(trade.get("tp_hit_count", 0)),
                "is_stuck":        is_stuck,
                "close_attempts":  close_attempts,
            })

        return enriched


# Singleton
monitor = PositionMonitor()
