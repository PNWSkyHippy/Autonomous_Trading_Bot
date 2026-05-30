"""
=============================================================
  RISK MANAGER
  The single most important module. Every trade must pass
  through here before execution. Enforces all rules:
  - 2% max position sizing
  - 1.5% stop-loss on all trades
  - 3% take-profit (initial)
  - 15% max daily loss ceiling
  - 10 consecutive loss halt
  - Max 30 open positions
=============================================================
"""

import logging
from datetime import date, datetime
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

import config
try:
    from reporting.alerts import alert_manager
except Exception:
    alert_manager = None
from data.database import db

logger = logging.getLogger(__name__)


@dataclass
class TradeApproval:
    approved:       bool
    reason:         str
    quantity:       float = 0.0
    position_value: float = 0.0
    stop_loss:      float = 0.0
    take_profit:    float = 0.0
    risk_amount:    float = 0.0


@dataclass
class DailyStatus:
    trading_active:         bool
    halt_reason:            str
    pnl_today:              float
    pnl_pct_today:          float
    consecutive_losses:     int
    trades_today:           int
    capital:                float
    daily_loss_remaining:   float
    starting_capital_today: float   # Locked at market open, never recalculated


class RiskManager:
    """
    Guardian of the trading system. Nothing gets traded without approval here.
    All rules are enforced strictly and logged.
    """

    def __init__(self):
        self._session_date  = None
        self._session       = None
        # Symbol cooldown: tracks (timestamp, was_win) for each recently closed symbol.
        # Loss cooldown  = 10 min  (revenge trade prevention)
        # Win  cooldown  =  2 min  (allow momentum re-entry, just not instant)
        self._last_closed:  dict = {}   # symbol -> {"time": float, "won": bool}
        self._refresh_session()

    def _refresh_session(self):
        """Always re-read session from DB to get latest values.
        The dashboard runs as a separate process from the bot, so in-memory
        state is stale. Always fetch fresh from DB."""
        today = date.today().isoformat()
        self._session_date = today
        self._session = db.get_session(today)

    def _get_capital(self) -> float:
        """
        Calculate true capital by adding all-time realized P&L to starting capital.
        This ensures capital tracks correctly even though Alpaca paper balance never changes.
        """
        try:
            closed    = db.get_all_closed_trades(limit=99999)
            total_pnl = sum(t.get("pnl", 0.0) or 0.0 for t in closed)
            total     = config.STARTING_CAPITAL + total_pnl

            cap  = db.get_latest_capital()
            last = cap["total_capital"] if cap else 0
            if abs(total - last) > 0.50:
                db.record_capital_snapshot(total)

            allocation = getattr(config, "BOT_CAPITAL_ALLOCATION", None)
            if allocation and allocation > 0:
                return min(total, allocation)
            return max(total, 1.0)
        except Exception:
            cap   = db.get_latest_capital()
            total = cap["total_capital"] if cap else config.STARTING_CAPITAL
            allocation = getattr(config, "BOT_CAPITAL_ALLOCATION", None)
            if allocation and allocation > 0:
                return min(total, allocation)
            return total

    def _get_or_lock_starting_capital(self) -> float:
        """
        Get today's starting capital.
        If not yet set (first call of the day), calculate and lock it now.
        Once locked it never changes during the day regardless of restarts.
        This is the core fix for the P&L% restart bug.
        """
        locked = db.get_starting_capital_today()
        if locked > 0:
            return locked
        # First call today — calculate and lock
        capital = self._get_capital()
        db.set_starting_capital_today(capital)
        return capital

    # ----------------------------------------------------------
    #  DAILY STATUS CHECK
    # ----------------------------------------------------------

    def get_daily_status(self) -> DailyStatus:
        self._refresh_session()
        capital         = self._get_capital()
        starting_cap    = self._get_or_lock_starting_capital()
        pnl_today       = self._session.get("pnl_today", 0.0)

        # Use locked starting capital for percentage calculation
        # This never drifts on restart
        pnl_pct = (pnl_today / starting_cap * 100) if starting_cap else 0

        max_daily_loss       = starting_cap * (config.MAX_DAILY_LOSS_PCT / 100)
        daily_loss_remaining = max_daily_loss + pnl_today

        return DailyStatus(
            trading_active          = not bool(self._session.get("trading_halted", 0)),
            halt_reason             = self._session.get("halt_reason", ""),
            pnl_today               = pnl_today,
            pnl_pct_today           = pnl_pct,
            consecutive_losses      = self._session.get("consecutive_losses", 0),
            trades_today            = self._session.get("trades_today", 0),
            capital                 = capital,
            daily_loss_remaining    = max(0, daily_loss_remaining),
            starting_capital_today  = starting_cap,
        )

    def is_trading_allowed(self) -> Tuple[bool, str]:
        self._refresh_session()

        if self._session.get("trading_halted"):
            reason = self._session.get("halt_reason", "Trading halted")
            return False, reason

        capital        = self._get_capital()
        pnl_today      = self._session.get("pnl_today", 0.0)
        starting_cap   = self._get_or_lock_starting_capital()
        max_daily_loss = starting_cap * (config.MAX_DAILY_LOSS_PCT / 100)

        if pnl_today <= -max_daily_loss:
            self._halt_trading(
                f"Daily loss limit reached: ${abs(pnl_today):.2f} "
                f"({abs(pnl_today / starting_cap * 100):.1f}%)"
            )
            return False, f"{config.MAX_DAILY_LOSS_PCT:.0f}% daily loss ceiling reached."

        if self._session.get("consecutive_losses", 0) >= config.MAX_CONSECUTIVE_LOSSES:
            self._halt_trading(f"{config.MAX_CONSECUTIVE_LOSSES} consecutive losing trades")
            return False, f"{config.MAX_CONSECUTIVE_LOSSES} consecutive losing trades. Trading halted."

        open_trades = db.get_open_trades()
        if len(open_trades) >= config.MAX_OPEN_POSITIONS:
            return False, f"Maximum {config.MAX_OPEN_POSITIONS} open positions reached."

        return True, "Trading allowed"

    # ----------------------------------------------------------
    #  POSITION SIZING & TRADE APPROVAL
    # ----------------------------------------------------------

    def approve_trade(self, symbol: str, entry_price: float,
                      signal_score: float, direction: str = "long",
                      custom_stop_loss_pct: float = None,
                      custom_take_profit_pct: float = None,
                      custom_position_pct: float = None,
                      broker_name: str = None,
                      structural_stop_price: float = None,
                      bypass_win_cooldown: bool = False) -> TradeApproval:
        """
        Approve a trade signal and compute position sizing.

        Parameters
        ----------
        broker_name : str, optional
            Explicit broker name for per-broker available cash check.
            Previously threaded through the mutable ``_pending_broker``
            singleton attribute — now a proper argument to eliminate the
            race condition when multiple threaded scans overlap.
        structural_stop_price : float, optional
            Absolute ATR/structure-based stop price from signal metadata.
            When valid (on the correct side of entry), overrides the config
            percentage stop for both SL placement and risk_amount sizing,
            keeping approve_trade in parity with the backtester and the
            fill-time stop logic in trade_executor.
        """
        # ── Guard: entry price ────────────────────────────────────────────
        if entry_price <= 0:
            return TradeApproval(
                approved=False,
                reason=f"Invalid entry price: {entry_price}"
            )

        # ── Guard: custom percentage parameters ──────────────────────────
        for _name, _val in [
            ("custom_stop_loss_pct",   custom_stop_loss_pct),
            ("custom_take_profit_pct", custom_take_profit_pct),
            ("custom_position_pct",    custom_position_pct),
        ]:
            if _val is not None and (_val <= 0 or _val > 100):
                return TradeApproval(
                    approved=False,
                    reason=f"Invalid {_name}: {_val} (must be 0 < value ≤ 100)"
                )

        allowed, reason = self.is_trading_allowed()
        if not allowed:
            return TradeApproval(approved=False, reason=reason)

        min_confidence = config.SIGNAL_TUNING.get(
            "min_signal_confidence", config.MIN_SIGNAL_CONFIDENCE
        )
        if signal_score < min_confidence:
            return TradeApproval(
                approved=False,
                reason=(f"Signal confidence {signal_score:.2f} below "
                        f"minimum {min_confidence:.2f}")
            )

        # ── Capital ───────────────────────────────────────────────────────
        capital = self._get_capital()
        if capital <= 0:
            return TradeApproval(approved=False, reason="No capital available")

        # ── Position sizing ───────────────────────────────────────────────
        position_pct   = (custom_position_pct or config.MAX_POSITION_PCT) / 100
        position_value = capital * position_pct

        # ── Per-broker cash check ─────────────────────────────────────────
        # broker_name is now an explicit parameter — no shared mutable state,
        # no race condition between overlapping threaded signal evaluations.
        if broker_name:
            try:
                from core.broker_manager import broker_manager
                bal              = broker_manager.get_broker_balance(broker_name)
                broker_available = bal.get("available", 0.0)
                # Reject immediately when the broker reports zero or near-zero
                # cash. Do NOT silently fall back to theoretical capital — that
                # masks a real funding problem and can over-commit the account.
                if broker_available <= 0:
                    return TradeApproval(
                        approved=False,
                        reason=(f"No available cash on {broker_name}: "
                                f"${broker_available:.2f}")
                    )
                if broker_available < 1.0:
                    return TradeApproval(
                        approved=False,
                        reason=(f"Insufficient cash on {broker_name}: "
                                f"${broker_available:.2f} available")
                    )
                # Clamp position_value directly to broker cash.
                # Cleaner and safer than reverse-engineering a fake total capital.
                position_value = min(position_value, broker_available)
            except Exception as _brok_err:
                logger.debug(
                    f"Broker balance check skipped ({broker_name}): {_brok_err} "
                    f"— proceeding with theoretical capital"
                )

        # ── Stop / take-profit percentages ────────────────────────────────
        stop_loss_pct   = (custom_stop_loss_pct   or config.DEFAULT_STOP_LOSS_PCT)   / 100
        take_profit_pct = (custom_take_profit_pct or config.DEFAULT_TAKE_PROFIT_PCT) / 100

        # ── Structural stop: derive effective SL for risk sizing ──────────
        # When an absolute ATR/structure-based stop is provided and valid,
        # use it for both SL placement and risk_amount so that position
        # scaling reflects the real dollars at risk (not the config %).
        # This closes the live/backtest parity gap: the backtester already
        # honours structural_stop_price; now approve_trade does too.
        effective_sl     = None          # None → fall back to pct below
        effective_sl_pct = stop_loss_pct
        if structural_stop_price and structural_stop_price > 0:
            if direction == "long" and structural_stop_price < entry_price:
                effective_sl     = structural_stop_price
                effective_sl_pct = (entry_price - structural_stop_price) / entry_price
            elif direction == "short" and structural_stop_price > entry_price:
                effective_sl     = structural_stop_price
                effective_sl_pct = (structural_stop_price - entry_price) / entry_price
            else:
                logger.warning(
                    f"[RISK] {symbol}: structural_stop_price "
                    f"${structural_stop_price:.6f} is on the wrong side of "
                    f"entry ${entry_price:.6f} ({direction}) — using config % stop"
                )

        risk_amount = position_value * effective_sl_pct

        # ── Daily budget constraint ───────────────────────────────────────
        pnl_today             = self._session.get("pnl_today", 0.0)
        starting_cap          = self._get_or_lock_starting_capital()
        max_daily_loss        = starting_cap * (config.MAX_DAILY_LOSS_PCT / 100)
        remaining_loss_budget = max_daily_loss + pnl_today
        if risk_amount > remaining_loss_budget and remaining_loss_budget > 0:
            position_value = remaining_loss_budget / effective_sl_pct
            risk_amount    = remaining_loss_budget
            logger.warning(f"Position scaled down for {symbol}: daily budget constraint")

        # ── Position sanity cap ───────────────────────────────────────────
        # If phantom profitable trades inflated the DB capital, position_value
        # compounds upward each restart. Hard cap at 3× the expected maximum
        # (MAX_POSITION_PCT of STARTING_CAPITAL) so a capital inflation bug
        # can't silently produce $45k positions on a $100k paper account.
        baseline_max = config.STARTING_CAPITAL * (config.MAX_POSITION_PCT / 100)
        sanity_cap   = baseline_max * 3   # 3× allows real growth without infinite blowup
        if position_value > sanity_cap:
            logger.error(
                f"[POSITION SANITY] {symbol}: position ${position_value:,.0f} is "
                f"{position_value / baseline_max:.1f}× normal max (${baseline_max:,.0f}). "
                f"Capital inflation bug likely — capping at ${baseline_max:,.0f}."
            )
            position_value = baseline_max

        quantity = position_value / entry_price
        if quantity <= 0 or position_value < 1.0:
            return TradeApproval(
                approved=False,
                reason="Position too small after risk constraints"
            )

        # ── Compute SL / TP prices ────────────────────────────────────────
        if effective_sl is not None:
            stop_loss = effective_sl
        elif direction == "long":
            stop_loss = entry_price * (1 - stop_loss_pct)
        else:
            stop_loss = entry_price * (1 + stop_loss_pct)

        if direction == "long":
            take_profit = entry_price * (1 + take_profit_pct)
        else:
            take_profit = entry_price * (1 - take_profit_pct)

        # ── Sanity check: enforce minimum SL/TP distance from entry ───────
        # Floating point rounding can occasionally place SL within 1 tick
        # of entry or on the wrong side. Enforce a minimum 0.1% distance.
        MIN_DISTANCE = max(entry_price * 0.001, 0.0001)
        if direction == "long":
            if stop_loss >= entry_price - MIN_DISTANCE:
                stop_loss = entry_price * (1 - max(stop_loss_pct, 0.001))
                logger.warning(
                    f"SL corrected for {symbol} LONG: was too close/wrong side "
                    f"of entry ${entry_price:.4f} — reset to ${stop_loss:.4f}"
                )
            if take_profit <= entry_price + MIN_DISTANCE:
                take_profit = entry_price * (1 + max(take_profit_pct, 0.005))
                logger.warning(
                    f"TP corrected for {symbol} LONG: was too close/wrong side "
                    f"of entry ${entry_price:.4f} — reset to ${take_profit:.4f}"
                )
        else:  # short
            if stop_loss <= entry_price + MIN_DISTANCE:
                stop_loss = entry_price * (1 + max(stop_loss_pct, 0.001))
                logger.warning(
                    f"SL corrected for {symbol} SHORT: was too close/wrong side "
                    f"of entry ${entry_price:.4f} — reset to ${stop_loss:.4f}"
                )
            if take_profit >= entry_price - MIN_DISTANCE:
                take_profit = entry_price * (1 - max(take_profit_pct, 0.005))
                logger.warning(
                    f"TP corrected for {symbol} SHORT: was too close/wrong side "
                    f"of entry ${entry_price:.4f} — reset to ${take_profit:.4f}"
                )

        # ── Symbol cooldown ───────────────────────────────────────────────
        # Loss: 10-min cooldown (revenge trade prevention)
        # Win:   2-min cooldown (allow momentum re-entry, block instant flip)
        LOSS_COOLDOWN_SEC = 10 * 60
        WIN_COOLDOWN_SEC  =  2 * 60
        last = self._last_closed.get(symbol)
        if last is None:
            # On restart, in-memory dict is empty. Check DB for a persisted
            # cooldown that hasn't expired yet (set in record_trade_result).
            try:
                raw = db.get_state(f"cooldown_{symbol}")
                if raw:
                    ts_str, won_str = str(raw).split(",")
                    self._last_closed[symbol] = {
                        "time": float(ts_str),
                        "won":  bool(int(won_str)),
                    }
                    last = self._last_closed[symbol]
            except Exception:
                pass
        if last:
            elapsed  = (datetime.now() - datetime.fromtimestamp(last["time"])).total_seconds()
            cooldown = WIN_COOLDOWN_SEC if last["won"] else LOSS_COOLDOWN_SEC
            label    = "win" if last["won"] else "loss"
            if elapsed < cooldown:
                if last["won"] and bypass_win_cooldown:
                    logger.info(
                        f"Symbol cooldown bypassed for winning breakout re-entry: "
                        f"{symbol} closed {int(elapsed)}s ago"
                    )
                else:
                    remaining = int((cooldown - elapsed) / 60)
                    return TradeApproval(
                        approved=False,
                        reason=(
                            f"Symbol cooldown ({label}): {symbol} closed {int(elapsed)}s ago "
                            f"— wait {remaining}min before re-entry"
                        )
                    )

        logger.info(
            f"APPROVED: {symbol} {direction.upper()} | "
            f"${position_value:.2f} ({position_pct*100:.0f}% capital) | "
            f"Entry: ${entry_price:.6f} | SL: ${stop_loss:.6f} | "
            f"TP: ${take_profit:.6f} | Risk: ${risk_amount:.2f} | "
            f"Score: {signal_score:.2f}"
        )

        return TradeApproval(
            approved        = True,
            reason          = "Approved",
            quantity        = round(quantity, 6),
            position_value  = round(position_value, 2),
            stop_loss       = round(stop_loss, 4),
            take_profit     = round(take_profit, 4),
            risk_amount     = round(risk_amount, 2)
        )

    # ----------------------------------------------------------
    #  TRAILING STOP MANAGEMENT
    # ----------------------------------------------------------

    def calculate_trailing_stop(self, trade: Dict,
                                 current_price: float,
                                 bars=None) -> Optional[Dict]:
        """
        Update trailing stop for an open trade.

        When `bars` (a closed-bar DataFrame, at least lookback+1 rows) is provided,
        uses the two-bar structural model: trail only triggers on a new N-bar high/low
        on a closed bar. This matches the production stop model and the backtester.

        Falls back to percent trailing when bars are unavailable — logged as WARNING
        so the mismatch is visible in the monitoring logs.
        """
        direction  = trade["direction"]
        current_sl = trade["stop_loss"]
        entry      = float(trade.get("entry_price") or 0)
        updates    = {}

        if bars is not None and len(bars) >= 3:
            from core.stop_engine import stop_engine
            triggered, new_sl = stop_engine.check_for_trail_update(
                bars, current_sl, direction
            )
            if triggered and self._trail_clears_fee_hurdle(trade, new_sl, entry, direction):
                updates["stop_loss"] = round(new_sl, 4)
                logger.info(
                    f"[TWO-BAR TRAIL] {trade['symbol']}: "
                    f"${current_sl:.4f} -> ${new_sl:.4f}"
                )
        else:
            trailing_gap = config.TRAILING_STOP_PCT / 100
            if direction == "long":
                new_sl = current_price * (1 - trailing_gap)
                if new_sl > current_sl and self._trail_clears_fee_hurdle(trade, new_sl, entry, direction):
                    updates["stop_loss"] = round(new_sl, 4)
                    logger.warning(
                        f"[PERCENT TRAIL fallback] {trade['symbol']}: "
                        f"${current_sl:.4f} -> ${new_sl:.4f} — bars unavailable"
                    )
            elif direction == "short":
                new_sl = current_price * (1 + trailing_gap)
                if new_sl < current_sl and self._trail_clears_fee_hurdle(trade, new_sl, entry, direction):
                    updates["stop_loss"] = round(new_sl, 4)
                    logger.warning(
                        f"[PERCENT TRAIL fallback] {trade['symbol']}: "
                        f"${current_sl:.4f} -> ${new_sl:.4f} — bars unavailable"
                    )

        return updates if updates else None

    def _trail_clears_fee_hurdle(self, trade, new_sl, entry, direction) -> bool:
        """
        Profit-activation gate (Opus audit 2026-05-29).

        A trailing stop may move INTO the profit zone (past entry) only if the
        gain it would lock clears the asset's round-trip fee hurdle plus a
        buffer. This prevents the ETH/USD-style exit: a trailing stop locking
        +0.41% on crypto where the round-trip fee is 0.62% => a NET LOSS booked
        as a 'stop'. Below the gate, the original (wider) protective stop stands
        and the trade is given room to reach a real, fee-clearing profit.

        Stops that are still on the LOSS side of entry are always allowed
        (that is normal protective trailing and must not be blocked).
        """
        if entry <= 0:
            return True
        asset   = trade.get("asset_class", "crypto")
        min_net = (config.MIN_NET_PROFIT_CRYPTO_PCT if asset == "crypto"
                   else config.MIN_NET_PROFIT_STOCK_PCT) / 100.0
        if direction == "long":
            if new_sl <= entry:
                return True                  # still protective — allow
            locked = (new_sl - entry) / entry
            return locked >= min_net
        else:  # short: locking profit means stop below entry
            if new_sl >= entry:
                return True                  # still protective — allow
            locked = (entry - new_sl) / entry
            return locked >= min_net

    def check_sl_exit(self, trade: Dict, current_price: float) -> Optional[str]:
        direction = trade["direction"]
        sl        = trade["stop_loss"]
        entry     = float(trade.get("entry_price") or 0)
        hit = (
            (direction == "long"  and current_price <= sl) or
            (direction == "short" and current_price >= sl)
        )
        if not hit:
            return None
        # Distinguish a profit-locking trailing stop from a protective stop-out
        # (Opus audit 2026-05-29). Both used to log "stop_loss", masking trailing
        # exits as apparent directional bugs (e.g. ETH/USD SHORT case).
        if entry > 0:
            on_profit_side = (
                (direction == "long"  and sl > entry) or
                (direction == "short" and sl < entry)
            )
            if on_profit_side:
                return "trailing_stop"
        return "stop_loss"

    def check_exit_conditions(self, trade: Dict,
                               current_price: float) -> Optional[str]:
        direction = trade["direction"]
        sl = trade["stop_loss"]
        tp = trade["take_profit"]

        if direction == "long":
            if current_price <= sl: return "stop_loss"
            if current_price >= tp: return "take_profit"
        elif direction == "short":
            if current_price >= sl: return "stop_loss"
            if current_price <= tp: return "take_profit"

        return None

    # ----------------------------------------------------------
    #  POST-TRADE RECORDING
    # ----------------------------------------------------------

    def record_trade_result(self, pnl: float, trade_won: bool,
                            symbol: str = ""):
        """Update session state after a trade closes."""
        # Record close time and outcome for symbol cooldown.
        # Persisted to DB so cooldowns survive bot restarts.
        if symbol:
            import time as _time
            now = _time.time()
            self._last_closed[symbol] = {"time": now, "won": trade_won}
            try:
                db.set_state(f"cooldown_{symbol}", f"{now},{int(trade_won)}")
            except Exception:
                pass
        self._refresh_session()
        session_date = date.today().isoformat()

        current_losses = self._session.get("consecutive_losses", 0)
        new_losses     = 0 if trade_won else current_losses + 1
        pnl_today      = self._session.get("pnl_today", 0.0) + pnl
        trades_today   = self._session.get("trades_today", 0) + 1

        db.update_session(session_date,
            consecutive_losses = new_losses,
            pnl_today          = pnl_today,
            trades_today       = trades_today
        )
        self._session["consecutive_losses"] = new_losses
        self._session["pnl_today"]          = pnl_today
        self._session["trades_today"]       = trades_today

        if not trade_won:
            logger.warning(
                f"Losing trade recorded. Consecutive losses: {new_losses}"
            )
            if new_losses >= config.MAX_CONSECUTIVE_LOSSES:
                self._halt_trading(
                    f"{config.MAX_CONSECUTIVE_LOSSES} consecutive losing trades"
                )
        else:
            logger.info(f"Winning trade: +${pnl:.2f}. Consecutive loss counter reset.")

    def _halt_trading(self, reason: str):
        session_date = date.today().isoformat()
        db.update_session(session_date, trading_halted=1, halt_reason=reason)
        self._session["trading_halted"] = 1
        self._session["halt_reason"]    = reason
        logger.critical(f"[HALT] TRADING HALTED: {reason}")
        try:
            if alert_manager:
                alert_manager.trading_halted(reason)
        except Exception:
            pass

    def manual_halt(self, reason: str = "Manual halt by user"):
        self._halt_trading(reason)

    def manual_resume(self):
        session_date = date.today().isoformat()
        db.update_session(session_date,
            trading_halted     = 0,
            halt_reason        = None,
            consecutive_losses = 0
        )
        self._session["trading_halted"] = 0
        self._session["halt_reason"]    = None
        logger.info("Trading resumed by manual command.")

    # ----------------------------------------------------------
    #  WITHDRAWAL PROCESSING
    # ----------------------------------------------------------

    def process_withdrawal(self, amount: float,
                            reason: str = "Living expenses") -> Dict:
        capital = self._get_capital()
        if amount > capital * 0.5:
            return {
                "approved": False,
                "message": (
                    f"Withdrawal of ${amount:.2f} exceeds 50% of capital "
                    f"(${capital:.2f}). Please confirm this is intentional."
                )
            }
        if amount <= 0:
            return {"approved": False, "message": "Invalid withdrawal amount."}

        new_capital = capital - amount
        db.record_withdrawal(amount, reason, capital, new_capital)
        db.log_capital(
            total     = new_capital,
            available = new_capital,
            invested  = 0,
            note      = f"Withdrawal: ${amount:.2f} - {reason}"
        )

        logger.info(
            f"Withdrawal processed: ${amount:.2f}. New capital: ${new_capital:.2f}"
        )
        return {
            "approved":       True,
            "withdrawn":      amount,
            "capital_before": capital,
            "capital_after":  new_capital,
            "message": (
                f"Withdrawal of ${amount:.2f} processed. "
                f"Capital adjusted to ${new_capital:.2f}"
            )
        }

    def reset_session(self):
        """Reset the daily session state (called at end of day)."""
        session_date = date.today().isoformat()
        db.update_session(session_date,
            trading_halted          = 0,
            halt_reason             = None,
            consecutive_losses      = 0,
            pnl_today               = 0.0,
            trades_today            = 0,
            starting_capital_today  = 0.0
        )
        self._session = db.get_session(session_date)
        logger.info("Session state reset for new trading day.")


# Singleton
risk_manager = RiskManager()
