"""
=============================================================
  IBKR EXECUTOR
  Executes stock trades via Interactive Brokers using ib_insync.

  IBKR Cash Account advantages:
    - No PDT rule (trading with settled funds only)
    - No $25K minimum
    - Full API access via TWS or IB Gateway
    - T+1 settlement (SEC standard since 2024)

  Setup:
    1. Install: pip install ib_insync --break-system-packages
    2. Run TWS or IB Gateway on localhost
    3. Enable API access in TWS: File -> Global Config -> API -> Settings
       Check 'Enable ActiveX and Socket Clients'
       UNCHECK 'Read-Only API'
       Set port to 7497 (paper) or 7496 (live)
    4. Add IBKR_ACCOUNT to .env
    5. Set IBKR_ENABLED=True in config.py when ready

  Connection model:
    Persistent connection maintained all session. is_available() simply
    checks isConnected() without reconnecting. _ensure_connected() is
    called before every order and reconnects if dropped with backoff.
    This avoids the clientId conflict that caused 'unreachable' warnings
    when connect/disconnect/reconnect happened on every trade.

  ClientId:
    IBKR_CLIENT_ID = 2. If you see 'clientId already in use' change this
    to any unused number (3, 4, etc). Each connection to TWS needs a
    unique clientId. ClientId 1 is often used by TWS itself or other tools.
=============================================================
"""

import asyncio
import logging
import time
from typing import Optional, Dict

import config

logger = logging.getLogger(__name__)

IBKR_PAPER_PORT = 7497
IBKR_LIVE_PORT  = 7496
IBKR_HOST       = "127.0.0.1"
IBKR_CLIENT_ID  = 2      # Changed from 1 — avoids conflict with TWS own clientId
MAX_RECONNECT_ATTEMPTS = 3
CLIENT_ID_FALLBACKS = [2, 3, 4, 5]  # Try these in order if primary clientId is in use


class IBKRExecutor:
    """
    Executes stock trades via Interactive Brokers TWS/Gateway API.
    Maintains a persistent connection all session to avoid clientId conflicts.
    """

    def __init__(self):
        self.account    = getattr(config, "IBKR_ACCOUNT", "")
        self.paper_mode = getattr(config, "IBKR_PAPER_MODE", True)
        self.port       = IBKR_PAPER_PORT if self.paper_mode else IBKR_LIVE_PORT
        self._ib        = None
        self._connected = False
        # Establish connection immediately at startup
        self._establish_connection()

    @staticmethod
    def _ensure_event_loop():
        """
        ib_insync uses asyncio internally. The bot's scanner spawns many
        background threads (Thread-N _do_stock_scan) that have no asyncio
        event loop. Calling any ib method from those threads raises
        'There is no current event loop in thread'.
        Fix: create a fresh event loop for the calling thread if none exists.
        Must be called at the top of every public method invoked from threads.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("loop is closed")
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    def _establish_connection(self) -> bool:
        """
        Establish persistent connection to TWS.
        Tries multiple clientIds in sequence if the primary one is still
        held by a previous bot instance (common after quick restarts).
        TWS holds clientIds for ~30-60s after disconnect.
        Returns True if connected successfully.
        """
        self._ensure_event_loop()
        from ib_insync import IB

        for client_id in CLIENT_ID_FALLBACKS:
            ib = IB()
            # Track if TWS sent error 326 (clientId in use) via error callback
            client_id_rejected = [False]

            def on_error(reqId, errorCode, errorString, contract):
                if errorCode == 326:
                    client_id_rejected[0] = True

            ib.errorEvent += on_error

            try:
                ib.connect(
                    host     = IBKR_HOST,
                    port     = self.port,
                    clientId = client_id,
                    timeout  = 5,
                    readonly = False
                )
                # Give TWS a moment to send back error 326 if clientId is in use
                ib.sleep(1.0)

                if client_id_rejected[0]:
                    # TWS accepted socket but rejected clientId
                    logger.warning(
                        f"IBKR clientId {client_id} in use — trying next..."
                    )
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    continue

                if ib.isConnected():
                    self._ib        = ib
                    self._connected = True
                    self._client_id = client_id
                    logger.info(
                        f"IBKR connected: port={self.port} "
                        f"({'paper' if self.paper_mode else 'LIVE'}) "
                        f"account={self.account or 'default'} "
                        f"clientId={client_id}"
                        + (f" (fallback — primary clientId {IBKR_CLIENT_ID} was in use)"
                           if client_id != IBKR_CLIENT_ID else "")
                    )
                    return True

            except Exception as e:
                logger.warning(f"IBKR clientId {client_id} connect error: {e}")
                try:
                    ib.disconnect()
                except Exception:
                    pass
                continue

        logger.warning(
            f"IBKR: all clientIds {CLIENT_ID_FALLBACKS} in use or failed. "
            f"TWS may need 30-60s to release previous connections after restart."
        )
        self._connected = False
        return False

    def _ensure_connected(self) -> bool:
        """
        Check connection and reconnect if dropped.
        Uses backoff to avoid hammering TWS.
        Returns True if connected and ready.
        """
        if self._ib and self._ib.isConnected():
            return True

        logger.warning("IBKR connection lost — attempting reconnect...")
        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            time.sleep(attempt * 1.0)  # 1s, 2s, 3s backoff
            if self._establish_connection():
                logger.info(f"IBKR reconnected on attempt {attempt}")
                return True
            logger.warning(f"IBKR reconnect attempt {attempt}/{MAX_RECONNECT_ATTEMPTS} failed")

        self._connected = False
        return False

    def is_available(self) -> bool:
        """
        Quick connectivity check — no connect/disconnect.
        Just checks if the persistent connection is alive.
        If not connected, tries one reconnect.
        """
        if self._ib and self._ib.isConnected():
            return True
        return self._establish_connection()

    def submit_order(self, symbol: str, qty: float, side: str,
                     stop_loss: float, take_profit: float) -> Optional[Dict]:
        """
        Submit a bracket order to IBKR.
        Uses persistent connection — no connect/disconnect cycle.
        """
        self._ensure_event_loop()
        if not self._ensure_connected():
            logger.error(f"IBKR submit_order: cannot connect for {symbol}")
            return None

        try:
            from ib_insync import Stock, MarketOrder, StopOrder, LimitOrder

            ib     = self._ib
            shares = max(1, int(round(qty)))
            action = "BUY" if side == "buy" else "SELL"

            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            parent = MarketOrder(
                action        = action,
                totalQuantity = shares,
                transmit      = False,
                orderId       = ib.client.getReqId()
            )

            sl_action = "SELL" if action == "BUY" else "BUY"
            stop = StopOrder(
                action        = sl_action,
                totalQuantity = shares,
                stopPrice     = round(stop_loss, 2),
                parentId      = parent.orderId,
                transmit      = False,
                orderId       = ib.client.getReqId()
            )

            tp = LimitOrder(
                action        = sl_action,
                totalQuantity = shares,
                lmtPrice      = round(take_profit, 2),
                parentId      = parent.orderId,
                transmit      = True,
                orderId       = ib.client.getReqId()
            )

            parent_trade = ib.placeOrder(contract, parent)
            ib.placeOrder(contract, stop)
            ib.placeOrder(contract, tp)

            # Wait for fill (up to 10 seconds)
            for _ in range(20):
                ib.sleep(0.5)
                if parent_trade.orderStatus.status in ("Filled", "PartiallyFilled"):
                    break

            status       = parent_trade.orderStatus.status
            filled_qty   = parent_trade.orderStatus.filled or 0
            filled_price = parent_trade.orderStatus.avgFillPrice or 0.0

            logger.info(
                f"IBKR bracket order: {symbol} {action} {shares} shares | "
                f"Status={status} | Fill=${filled_price:.4f} | "
                f"SL={stop_loss:.4f} TP={take_profit:.4f} | "
                f"OrderID={parent.orderId}"
            )

            return {
                "broker_order_id":  str(parent.orderId),
                "status":           status.lower(),
                "filled_qty":       float(filled_qty),
                "filled_avg_price": float(filled_price),
                "manual_sl_tp":     False
            }

        except Exception as e:
            logger.error(f"IBKR order failed for {symbol}: {e}")
            return None

    def close_position(self, symbol: str) -> bool:
        """Close an open IBKR position via market order."""
        self._ensure_event_loop()
        if not self._ensure_connected():
            logger.error(f"IBKR close_position: cannot connect for {symbol}")
            return False

        try:
            from ib_insync import Stock, MarketOrder

            ib        = self._ib
            positions = ib.positions(account=self.account or "")

            # If no positions found with account filter, try without filter
            # (paper accounts sometimes return positions without account qualifier)
            if not positions:
                positions = ib.positions()

            target    = None

            for pos in positions:
                if pos.contract.symbol == symbol:
                    target = pos
                    break

            if not target:
                logger.warning(f"IBKR close: no position found for {symbol}")
                return False

            qty    = abs(target.position)
            action = "SELL" if target.position > 0 else "BUY"

            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            order = MarketOrder(action=action, totalQuantity=qty, transmit=True)
            trade = ib.placeOrder(contract, order)

            for _ in range(20):
                ib.sleep(0.5)
                if trade.orderStatus.status in ("Filled", "PartiallyFilled"):
                    break

            logger.info(
                f"IBKR position closed: {symbol} {action} {qty} shares | "
                f"Status={trade.orderStatus.status}"
            )
            return True

        except Exception as e:
            logger.error(f"IBKR close_position failed for {symbol}: {e}")
            return False

    def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol."""
        self._ensure_event_loop()
        if not self._ensure_connected():
            return 0

        try:
            ib          = self._ib
            open_orders = ib.openOrders()
            cancelled   = 0

            for order in open_orders:
                try:
                    ib.cancelOrder(order)
                    cancelled += 1
                except Exception:
                    pass

            logger.info(f"IBKR cancelled {cancelled} open orders for {symbol}")
            return cancelled

        except Exception as e:
            logger.error(f"IBKR cancel_all_orders failed for {symbol}: {e}")
            return 0

    def get_account(self) -> Dict:
        """Fetch account summary from IBKR."""
        self._ensure_event_loop()
        if not self._ensure_connected():
            return {"equity": 0, "cash": 0, "buying_power": 0,
                    "portfolio_value": 0, "settled_cash": 0}

        try:
            ib      = self._ib
            summary = ib.accountSummary(account=self.account or "")
            values  = {item.tag: item.value for item in summary}

            equity       = float(values.get("NetLiquidation", 0))
            cash         = float(values.get("CashBalance",    0))
            buying_power = float(values.get("BuyingPower",    0))
            settled_cash = float(values.get("SettledCash",    0))

            logger.info(
                f"IBKR account: equity=${equity:,.2f} | "
                f"cash=${cash:,.2f} | settled=${settled_cash:,.2f}"
            )

            return {
                "equity":          equity,
                "cash":            cash,
                "buying_power":    buying_power,
                "portfolio_value": equity,
                "settled_cash":    settled_cash
            }

        except Exception as e:
            logger.error(f"IBKR get_account failed: {e}")
            return {"equity": 0, "cash": 0, "buying_power": 0,
                    "portfolio_value": 0, "settled_cash": 0}

    def get_historical_bars(self, symbol: str, timeframe: str = '5m',
                            limit: int = 300, sec_type: str = 'STK',
                            exchange: str = 'SMART') -> list:
        """
        Return OHLCV bars from IBKR TWS as a list of dicts.
        Used by the chart panel — does NOT affect trading state.
        Returns [] if IBKR is unavailable.

        sec_type: 'STK' for stocks (default), 'FUT' for futures continuous contracts.
        exchange: IBKR exchange string — 'SMART' for stocks, CME/NYMEX/COMEX/CBOT for futures.
        """
        self._ensure_event_loop()
        if not self._ensure_connected():
            return []
        try:
            from ib_insync import Stock, ContFuture
            if sec_type == 'FUT':
                # ContFuture = continuous front-month contract — best for charting
                contract = ContFuture(symbol, exchange, 'USD')
            else:
                contract = Stock(symbol, 'SMART', 'USD')

            _bar_size = {
                '1m': '1 min',  '5m': '5 mins', '15m': '15 mins',
                '30m': '30 mins','1h': '1 hour', '4h': '4 hours',
                '1D': '1 day',   '1W': '1 week',
            }.get(timeframe, '5 mins')

            # Duration string: enough history to cover `limit` bars
            _minutes = {
                '1m': 1, '5m': 5, '15m': 15, '30m': 30,
                '1h': 60, '4h': 240, '1D': 1440, '1W': 10080,
            }.get(timeframe, 5)
            total_min = _minutes * limit
            if   total_min <= 1440:   dur = '2 D'
            elif total_min <= 7200:   dur = '10 D'
            elif total_min <= 43200:  dur = '3 M'
            else:                     dur = '1 Y'

            raw = self._ib.reqHistoricalData(
                contract, endDateTime='', durationStr=dur,
                barSizeSetting=_bar_size, whatToShow='TRADES',
                useRTH=False, formatDate=2, keepUpToDate=False, timeout=30,
            )
            if not raw:
                return []
            bars = [
                {
                    'time':   int(b.date),
                    'open':   float(b.open),
                    'high':   float(b.high),
                    'low':    float(b.low),
                    'close':  float(b.close),
                    'volume': float(b.volume) if b.volume >= 0 else 0,
                }
                for b in raw
            ]
            # trim to requested limit
            return bars[-limit:]
        except Exception as e:
            logger.warning(f"IBKR historical bars failed for {symbol}: {e}")
            return []

    def disconnect(self):
        """Graceful shutdown — called when bot stops."""
        try:
            if self._ib and self._ib.isConnected():
                self._ib.disconnect()
                logger.info("IBKR disconnected cleanly.")
        except Exception:
            pass
