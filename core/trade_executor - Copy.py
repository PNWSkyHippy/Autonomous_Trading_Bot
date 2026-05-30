"""
=============================================================
  TRADE EXECUTOR
  Sends approved orders to Alpaca (stocks), Coinbase (crypto),
  or Kraken (crypto). Every order is logged before and after
  execution. Handles order confirmation, fills, partial fills.
=============================================================
"""

import uuid
import logging
from datetime import datetime
from typing import Optional, Dict

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
from data.database import db

logger = logging.getLogger(__name__)


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

        Alpaca silently rejects bracket orders for symbols with 1-2 character
        tickers (e.g. V, GE, ON). These orders appear 'submitted' on our side
        but never appear in the Alpaca account, causing ghost trades.

        Fix: detect short symbols and submit a plain market order instead.
        SL/TP for these trades will be managed by position_monitor.py,
        exactly the same way crypto SL/TP is handled.
        """
        # ── Short-symbol fallback (1-2 char tickers reject bracket orders) ──
        if len(symbol) <= 2:
            return self._submit_plain_market(symbol, qty, side, stop_loss, take_profit)

        # ── Normal bracket order ─────────────────────────────────────────────
        try:
            order = self.api.submit_order(
                symbol        = symbol,
                qty           = round(qty, 0) if qty >= 1 else qty,
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
            # If bracket order fails for any other reason, attempt plain market
            # order as a last-resort fallback so we don't silently drop the signal.
            logger.warning(
                f"Falling back to plain market order for {symbol} "
                f"(bracket rejected — SL/TP will be managed by position monitor)"
            )
            return self._submit_plain_market(symbol, qty, side, stop_loss, take_profit)

    def _submit_plain_market(self, symbol: str, qty: float, side: str,
                            stop_loss: float, take_profit: float) -> Optional[Dict]:
        """
        Submit a plain market order (no bracket) and flag the result so the
        trade record notes that SL/TP is managed by position_monitor.py.

        Used for:
            - 1-2 character symbols (Alpaca bracket limitation)
            - Fallback when a bracket order is rejected for any other reason
        """

        try:
            # ============================================================
            # PATCH 1 START — ORDER SUBMISSION ENHANCEMENT LAYER
            # ------------------------------------------------------------
            # NOTE TO LOCAL TEAM:
            # This block ensures consistent qty normalization and future
            # hook for broker-side routing logic (slippage handling, etc.)
            # ============================================================

            normalized_qty = round(qty, 0) if qty >= 1 else qty

            order = self.api.submit_order(
                symbol        = symbol,
                qty           = normalized_qty,
                side          = side,
                type          = "market",
                time_in_force = "day"
            )

            # ============================================================
            # PATCH 1 END
            # ============================================================


            logger.info(
                f"Alpaca plain market order submitted: {symbol} {side} "
                f"{qty} shares | SL/TP managed by position monitor | "
                f"SL={stop_loss:.4f} TP={take_profit:.4f} | Order ID: {order.id}"
            )

            # ============================================================
            # PATCH 2 START — POST-FILL RESPONSE ENHANCEMENT
            # ------------------------------------------------------------
            # NOTE TO LOCAL TEAM:
            # This ensures downstream systems (dashboard, monitor, DB)
            # correctly identify this as non-bracket-managed trade.
            # ============================================================

            result = {
                "broker_order_id":  order.id,
                "status":           order.status,
                "filled_qty":       float(order.filled_qty or 0),
                "filled_avg_price": float(order.filled_avg_price or 0),
                "manual_sl_tp":     True,   # signals position_monitor handles exits
                "sl":               stop_loss,
                "tp":               take_profit
            }

            return result

            # ============================================================
            # PATCH 2 END
            # ============================================================

        except Exception as e:
            logger.error(f"Alpaca plain market order also failed for {symbol}: {e}")
            return None
 

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
        """Close all open stock positions — called at end of day."""
        try:
            positions = self.api.list_positions()
            for pos in positions:
                self.close_position(pos.symbol)
            logger.info(f"Closed {len(positions)} stock positions at end of day.")
        except Exception as e:
            logger.error(f"Error closing all stock positions: {e}")


class CoinbaseExecutor:
    """Executes crypto trades via Coinbase Advanced Trade."""

    def __init__(self):
        self.exchange = ccxt.coinbase({
            "apiKey":          config.COINBASE_API_KEY,
            "secret":          config.COINBASE_SECRET_KEY,
            "enableRateLimit": True,
        })

    def submit_order(self, symbol: str, qty: float, side: str,
                     stop_loss: float, take_profit: float) -> Optional[Dict]:
        try:
            order = self.exchange.create_market_order(symbol, side, qty)
            filled_price = float(
                order.get("average") or order.get("price") or 0
            )
            filled_qty = float(order.get("filled") or qty)
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
    and logging. Supports Alpaca (stocks), Coinbase (crypto), Kraken (crypto).
    """

    def __init__(self, db_ref=None, risk_manager_ref=None, broker_manager_ref=None):
        # Accept injected dependencies or use singletons
        self._db           = db_ref or db
        self._risk_manager = risk_manager_ref or risk_manager

        self.alpaca   = AlpacaExecutor()
        self.coinbase = CoinbaseExecutor()
        self.kraken   = KrakenExecutor(
            api_key    = config.KRAKEN_API_KEY,
            api_secret = config.KRAKEN_SECRET_KEY,
            paper      = config.KRAKEN_PAPER_MODE
        )

    def execute_signal(self, signal) -> Optional[str]:
        """
        Full pipeline: approve -> execute -> log -> return trade_id.
        Accepts a Signal object from market_scanner.
        """
        logger.info(
            f"Attempting to execute signal: {signal.symbol} {signal.direction}"
        )

        # Pull custom SL/TP/position from strategy if provided
        custom_sl  = signal.indicators.get("custom_stop_loss_pct")
        custom_tp  = signal.indicators.get("custom_take_profit_pct")
        custom_pos = signal.indicators.get("custom_position_pct")

        # Risk approval
        approval = self._risk_manager.approve_trade(
            symbol                 = signal.symbol,
            entry_price            = signal.current_price,
            signal_score           = signal.score,
            direction              = signal.direction,
            custom_stop_loss_pct   = custom_sl,
            custom_take_profit_pct = custom_tp,
            custom_position_pct    = custom_pos
        )
        if not approval.approved:
            logger.info(
                f"Trade rejected for {signal.symbol}: {approval.reason}"
            )
            return None

        # Execute order
        trade_id     = str(uuid.uuid4())[:12]
        broker_result= self._submit(signal, approval)
        if not broker_result:
            logger.error(f"Order submission failed for {signal.symbol}")
            return None

        # Use actual fill price if available
        entry_price = broker_result.get("filled_avg_price") or signal.current_price
        if entry_price == 0:
            entry_price = signal.current_price

        # Recalculate SL/TP at actual fill price
        sl_pct = (custom_sl  or config.DEFAULT_STOP_LOSS_PCT)   / 100
        tp_pct = (custom_tp  or config.DEFAULT_TAKE_PROFIT_PCT) / 100

        if signal.direction == "long":
            stop_loss   = entry_price * (1 - sl_pct)
            take_profit = entry_price * (1 + tp_pct)
        else:
            stop_loss   = entry_price * (1 + sl_pct)
            take_profit = entry_price * (1 - tp_pct)

        broker_name = self._get_broker_name(signal)

        # Note in log if this trade uses manual SL/TP tracking
        if broker_result.get("manual_sl_tp"):
            logger.info(
                f"[SHORT TICKER] {signal.symbol} — bracket order not supported. "
                f"SL=${stop_loss:.4f} TP=${take_profit:.4f} tracked by position monitor."
            )

        # Log trade to database
        trade_record = {
            "trade_id":        trade_id,
            "symbol":          signal.symbol,
            "asset_class":     signal.asset_class,
            "direction":       signal.direction,
            "entry_time":      datetime.utcnow().isoformat(),
            "entry_price":     round(entry_price, 6),
            "quantity":        approval.quantity,
            "strategy_name":   signal.indicators.get("strategy_name", "original"),
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
            f"Entry: ${entry_price:.4f} | Qty: {approval.quantity:.4f} | "
            f"SL: ${stop_loss:.4f} | TP: ${take_profit:.4f} | "
            f"Broker: {broker_name} | ID: {trade_id}"
        )
        return trade_id

    def close_trade(self, trade: Dict, current_price: float,
                    exit_reason: str) -> bool:
        """Close an open position. Updates database and notifies risk manager."""
        symbol      = trade["symbol"]
        direction   = trade["direction"]
        qty         = trade["quantity"]
        entry_price = trade["entry_price"]
        asset_class = trade["asset_class"]
        broker      = trade.get("broker", "alpaca")

        # Execute the close
        success = False
        if asset_class == "stock":
            success = self.alpaca.close_position(symbol)
        elif asset_class == "crypto" and broker == "kraken" and config.KRAKEN_ENABLED:
            side    = "buy" if direction == "long" else "sell"
            success = self.kraken.close_position(symbol, qty, side)
        elif asset_class == "crypto":
            # Try Coinbase — fall back to paper simulation if it fails
            side = "buy" if direction == "long" else "sell"
            try:
                result = self.coinbase.close_position(symbol, qty, side)
                success = result is not False and result is not None
            except Exception as e:
                logger.debug(f"Coinbase close failed for {symbol}: {e} — using paper simulation")
                success = False
            if not success:
                logger.info(f"[PAPER] Simulated close: {symbol} {side.upper()} @ ${current_price:.4f}")
                success = True

        if not success:
            logger.error(f"Failed to close position {symbol}")
            return False

        # Calculate P&L
        if direction == "long":
            pnl = (current_price - entry_price) * qty
        else:
            pnl = (entry_price - current_price) * qty

        pnl_pct   = (pnl / trade["position_value"]) * 100 if trade["position_value"] else 0
        trade_won = pnl > 0

        # Record T+2 settlement for stock trades
        if asset_class == "stock" and settlement_tracker:
            try:
                settlement_tracker.record_trade_closed(
                    trade_id       = trade["trade_id"],
                    symbol         = symbol,
                    position_value = trade.get("position_value", 0)
                )
            except Exception as e:
                logger.debug(f"Settlement record error: {e}")

        # Update database
        self._db.close_trade(
            trade_id   = trade["trade_id"],
            exit_price = current_price,
            exit_reason= exit_reason,
            pnl        = round(pnl, 4),
            pnl_pct    = round(pnl_pct, 4)
        )

        # Record tax event
        full_trade = {
            **trade,
            "exit_time":  datetime.utcnow().isoformat(),
            "exit_price": current_price,
            "status":     "closed"
        }
        self._db.record_tax_event(full_trade)

        # Notify risk manager
        self._risk_manager.record_trade_result(pnl, trade_won)

       # Record strategy performance
        strategy_name = trade.get("strategy_name", "original")
        if strategy_name not in ("original", "manual") and exit_reason != "manual_close":
            try:
                from strategies.strategy_engine import strategy_engine
                strategy_engine.record_trade_result(strategy_name, pnl, trade_won)
            except Exception as e:
                logger.debug(f"Strategy result recording error: {e}")

        # Update scalp_master adaptive SL/TP params from actual trade outcome
        if strategy_name == "scalp_master" and exit_reason != "manual_close":
            try:
                from strategies.scalp_master import log_trade_result
                log_trade_result(symbol, round(pnl_pct, 4), trade_won)
            except Exception as e:
                logger.debug(f"Scalp adaptive params update error: {e}")
        # Update capital record
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

    def _submit(self, signal, approval: TradeApproval) -> Optional[Dict]:
        """Route order to the correct broker."""
        side   = "buy" if signal.direction == "long" else "sell"
        broker = signal.indicators.get("exchange", "").lower()

        if signal.asset_class == "stock":
            return self.alpaca.submit_order(
                signal.symbol, approval.quantity, side,
                approval.stop_loss, approval.take_profit
            )
        elif signal.asset_class == "crypto" and "kraken" in broker:
            if not config.KRAKEN_ENABLED:
                logger.info(
                    f"Kraken disabled — routing {signal.symbol} to paper simulation"
                )
                return self._paper_fill(signal, approval)
            return self.kraken.submit_order(
                signal.symbol, approval.quantity, side,
                approval.stop_loss, approval.take_profit
            )
        else:
            # Try Coinbase — fall back to paper simulation if unauthorized
            result = self.coinbase.submit_order(
                signal.symbol, approval.quantity, side,
                approval.stop_loss, approval.take_profit
            )
            if result is None:
                logger.info(
                    f"Coinbase unavailable — using paper simulation for {signal.symbol}"
                )
                return self._paper_fill(signal, approval)
            return result

    def _paper_fill(self, signal, approval: TradeApproval) -> Dict:
        """
        Simulate a paper trade fill locally.
        Used when broker APIs are unavailable or disabled.
        Records the trade at current market price with no slippage.
        """
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
        """Determine which broker name to record for this trade."""
        exchange = signal.indicators.get("exchange", "").lower()
        if "kraken" in exchange and config.KRAKEN_ENABLED:
            return "kraken"
        if signal.asset_class == "stock":
            return "alpaca"
        if "kraken" in exchange and not config.KRAKEN_ENABLED:
            return "paper"
        return "coinbase"

    def get_account_value(self) -> float:
        """Get total account equity from Alpaca."""
        try:
            acct = self.alpaca.get_account()
            return acct["equity"]
        except Exception:
            cap = self._db.get_latest_capital()
            return cap["total_capital"] if cap else 0.0


# Singleton
executor = TradeExecutor()
