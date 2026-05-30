"""
=============================================================
  TRADE EXECUTOR
  Sends approved orders to the correct broker:
    Stocks: IBKR (preferred, if enabled) → Alpaca (fallback)
    Crypto: Coinbase → Kraken → paper simulation

  IBKR cash account mode:
    - No PDT rule (unlimited day trades with settled funds)
    - settlement_tracker checks settled capital before every IBKR stock trade
    - T+1 settlement (SEC standard since May 2024)
    - Full bracket order support including 1-2 char tickers (unlike Alpaca)

  To switch from Alpaca to IBKR:
    1. Set IBKR_ENABLED = True in config.py
    2. Set IBKR_ACCOUNT = "DUxxxxxxx" in .env
    3. Start TWS or IB Gateway on port 7497 (paper) or 7496 (live)
    4. Set IBKR_PAPER_MODE = True/False to match your TWS session
=============================================================
"""

import uuid
import time
import logging
from datetime import datetime
from typing import Optional, Dict
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import alpaca_trade_api as tradeapi
import ccxt

import config
try:
    from core.settlement_tracker import settlement_tracker
except Exception:
    settlement_tracker = None
from reporting.alerts import alert_manager
from core.risk_manager import risk_manager, TradeApproval
from core.kraken_executor import KrakenExecutor
from core.exchange_capabilities import exchange_capabilities
from data.database import db

logger = logging.getLogger(__name__)


def _stock_entries_allowed_now() -> bool:
    """Stocks may open during regular hours, but not after the EOD close window."""
    now_et = datetime.now(ZoneInfo("America/New_York"))

    if now_et.weekday() > 4:
        return False

    after_open = (
        now_et.hour > 9 or
        (now_et.hour == 9 and now_et.minute >= 30)
    )
    before_eod_close = (
        now_et.hour < 15 or
        (now_et.hour == 15 and now_et.minute < 45)
    )
    return after_open and before_eod_close


class AlpacaExecutor:
    """Executes stock trades via Alpaca Markets API."""

    def __init__(self):
        self.api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL
        )

    def submit_order(self, symbol: str, qty: float, side: str,
                     stop_loss: float, take_profit: float) -> Optional[Dict]:
        """
        Submit a stock order to Alpaca.
        Short tickers (1-2 chars) use plain market order — Alpaca silently
        rejects bracket orders for these, causing ghost trades.
        """
        if len(symbol) <= 2:
            return self._submit_plain_market(symbol, qty, side, stop_loss, take_profit)

        try:
            order = self.api.submit_order(
                symbol        = symbol,
                qty           = int(qty) if qty >= 1 else qty,
                side          = side,
                type          = "market",
                time_in_force = "day",
                order_class   = "bracket",
                stop_loss     = {"stop_price": str(round(stop_loss, 2))},
                take_profit   = {"limit_price": str(round(take_profit, 2))}
            )
            logger.info(
                f"Alpaca bracket order submitted: {symbol} {side} {qty} shares | "
                f"SL={stop_loss} TP={take_profit} | Order ID: {order.id}"
            )
            return {
                "broker_order_id":  order.id,
                "status":           order.status,
                "filled_qty":       float(order.filled_qty or 0),
                "filled_avg_price": float(order.filled_avg_price or 0),
                "manual_sl_tp":     False
            }
        except Exception as e:
            logger.error(f"Alpaca bracket order failed for {symbol}: {e}")
            logger.warning(f"Falling back to plain market order for {symbol}")
            return self._submit_plain_market(symbol, qty, side, stop_loss, take_profit)

    def _submit_plain_market(self, symbol: str, qty: float, side: str,
                             stop_loss: float, take_profit: float) -> Optional[Dict]:
        """Plain market order — used for short tickers or as bracket fallback."""
        try:
            order = self.api.submit_order(
                symbol        = symbol,
                qty           = int(qty) if qty >= 1 else qty,
                side          = side,
                type          = "market",
                time_in_force = "day"
            )
            logger.info(
                f"[SHORT TICKER] Alpaca plain market order: {symbol} {side} "
                f"{qty} shares | SL/TP managed by position monitor | "
                f"SL={stop_loss:.4f} TP={take_profit:.4f} | Order ID: {order.id}"
            )
            return {
                "broker_order_id":  order.id,
                "status":           order.status,
                "filled_qty":       float(order.filled_qty or 0),
                "filled_avg_price": float(order.filled_avg_price or 0),
                "manual_sl_tp":     True
            }
        except Exception as e:
            logger.error(f"Alpaca plain market order also failed for {symbol}: {e}")
            return None

    def cancel_open_orders(self, symbol: str) -> int:
        """
        Cancel all open orders for a symbol before closing the position.
        This is CRITICAL for bracket orders — the limit (TP) and stop (SL)
        legs must be canceled first or Alpaca will reject the close order
        with 'insufficient qty available'.

        Returns number of orders canceled.
        """
        canceled = 0
        try:
            open_orders = self.api.list_orders(status="open", symbols=[symbol])
            for order in open_orders:
                try:
                    self.api.cancel_order(order.id)
                    canceled += 1
                    logger.info(
                        f"Canceled open order for {symbol}: "
                        f"{order.order_type} {order.side} @ {order.limit_price or order.stop_price} "
                        f"| ID: {order.id}"
                    )
                except Exception as e:
                    logger.warning(f"Could not cancel order {order.id} for {symbol}: {e}")
            if canceled > 0:
                # Brief pause to let Alpaca process cancellations before closing
                time.sleep(0.5)
        except Exception as e:
            logger.warning(f"Error listing open orders for {symbol}: {e}")
        return canceled

    def close_position(self, symbol: str) -> bool:
        """
        Close a position at Alpaca.
        ALWAYS cancels open orders first (bracket legs) before sending
        the close order. Without this, Alpaca returns 'insufficient qty
        available' because the bracket order has already allocated the shares.
        """
        # Step 1: Cancel all open orders (bracket legs) first
        canceled = self.cancel_open_orders(symbol)
        if canceled > 0:
            logger.info(f"Canceled {canceled} open order(s) for {symbol} before closing")

        # Step 2: Now close the actual position
        try:
            self.api.close_position(symbol)
            logger.info(f"Alpaca position closed: {symbol}")
            return True
        except Exception as e:
            # If close_position fails, try a manual market sell
            logger.warning(f"close_position failed for {symbol}: {e} — trying manual market order")
            try:
                pos = self.api.get_position(symbol)
                qty = abs(float(pos.qty))
                side = "sell" if float(pos.qty) > 0 else "buy"
                self.api.submit_order(
                    symbol        = symbol,
                    qty           = qty,
                    side          = side,
                    type          = "market",
                    time_in_force = "day"
                )
                logger.info(f"Manual market close submitted for {symbol} {side} {qty}")
                return True
            except Exception as e2:
                logger.error(f"Failed to close {symbol}: {e2}")
                return False

    def get_position(self, symbol: str) -> Optional[Dict]:
        try:
            pos = self.api.get_position(symbol)
            return {
                "symbol":        pos.symbol,
                "qty":           float(pos.qty),
                "market_value":  float(pos.market_value),
                "unrealized_pl": float(pos.unrealized_pl),
                "current_price": float(pos.current_price),
                "avg_entry":     float(pos.avg_entry_price)
            }
        except Exception:
            return None

    def get_account(self) -> Dict:
        acct = self.api.get_account()
        return {
            "equity":          float(acct.equity),
            "cash":            float(acct.cash),
            "buying_power":    float(acct.buying_power),
            "portfolio_value": float(acct.portfolio_value)
        }

    def close_all_stock_positions(self):
        """
        Close all open stock positions at EOD.
        Cancels all open orders first, then closes positions.
        """
        try:
            # Cancel ALL open orders first before closing any positions
            try:
                all_open_orders = self.api.list_orders(status="open")
                for order in all_open_orders:
                    try:
                        self.api.cancel_order(order.id)
                        logger.info(f"EOD: Canceled order {order.id} for {order.symbol}")
                    except Exception as e:
                        logger.warning(f"EOD: Could not cancel order {order.id}: {e}")
                if all_open_orders:
                    time.sleep(1)  # Let cancellations process
            except Exception as e:
                logger.warning(f"EOD: Error canceling open orders: {e}")

            # Now close all positions
            positions = self.api.list_positions()
            for pos in positions:
                try:
                    self.api.close_position(pos.symbol)
                    logger.info(f"EOD: Closed position {pos.symbol}")
                except Exception as e:
                    logger.error(f"EOD: Failed to close {pos.symbol}: {e}")
            logger.info(f"EOD close complete: {len(positions)} position(s) processed.")
        except Exception as e:
            logger.error(f"Error in EOD close all: {e}")


class KrakenSpotExecutor:
    """
    Legacy direct-ccxt Kraken executor.
    NOTE: The primary crypto execution path is KrakenExecutor (core/kraken_executor.py),
    which handles paper mode, stop orders, and position tracking. This class was
    previously named CoinbaseExecutor but always used Kraken internally. It is
    retained for reference and potential fallback use; the main TradeExecutor
    routes all crypto through self.kraken (KrakenExecutor), not this class.
    """

    def __init__(self):
        self.exchange = ccxt.kraken({
            "apiKey":          config.KRAKEN_API_KEY,
            "secret":          config.KRAKEN_SECRET_KEY,
            "enableRateLimit": True,
        })

    def submit_order(self, symbol: str, qty: float, side: str,
                     stop_loss: float, take_profit: float) -> Optional[Dict]:
        try:
            order = self.exchange.create_market_order(symbol, side, qty)
            filled_price = float(order.get("average") or order.get("price") or 0)
            filled_qty   = float(order.get("filled") or qty)
            logger.info(
                f"Coinbase order filled: {symbol} {side} "
                f"{filled_qty} @ ${filled_price:.4f}"
            )
            return {
                "broker_order_id":  order["id"],
                "status":           order["status"],
                "filled_qty":       filled_qty,
                "filled_avg_price": filled_price
            }
        except Exception as e:
            logger.error(f"Coinbase order failed for {symbol}: {e}")
            return None

    def close_position(self, symbol: str, qty: float, side: str) -> bool:
        try:
            close_side = "sell" if side == "buy" else "buy"
            self.exchange.create_market_order(symbol, close_side, qty)
            logger.info(f"Coinbase position closed: {symbol}")
            return True
        except Exception as e:
            logger.error(f"Failed to close Coinbase {symbol}: {e}")
            return False

    def get_balance(self, currency: str = "USD") -> float:
        try:
            balance = self.exchange.fetch_balance()
            return float(balance.get(currency, {}).get("free", 0))
        except Exception as e:
            logger.debug(f"Balance fetch error: {e}")
            return 0.0


class TradeExecutor:
    """
    Unified executor that coordinates risk approval, order submission,
    and logging.

    Stock broker priority:
      1. IBKR  — if IBKR_ENABLED=True and TWS is reachable
                  Cash account: no PDT, settlement tracked by settlement_tracker
      2. Alpaca — fallback (paper mode by default)

    Crypto broker priority:
      1. Kraken  — if KRAKEN_ENABLED=True
      2. Coinbase
      3. Paper simulation — if both fail
    """

    def __init__(self, db_ref=None, risk_manager_ref=None, broker_manager_ref=None):
        self._db           = db_ref or db
        self._risk_manager = risk_manager_ref or risk_manager

        self.alpaca   = AlpacaExecutor()
        self.kraken   = KrakenExecutor(
            api_key    = config.KRAKEN_API_KEY,
            api_secret = config.KRAKEN_SECRET_KEY,
            paper      = config.KRAKEN_PAPER_MODE
        )

        self._ibkr = None
        if getattr(config, "IBKR_ENABLED", False):
            try:
                from core.ibkr_executor import IBKRExecutor
                self._ibkr = IBKRExecutor()
                logger.info(
                    f"IBKR executor initialized — "
                    f"{'paper' if config.IBKR_PAPER_MODE else 'LIVE'} mode | "
                    f"account={config.IBKR_ACCOUNT or 'default'}"
                )
            except Exception as e:
                logger.warning(
                    f"IBKR executor failed to initialize: {e} — "
                    f"falling back to Alpaca for stocks"
                )

    def execute_signal(self, signal) -> Optional[str]:
        logger.info(
            f"Attempting to execute signal: {signal.symbol} {signal.direction}"
        )

        if signal.asset_class == "stock" and not _stock_entries_allowed_now():
            logger.info(
                f"Trade rejected for {signal.symbol}: stock entries closed "
                f"after 15:45 ET EOD cutoff"
            )
            return None

        # ── Short eligibility check ───────────────────────────────────────
        # Before doing any expensive approval work, verify the broker actually
        # allows shorting this symbol. Kraken only supports margin on certain
        # pairs; Alpaca requires easy_to_borrow. Fails-open on API errors so
        # a temporary outage does not block all shorts.
        if signal.direction == "short":
            _broker_for_cap = self._get_broker_name(signal)
            if not exchange_capabilities.can_short(signal.symbol, _broker_for_cap):
                logger.info(
                    f"[SHORT BLOCKED] {signal.symbol} on {_broker_for_cap}: "
                    f"not margin-eligible — short signal dropped"
                )
                return None

        custom_sl  = signal.indicators.get("custom_stop_loss_pct")
        custom_tp  = signal.indicators.get("custom_take_profit_pct")
        custom_pos = signal.indicators.get("custom_position_pct")
        bypass_win_cooldown = bool(signal.indicators.get("bypass_win_cooldown", False))

        # Pass broker name and structural stop directly as explicit args —
        # no shared mutable state, no race condition between threaded scans.
        broker_name_hint      = self._get_broker_name(signal)
        structural_stop_hint  = signal.indicators.get("structural_stop_price")
        approval = self._risk_manager.approve_trade(
            symbol                 = signal.symbol,
            entry_price            = signal.current_price,
            signal_score           = signal.score,
            direction              = signal.direction,
            custom_stop_loss_pct   = custom_sl,
            custom_take_profit_pct = custom_tp,
            custom_position_pct    = custom_pos,
            broker_name            = broker_name_hint,
            structural_stop_price  = structural_stop_hint,
            bypass_win_cooldown    = bypass_win_cooldown,
        )
        if not approval.approved:
            # If IBKR was selected but has $0 cash (drained paper account or
            # gateway down), automatically retry with Alpaca before giving up.
            if (broker_name_hint == "ibkr"
                    and "No available cash on ibkr" in (approval.reason or "")
                    and signal.asset_class == "stock"):
                logger.warning(
                    f"IBKR has $0 cash for {signal.symbol} — retrying with Alpaca"
                )
                broker_name_hint = "alpaca"
                approval = self._risk_manager.approve_trade(
                    symbol                 = signal.symbol,
                    entry_price            = signal.current_price,
                    signal_score           = signal.score,
                    direction              = signal.direction,
                    custom_stop_loss_pct   = custom_sl,
                    custom_take_profit_pct = custom_tp,
                    custom_position_pct    = custom_pos,
                    broker_name            = "alpaca",
                    structural_stop_price  = structural_stop_hint,
                    bypass_win_cooldown    = bypass_win_cooldown,
                )
                if not approval.approved:
                    logger.info(
                        f"Trade rejected for {signal.symbol} (Alpaca fallback): "
                        f"{approval.reason}"
                    )
                    return None
            else:
                logger.info(f"Trade rejected for {signal.symbol}: {approval.reason}")
                return None

        if (signal.asset_class == "stock" and
                self._ibkr is not None and
                settlement_tracker is not None and
                getattr(config, "CASH_ACCOUNT_MODE", True)):
            allowed, reason, available = settlement_tracker.can_open_trade(
                approval.position_value
            )
            if not allowed:
                logger.info(
                    f"Trade blocked by settlement tracker: {signal.symbol} — {reason}"
                )
                return None

        trade_id     = str(uuid.uuid4())[:12]
        broker_result = self._submit(signal, approval, broker_name_hint)
        if not broker_result:
            logger.error(f"Order submission failed for {signal.symbol}")
            return None

        entry_price = broker_result.get("filled_avg_price") or signal.current_price
        if entry_price == 0:
            entry_price = signal.current_price

        # Recalculate quantity from actual fill price.
        # approval.quantity was sized using signal.current_price which can be stale
        # or from a bad feed (e.g. $0.05 instead of $42.79). Using the real fill
        # price ensures quantity × fill_price == position_value and prevents
        # phantom PNL on close (e.g. 39,952 qty × $1.14 = $45k on a $2k position).
        if entry_price > 0 and approval.position_value > 0:
            actual_quantity = round(approval.position_value / entry_price, 6)
        else:
            actual_quantity = approval.quantity

        tp_pct = (custom_tp or config.DEFAULT_TAKE_PROFIT_PCT) / 100

        if signal.direction == "long":
            take_profit = entry_price * (1 + tp_pct)
        else:
            take_profit = entry_price * (1 - tp_pct)

        # ── Structural stop: recompute at fill price (hard pre-live gate) ───
        # The scanner embeds structural_stop_price from bars at signal time.
        # An adverse fill (price moved between signal and execution) can place
        # the stop above the fill price for longs (or below for shorts), causing
        # an immediate stop-out on the next monitor cycle.
        # Fix: re-run initial_stop_from_tail() on fresh bars after the fill is
        # confirmed, then validate the result is on the correct side of fill price.
        structural_stop = signal.indicators.get("structural_stop_price")
        if structural_stop is not None:
            try:
                from core.stop_engine import stop_engine as _se
                from scanners.market_scanner import scanner as _sc
                _tf = signal.indicators.get(
                    "timeframe", "5Min" if signal.asset_class == "stock" else "5m"
                )
                if signal.asset_class == "stock":
                    _fresh = _sc.stock_scanner.get_bars(
                        signal.symbol, timeframe=_tf, limit=10
                    )
                else:
                    _fresh = _sc.crypto_scanner.get_ohlcv(
                        signal.symbol, timeframe=_tf, limit=10
                    )
                if _fresh is not None and len(_fresh) >= 3:
                    _new_stop = _se.initial_stop_from_tail(_fresh, signal.direction)
                    _valid = (
                        (signal.direction == "long"  and _new_stop < entry_price) or
                        (signal.direction == "short" and _new_stop > entry_price)
                    )
                    if _valid:
                        logger.info(
                            f"[STOP REFRESH] {signal.symbol}: structural stop "
                            f"recomputed at fill — "
                            f"${structural_stop:.6f} → ${_new_stop:.6f}"
                        )
                        structural_stop = _new_stop
                    else:
                        logger.warning(
                            f"[STOP REFRESH] {signal.symbol}: fresh structural stop "
                            f"${_new_stop:.6f} is on the wrong side of fill price "
                            f"${entry_price:.6f} ({signal.direction}) — "
                            f"falling back to percent-based stop"
                        )
                        structural_stop = None   # force percent fallback below
            except Exception as _sr_err:
                logger.warning(
                    f"[STOP REFRESH] {signal.symbol}: could not recompute "
                    f"structural stop at fill: {_sr_err}"
                )

        # ── Stop-loss selection with minimum-distance guard ──────────────────
        # Structural stop wins over percent-based stop UNLESS it would result in
        # a tighter stop than the strategy's own declared stop_loss_pct.
        # Scenario: DCA accumulator enters at a local bottom; the prior 2 bars
        # are also at lows, so initial_stop_from_tail() places the stop almost
        # at entry price (< 0.3%), overriding DCA's 2% fallback and causing an
        # immediate stop-out on any noise.  The guard enforces the declared %
        # as a hard floor — structural stops must be at least that far away.
        strategy_sl_pct = (custom_sl or config.DEFAULT_STOP_LOSS_PCT) / 100
        if structural_stop is not None:
            if signal.direction == "long":
                min_stop = entry_price * (1 - strategy_sl_pct)
                if structural_stop > min_stop:
                    logger.info(
                        f"[STOP GUARD] {signal.symbol}: structural ${structural_stop:.6f} "
                        f"tighter than declared {strategy_sl_pct*100:.2f}% floor "
                        f"(${min_stop:.6f}) — using percent floor"
                    )
                    stop_loss = min_stop
                else:
                    stop_loss = structural_stop
            else:  # short
                min_stop = entry_price * (1 + strategy_sl_pct)
                if structural_stop < min_stop:
                    logger.info(
                        f"[STOP GUARD] {signal.symbol}: structural ${structural_stop:.6f} "
                        f"tighter than declared {strategy_sl_pct*100:.2f}% floor "
                        f"(${min_stop:.6f}) — using percent floor"
                    )
                    stop_loss = min_stop
                else:
                    stop_loss = structural_stop
        else:
            stop_loss = (entry_price * (1 - strategy_sl_pct) if signal.direction == "long"
                         else entry_price * (1 + strategy_sl_pct))

        # Use the broker that actually filled the order (tagged in _submit).
        # Fallback to _get_broker_name() only if the result has no tag
        # (e.g. paper fill or legacy code path).
        broker_name = broker_result.get("actual_broker") or self._get_broker_name(signal)

        trade_record = {
            "trade_id":        trade_id,
            "symbol":          signal.symbol,
            "asset_class":     signal.asset_class,
            "direction":       signal.direction,
            "entry_time":      datetime.now().isoformat(),
            "entry_price":     round(entry_price, 6),
            "quantity":        actual_quantity,
            "strategy_name":   signal.indicators.get("strategy_name", "original"),
            "entry_timeframe": signal.indicators.get(
                "timeframe",
                "5Min" if signal.asset_class == "stock" else "5m"
            ),
            "position_value":  approval.position_value,
            "stop_loss":       round(stop_loss, 6),
            "take_profit":     round(take_profit, 6),
            "signal_score":    signal.score,
            "indicators":      signal.indicators,
            "broker":          broker_name,
            "broker_order_id": broker_result.get("broker_order_id")
        }
        self._db.open_trade(trade_record)

        try:
            alert_manager.trade_opened({
                "symbol": signal.symbol, "direction": signal.direction,
                "entry_price": entry_price, "stop_loss": stop_loss,
                "take_profit": take_profit, "position_value": approval.position_value,
                "strategy_name": signal.indicators.get("strategy_name", "unknown")
            })
        except Exception:
            pass

        logger.info(
            f"[TRADE OPEN] {signal.symbol} {signal.direction.upper()} | "
            f"Entry: ${entry_price:.4f} | Qty: {actual_quantity:.4f} | "
            f"SL: ${stop_loss:.4f} | TP: ${take_profit:.4f} | "
            f"Broker: {broker_name} | ID: {trade_id}"
        )

        # ── Update broker available cash (deduct position value) ──────────
        try:
            from core.broker_manager import broker_manager
            bal = broker_manager.get_broker_balance(broker_name)
            new_invested  = bal.get("invested",  0.0) + approval.position_value
            new_available = max(0.0, bal.get("available", 0.0) - approval.position_value)
            broker_manager.update_broker_balance(
                broker_name, bal.get("balance", 0.0),
                new_invested, new_available
            )
        except Exception as _bm_err:
            logger.debug(f"Broker balance update on open failed: {_bm_err}")

        # ── Place real stop order on Kraken (live + paper) ────────────────
        # In paper mode this stores a fake order ID for tracking logic parity.
        # In live mode this places an actual stop-market order on Kraken.
        if signal.asset_class == "crypto" and broker_name in ("kraken", "paper"):
            stop_side = "buy" if signal.direction == "short" else "sell"
            stop_order_id = self.kraken.place_stop_order(
                signal.symbol, actual_quantity, stop_side, round(stop_loss, 6)
            )
            if stop_order_id:
                self._db.update_trade_order_ids(trade_id, stop_order_id=stop_order_id)
                logger.info(
                    f"[{'PAPER' if config.KRAKEN_PAPER_MODE else 'LIVE'}] "
                    f"Stop order placed for {signal.symbol} @ ${stop_loss:.4f} "
                    f"| order_id={stop_order_id}"
                )

        return trade_id

    def close_trade(self, trade: Dict, current_price: float,
                    exit_reason: str) -> bool:
        symbol      = trade["symbol"]
        direction   = trade["direction"]
        qty         = trade["quantity"]
        entry_price = trade["entry_price"]
        asset_class = trade["asset_class"]
        broker      = trade.get("broker", "alpaca")

        success = False
        if asset_class == "stock":
            if broker == "ibkr" and self._ibkr is not None:
                success = self._ibkr.close_position(symbol)
                # If IBKR close failed, ALWAYS try Alpaca too before giving up.
                # Do NOT short-circuit with success=True just because IBKR
                # doesn't list the position — it may have been opened via the
                # Alpaca fallback path even though DB records broker=ibkr.
                if not success:
                    logger.warning(
                        f"[IBKR CLOSE FAILED] {symbol}: trying Alpaca as well "
                        f"(position may exist on either broker)"
                    )
                    alpaca_success = self.alpaca.close_position(symbol)
                    if alpaca_success:
                        logger.info(f"[ALPACA FALLBACK CLOSE] {symbol}: closed via Alpaca")
                        # Correct the DB broker field so future lookups are accurate
                        trade_id = trade.get("trade_id")
                        if trade_id:
                            try:
                                self._db.update_trade_broker(trade_id, "alpaca")
                            except Exception:
                                pass
                        success = True
                    else:
                        logger.warning(f"[ALPACA FALLBACK CLOSE] {symbol}: not found on Alpaca either")
                        # Both brokers checked — do NOT mark success here.
                        # IBKR may be temporarily down; leave trade open in DB
                        # so EOD Pass 3 (IBKR sweep) can retry when it comes back.
                        success = False
                        logger.warning(
                            f"[CLOSE SKIP] {symbol}: not found on IBKR or Alpaca "
                            f"— leaving open in DB for EOD sweep retry"
                        )
            else:
                success = self.alpaca.close_position(symbol)
                # If Alpaca doesn't have it, check IBKR too
                if not success and self._ibkr is not None:
                    logger.warning(
                        f"[ALPACA CLOSE FAILED] {symbol}: trying IBKR as well"
                    )
                    ibkr_success = self._ibkr.close_position(symbol)
                    if ibkr_success:
                        logger.info(f"[IBKR FALLBACK CLOSE] {symbol}: closed via IBKR")
                        # Correct the DB broker field
                        trade_id = trade.get("trade_id")
                        if trade_id:
                            try:
                                self._db.update_trade_broker(trade_id, "ibkr")
                            except Exception:
                                pass
                        success = True
        elif asset_class == "crypto":
            # Kraken is the default crypto executor — Coinbase API is deprecated.
            side = "buy" if direction == "long" else "sell"
            if config.KRAKEN_ENABLED:
                success = self.kraken.close_position(symbol, qty, side)
                # ── Cancel the resting stop order (Opus audit 2026-05-29) ──────
                # close_position sends only a market close; the stop-market order
                # placed at entry stays LIVE on Kraken. Cancel it here so every
                # non-momentum-ride exit can't leave an orphaned stop that later
                # fires a phantom order or opens an unintended short.
                if success:
                    _stop_id = trade.get("stop_order_id", "")
                    if _stop_id:
                        try:
                            self.kraken.cancel_order(symbol, _stop_id)
                            logger.info(
                                f"[STOP CANCEL] {symbol}: resting stop {_stop_id} "
                                f"cancelled on close"
                            )
                        except Exception as _co_err:
                            logger.warning(
                                f"Failed to cancel resting stop {_stop_id} "
                                f"for {symbol}: {_co_err}"
                            )
            else:
                logger.info(f"[PAPER] Simulated close: {symbol} {side.upper()} @ ${current_price:.4f}")
                success = True

        if not success:
            logger.error(f"Failed to close position {symbol}")
            return False

        if direction == "long":
            pnl = (current_price - entry_price) * qty
        else:
            pnl = (entry_price - current_price) * qty

        # ── Brokerage fee simulation (matches backtester rates) ───────────
        if asset_class == "crypto":
            fee_rt_pct = (config.BT_COMMISSION_CRYPTO_PCT + config.BT_SLIPPAGE_CRYPTO_PCT) / 100 * 2
        else:
            fee_rt_pct = (config.BT_COMMISSION_STOCK_PCT + config.BT_SLIPPAGE_STOCK_PCT) / 100 * 2
        fees_paid = round(entry_price * qty * fee_rt_pct, 4)
        pnl       = pnl - fees_paid

        # ── PnL% — calculate directly from price move, not position_value ─
        # position_value in DB can drift; price-based calc is always correct.
        # pnl_pct = % move from entry to exit, always positive for wins.
        if entry_price and entry_price > 0:
            raw_move_pct = ((current_price - entry_price) / entry_price) * 100
            pnl_pct      = raw_move_pct if direction == "long" else -raw_move_pct
        else:
            pnl_pct = (pnl / trade["position_value"]) * 100 if trade["position_value"] else 0

        # ── Sanity check: pnl_pct should never exceed 3x the trade's actual SL ─
        # If it does, the price feed was corrupt (e.g. Kraken returning a stale
        # listing price).  Cap pnl_pct AND back-calculate a sane exit_price so
        # the DB doesn't record a nonsense price alongside capped P&L numbers.
        #
        # Cap is based on the trade's stored stop_loss price (not the config TP)
        # so HV coins with wider stops get a proportionally wider cap while
        # tight-stop coins (like grid_bot 0.8%) get a tighter cap.
        # Floor: 3× config SL so we never cap a legitimate exit on a volatile bar.
        sl_price = trade.get("stop_loss")
        if sl_price and sl_price > 0 and entry_price and entry_price > 0:
            actual_sl_pct = abs(entry_price - sl_price) / entry_price * 100
            max_sane_pct  = max(actual_sl_pct * 3, config.DEFAULT_STOP_LOSS_PCT * 3)
        else:
            max_sane_pct  = config.DEFAULT_STOP_LOSS_PCT * 3
        if abs(pnl_pct) > max_sane_pct:
            logger.warning(
                f"[PNL SANITY] {symbol}: pnl_pct={pnl_pct:.2f}% exceeds 3x SL cap "
                f"({max_sane_pct:.1f}%). Bad price feed suspected — "
                f"entry={entry_price} bad_exit={current_price} qty={qty}"
            )
            pnl_pct = max_sane_pct if pnl_pct > 0 else -max_sane_pct
            # Recalculate pnl dollar amount to match capped pct
            pnl = (pnl_pct / 100) * trade["position_value"]
            # Back-calculate a sane exit_price so DB isn't poisoned with the
            # bad feed price — this implied price is consistent with capped pnl
            if direction == "long":
                current_price = entry_price * (1 + pnl_pct / 100)
            else:
                current_price = entry_price * (1 - pnl_pct / 100)
            logger.warning(
                f"[PNL SANITY] {symbol}: exit_price corrected to implied "
                f"${current_price:.4f} (from capped {pnl_pct:+.2f}%)"
            )

        trade_won = pnl > 0

        if asset_class == "stock" and settlement_tracker:
            try:
                settlement_tracker.record_trade_closed(
                    trade_id       = trade["trade_id"],
                    symbol         = symbol,
                    position_value = trade.get("position_value", 0),
                    asset_class    = asset_class   # crypto excluded automatically
                )
            except Exception as e:
                logger.debug(f"Settlement record error: {e}")

        self._db.close_trade(
            trade_id   = trade["trade_id"],
            exit_price = current_price,
            exit_reason= exit_reason,
            pnl        = round(pnl, 4),
            pnl_pct    = round(pnl_pct, 4),
            fees_paid  = fees_paid,
        )

        full_trade = {
            **trade,
            "exit_time":  datetime.now().isoformat(),
            "exit_price": current_price,
            "fees_paid":  fees_paid,   # Opus audit 2026-05-29: was missing → tax ledger showed fees=0
            "status":     "closed"
        }
        self._db.record_tax_event(full_trade)
        self._risk_manager.record_trade_result(pnl, trade_won, symbol=symbol)

        strategy_name = trade.get("strategy_name", "original")
        if strategy_name not in ("original", "manual") and exit_reason != "manual_close":
            try:
                from strategies.strategy_engine import strategy_engine
                strategy_engine.record_trade_result(
                    strategy_name, pnl, trade_won, exit_reason=exit_reason
                )
            except Exception as e:
                logger.debug(f"Strategy result recording error: {e}")

        if strategy_name == "scalp_master" and exit_reason != "manual_close":
            try:
                from strategies.scalp_master import log_trade_result
                log_trade_result(symbol, round(pnl_pct, 4), trade_won)
            except Exception as e:
                logger.debug(f"Scalp adaptive params update error: {e}")

        cap = self._db.get_latest_capital()
        if cap:
            new_capital = cap["total_capital"] + pnl
            self._db.log_capital(
                total     = new_capital,
                available = cap["available_cash"] + trade["position_value"] + pnl,
                invested  = max(0, cap["invested_value"] - trade["position_value"]),
                daily_pnl = cap["daily_pnl"] + pnl,
                total_pnl = cap["total_pnl"] + pnl,
                note      = (
                    f"Closed {symbol} @ {broker} {exit_reason}: "
                    f"{'+'if pnl>0 else ''}{pnl:.2f}"
                )
            )

        # ── Restore broker available cash (return position value + PnL) ────
        try:
            from core.broker_manager import broker_manager
            close_broker = trade.get("broker", broker)
            bal = broker_manager.get_broker_balance(close_broker)
            returned      = trade.get("position_value", 0.0) + pnl
            new_invested  = max(0.0, bal.get("invested",  0.0) - trade.get("position_value", 0.0))
            new_available = bal.get("available", 0.0) + returned
            new_balance   = bal.get("balance",  0.0) + pnl
            broker_manager.update_broker_balance(
                close_broker, new_balance, new_invested, new_available
            )
        except Exception as _bm_err:
            logger.debug(f"Broker balance update on close failed: {_bm_err}")

        result = "WIN" if trade_won else "LOSS"
        try:
            alert_manager.trade_closed({
                "symbol": symbol, "direction": direction,
                "entry_price": entry_price, "exit_price": current_price,
                "pnl": round(pnl, 4), "pnl_pct": round(pnl_pct, 4),
                "exit_reason": exit_reason,
                "strategy_name": trade.get("strategy_name", "unknown")
            })
        except Exception:
            pass

        logger.info(
            f"[TRADE CLOSED] [{result}] {symbol} | {broker} | {exit_reason} | "
            f"PnL: {'+'if pnl>0 else ''}{pnl:.4f} ({pnl_pct:+.2f}%)"
        )
        return True

    def close_all_stock_positions(self):
        """Close all open stock positions — called at end of day."""
        self.alpaca.close_all_stock_positions()

    def _submit(
        self,
        signal,
        approval: TradeApproval,
        broker_name_hint: Optional[str] = None,
    ) -> Optional[Dict]:
        side   = "buy" if signal.direction == "long" else "sell"

        if signal.asset_class == "stock":
            use_ibkr = broker_name_hint != "alpaca"
            if use_ibkr and self._ibkr is not None:
                if self._ibkr.is_available():
                    logger.info(f"Routing {signal.symbol} to IBKR (cash account)")
                    result = self._ibkr.submit_order(
                        signal.symbol, approval.quantity, side,
                        approval.stop_loss, approval.take_profit
                    )
                    if result is not None:
                        result["actual_broker"] = "ibkr"   # record which broker filled
                        return result
                    logger.warning(
                        f"IBKR order failed for {signal.symbol} — "
                        f"falling back to Alpaca"
                    )
                else:
                    logger.warning(
                        f"IBKR unreachable (TWS not running?) — "
                        f"falling back to Alpaca for {signal.symbol}"
                    )
            elif broker_name_hint == "alpaca":
                logger.info(f"Routing {signal.symbol} to Alpaca (IBKR cash unavailable)")
            result = self.alpaca.submit_order(
                signal.symbol, approval.quantity, side,
                approval.stop_loss, approval.take_profit
            )
            if result is not None:
                result["actual_broker"] = "alpaca"         # record which broker filled
            return result

        elif signal.asset_class == "crypto":
            # Kraken is the default crypto executor — Coinbase API is deprecated.
            if not config.KRAKEN_ENABLED:
                return self._paper_fill(signal, approval)

            qty      = approval.quantity
            leverage = 0   # 0 = spot (no margin params sent)

            # ── Margin leverage for short orders ─────────────────────────────
            # Kraken allows up to KRAKEN_MAX_LEVERAGE× on eligible pairs.
            # We cap at KRAKEN_SHORT_LEVERAGE and scale the position down so
            # our effective notional exposure = (desired/max) * normal_notional.
            # Example at defaults (desired=2, max=10):
            #   scale = 2/10 = 0.20  →  order qty = 20% of normal
            #   At 2× leverage, margin required = qty/2 = 10% of normal capital
            if signal.direction == "short":
                desired_lev = getattr(config, "KRAKEN_SHORT_LEVERAGE", 2)
                max_lev     = exchange_capabilities.get_max_short_leverage(
                    signal.symbol, "kraken"
                )
                # Cap desired to whatever the pair actually supports
                actual_lev  = min(desired_lev, max_lev) if max_lev > 1 else 0
                if actual_lev > 1:
                    ref_max = getattr(config, "KRAKEN_MAX_LEVERAGE", 10)
                    scale   = actual_lev / ref_max
                    qty     = round(approval.quantity * scale, 8)
                    leverage = actual_lev
                    logger.info(
                        f"[LEVERAGE] {signal.symbol} short: "
                        f"{actual_lev}×/{ref_max}× → scale {scale:.2f} "
                        f"qty {approval.quantity:.6f}→{qty:.6f} "
                        f"(margin≈${qty/actual_lev * signal.current_price:.2f})"
                    )

            return self.kraken.submit_order(
                signal.symbol, qty, side,
                approval.stop_loss, approval.take_profit,
                leverage=leverage
            )

    def _paper_fill(self, signal, approval: TradeApproval) -> Dict:
        logger.info(
            f"[PAPER] Simulated fill: {signal.symbol} {signal.direction.upper()} "
            f"@ ${signal.current_price:.4f} x {approval.quantity:.6f}"
        )
        return {
            "broker_order_id":  f"PAPER-{signal.symbol}-{int(signal.current_price)}",
            "status":           "filled",
            "filled_qty":       approval.quantity,
            "filled_avg_price": signal.current_price,
        }

    def _get_broker_name(self, signal) -> str:
        exchange = signal.indicators.get("exchange", "").lower()
        if signal.asset_class == "stock":
            if self._ibkr is not None and self._ibkr.is_available():
                return "ibkr"
            return "alpaca"
        if signal.asset_class == "crypto":
            return "kraken" if config.KRAKEN_ENABLED else "paper"

    def get_account_value(self) -> float:
        try:
            if self._ibkr is not None and self._ibkr.is_available():
                acct = self._ibkr.get_account()
                return acct["equity"]
            acct = self.alpaca.get_account()
            return acct["equity"]
        except Exception:
            cap = self._db.get_latest_capital()
            return cap["total_capital"] if cap else 0.0


# Singleton
executor = TradeExecutor()
