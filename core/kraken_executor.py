"""
=============================================================
  KRAKEN EXECUTOR
  Executes crypto trades via Kraken using CCXT.

  PAPER MODE NOTE:
    Kraken does not support a sandbox/demo API endpoint.
    ccxt's sandbox=True flag has no effect on Kraken.
    Paper mode is implemented locally — when paper=True,
    all order submissions are simulated in memory and no
    real API calls are made for order entry/exit.
    Price lookups still use the real Kraken API.
=============================================================
"""

import logging
import time
from typing import Optional, Dict
import ccxt

logger = logging.getLogger(__name__)


class KrakenExecutor:

    def __init__(self, api_key: str = "", api_secret: str = "",
                 paper: bool = False):
        self.paper = paper

        # Always init the exchange for price lookups
        # timeout=10000ms — prevents API hangs from freezing the scan thread
        self.exchange = ccxt.kraken({
            "apiKey":          api_key,
            "secret":          api_secret,
            "enableRateLimit": True,
            "timeout":         10000,   # 10 second hard timeout on all API calls
        })

        if paper:
            logger.info("Kraken executor initialized in PAPER mode — orders simulated locally.")
        else:
            logger.info("Kraken executor initialized in LIVE mode.")

    def submit_order(self, symbol: str, qty: float, side: str,
                     stop_loss: float, take_profit: float,
                     leverage: int = 0) -> Optional[Dict]:
        """
        Submit a market order to Kraken.

        leverage:
          0 (default) — spot order, no margin params sent
          N > 1       — margin order at N× leverage; ccxt sends leverage in params
                        Required for short (sell) orders on Kraken; optional for
                        leveraged longs.

        qty is the notional amount in base currency AFTER any scaling the caller
        has already applied (trade_executor scales qty down for margin trades so
        that effective exposure = qty * leverage, not qty * max_leverage).
        """
        ccxt_symbol = symbol.replace("-", "/")
        lev_str     = f" {leverage}x" if leverage > 1 else ""

        if self.paper:
            # Paper mode: simulate fill at current market price
            price    = self.get_current_price(symbol) or 0.0
            order_id = f"PAPER-{symbol}-{int(time.time())}"
            logger.info(
                f"[PAPER] Kraken simulated fill: {symbol} {side}{lev_str} "
                f"{qty} @ ${price:.4f}"
            )
            return {
                "broker_order_id":  order_id,
                "status":           "filled",
                "filled_qty":       qty,
                "filled_avg_price": price,
            }

        # Live mode
        try:
            params = {}
            if leverage > 1:
                params["leverage"] = leverage

            order        = self.exchange.create_market_order(
                ccxt_symbol, side, qty, params=params
            )
            filled_price = float(order.get("average") or order.get("price") or 0)
            filled_qty   = float(order.get("filled") or qty)
            logger.info(
                f"Kraken order filled: {symbol} {side}{lev_str} "
                f"{filled_qty} @ ${filled_price:.4f}"
            )
            return {
                "broker_order_id":  order["id"],
                "status":           order["status"],
                "filled_qty":       filled_qty,
                "filled_avg_price": filled_price,
            }
        except Exception as e:
            logger.error(f"Kraken order failed for {symbol}: {e}")
            return None

    def close_position(self, symbol: str, qty: float, side: str) -> bool:
        ccxt_symbol = symbol.replace("-", "/")

        if self.paper:
            price = self.get_current_price(symbol) or 0.0
            close_side = "sell" if side == "buy" else "buy"
            logger.info(
                f"[PAPER] Kraken simulated close: {symbol} "
                f"{close_side} {qty} @ ${price:.4f}"
            )
            return True

        # Live mode
        try:
            close_side = "sell" if side == "buy" else "buy"
            self.exchange.create_market_order(ccxt_symbol, close_side, qty)
            logger.info(f"Kraken position closed: {symbol}")
            return True
        except Exception as e:
            logger.error(f"Failed to close Kraken {symbol}: {e}")
            return False

    def get_balance(self, currency: str = "USD") -> float:
        if self.paper:
            # Paper mode: return a nominal balance so capital tracking works
            return 0.0
        try:
            balance = self.exchange.fetch_balance()
            usd = float(balance.get(currency, {}).get("free", 0))
            if usd == 0 and currency == "USD":
                usd = float(balance.get("ZUSD", {}).get("free", 0))
            return usd
        except Exception as e:
            logger.debug(f"Kraken balance fetch error: {e}")
            return 0.0

    def get_open_position(self, symbol: str) -> Optional[Dict]:
        """
        Check if a position actually exists on Kraken exchange.
        Returns position dict with size and avg price, or None if no position.
        In paper mode always returns None (positions are DB-only).
        Used by position_monitor to detect broker-closed positions.
        """
        if self.paper:
            return None   # paper mode has no real positions

        try:
            # Kraken uses base currency as the position key (e.g. BTC for BTC/USD)
            base = symbol.split("/")[0]
            balance = self.exchange.fetch_balance()
            free  = float(balance.get(base, {}).get("free",  0) or 0)
            used  = float(balance.get(base, {}).get("used",  0) or 0)
            total = free + used
            if total > 0.0001:   # position exists
                return {"symbol": symbol, "size": total, "free": free, "used": used}
            return None
        except Exception as e:
            logger.debug(f"get_open_position error for {symbol}: {e}")
            return None

    def get_recent_closed_order(self, symbol: str,
                                 since_timestamp: float = None) -> Optional[Dict]:
        """
        Query Kraken for the most recent closed order for a symbol.
        Used to find the actual fill price after an SL/TP fires at the exchange.
        Returns dict with fill price and side, or None if not found.
        """
        if self.paper:
            return None

        try:
            ccxt_symbol = symbol.replace("-", "/")
            since_ms    = int(since_timestamp * 1000) if since_timestamp else None
            orders      = self.exchange.fetch_closed_orders(
                ccxt_symbol, since=since_ms, limit=5
            )
            if not orders:
                return None
            # Most recent closed order
            orders.sort(key=lambda o: o.get("timestamp", 0), reverse=True)
            o = orders[0]
            return {
                "fill_price": float(o.get("average") or o.get("price") or 0),
                "filled_qty": float(o.get("filled") or 0),
                "side":       o.get("side", ""),
                "order_id":   o.get("id", ""),
                "timestamp":  o.get("timestamp", 0),
            }
        except Exception as e:
            logger.debug(f"get_recent_closed_order error for {symbol}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Return the current mid-price for *symbol* from Kraken.

        Defence against stale / garbage ticker prices
        ------------------------------------------------
        Some tokens (especially newly listed ones) have a `last` trade price
        that is days or weeks old and wildly different from the active market.
        We cross-check `last` against the live bid/ask mid-price:

        * If bid AND ask are present and sane (bid < ask, both > 0), use
          mid-price as the reference.
        * If `last` deviates from mid by more than 50 %, the ticker is stale
          — return mid instead (it reflects the live order book).
        * If bid/ask are missing (e.g. market closed or illiquid) fall back to
          `last` as before, so normal operation is unaffected.
        """
        try:
            ccxt_symbol = symbol.replace("-", "/")
            ticker = self.exchange.fetch_ticker(ccxt_symbol)
            last = float(ticker.get("last") or 0)
            bid  = float(ticker.get("bid")  or 0)
            ask  = float(ticker.get("ask")  or 0)

            # ── prefer mid-price when the order book is live ─────────────────
            if bid > 0 and ask > 0 and bid < ask:
                mid = (bid + ask) / 2.0
                if last > 0:
                    deviation = abs(last - mid) / mid
                    if deviation > 0.50:
                        logger.warning(
                            f"[TICKER SANITY] {symbol}: last=${last:.6f} deviates "
                            f"{deviation*100:.1f}% from mid=${mid:.6f} — "
                            f"using mid (stale last price suspected)"
                        )
                        return mid
                return mid          # bid/ask available — mid is more reliable

            # ── no live book — fall back to last ─────────────────────────────
            if last > 0:
                return last

            return None
        except Exception as e:
            logger.debug(f"Kraken price lookup failed for {symbol}: {e}")
            return None

    def place_stop_order(self, symbol: str, qty: float,
                         side: str, stop_price: float) -> Optional[str]:
        """
        Place a stop-market order on Kraken.
        side: 'buy' (to close a short) or 'sell' (to close a long)
        Returns order_id string or None on failure.
        Paper mode: returns a fake order ID for tracking.
        """
        ccxt_symbol = symbol.replace("-", "/")
        if self.paper:
            fake_id = f"PAPER-STOP-{symbol}-{int(stop_price*10000)}"
            logger.info(
                f"[PAPER] Stop order placed: {symbol} {side} {qty} "
                f"@ ${stop_price:.4f} | id={fake_id}"
            )
            return fake_id
        try:
            order = self.exchange.create_order(
                symbol    = ccxt_symbol,
                type      = "stop-market",
                side      = side,
                amount    = qty,
                params    = {"stopPrice": stop_price}
            )
            order_id = order["id"]
            logger.info(
                f"Stop order placed: {symbol} {side} {qty} "
                f"@ ${stop_price:.4f} | id={order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"Failed to place stop order for {symbol}: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order by ID. Paper mode: always succeeds."""
        if self.paper:
            logger.info(f"[PAPER] Order cancelled: {order_id}")
            return True
        ccxt_symbol = symbol.replace("-", "/")
        try:
            self.exchange.cancel_order(order_id, ccxt_symbol)
            logger.info(f"Order cancelled: {order_id} for {symbol}")
            return True
        except Exception as e:
            logger.warning(f"Cancel order failed {order_id} for {symbol}: {e}")
            return False

    def update_stop_order(self, symbol: str, old_order_id: str,
                          qty: float, side: str,
                          new_stop_price: float) -> Optional[str]:
        """
        Move a stop order to a new price.
        Cancel old order → place new one → return new order_id.
        Returns new order_id or None on failure.
        """
        self.cancel_order(symbol, old_order_id)
        return self.place_stop_order(symbol, qty, side, new_stop_price)

    def get_trading_pairs(self) -> list:
        try:
            markets = self.exchange.load_markets()
            return [m for m in markets.keys() if "/USD" in m]
        except Exception as e:
            logger.debug(f"Failed to load Kraken markets: {e}")
            return []
