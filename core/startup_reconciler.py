"""
=============================================================
  STARTUP RECONCILER
  Runs once at bot startup to reconcile DB state vs broker.

  Problems it fixes:
  1. Ghost open trades — trades the DB thinks are open but
     the broker already closed (stop-loss hit, TP hit, or
     closed manually while bot was offline). These cause
     capital calculations to be wrong after restart.

  2. Capital drift — after reconciling ghost trades, recalculates
     true capital from all closed trades so the dashboard
     shows accurate numbers immediately on startup.

  How it works:
  - Fetches all open trades from DB
  - For stocks: checks Alpaca for current position
  - For crypto: checks current price vs SL/TP via Kraken price feed
  - If position not found at broker: marks it closed at last
    known price with reason 'reconciled_on_startup'
  - Logs every action clearly so you can see what happened

  Called automatically from bot_engine.py on startup.
=============================================================
"""

import logging
from datetime import datetime
from typing import List, Dict, Optional

import config
from data.database import db

logger = logging.getLogger(__name__)


def reconcile_on_startup():
    """
    Main entry point. Call this once at bot startup before
    any scanning begins.
    """
    logger.info("[RECONCILE] Starting startup reconciliation...")

    open_trades = db.get_open_trades()
    if not open_trades:
        logger.info("[RECONCILE] No open trades in DB — nothing to reconcile.")
        return

    logger.info(f"[RECONCILE] Found {len(open_trades)} open trade(s) in DB to verify.")

    stock_trades  = [t for t in open_trades if t["asset_class"] == "stock"]
    crypto_trades = [t for t in open_trades if t["asset_class"] == "crypto"]

    reconciled = 0

    # ---- Stocks: check against Alpaca ----
    if stock_trades:
        reconciled += _reconcile_stocks(stock_trades)

    # ---- Crypto: check price vs SL/TP ----
    if crypto_trades:
        reconciled += _reconcile_crypto(crypto_trades)

    if reconciled > 0:
        logger.info(
            f"[RECONCILE] Closed {reconciled} ghost trade(s). "
            f"Capital will now calculate correctly."
        )
    else:
        logger.info("[RECONCILE] All open trades verified — no ghosts found.")

    logger.info("[RECONCILE] Startup reconciliation complete.")


def _reconcile_stocks(trades: List[Dict]) -> int:
    """Check each open stock trade against Alpaca positions."""
    closed = 0
    try:
        import alpaca_trade_api as tradeapi
        api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL
        )
        # Get all current Alpaca positions
        try:
            positions = api.list_positions()
            open_symbols = {p.symbol for p in positions}
        except Exception as e:
            logger.warning(f"[RECONCILE] Could not fetch Alpaca positions: {e}")
            return 0

        for trade in trades:
            symbol = trade["symbol"]
            broker = trade.get("broker", "alpaca")

            # Only reconcile alpaca trades — skip IBKR and paper
            if broker not in ("alpaca", ""):
                continue

            if symbol not in open_symbols:
                # Position not found at broker — ghost trade
                # Use entry price as best estimate (we don't know exit price)
                exit_price = trade["entry_price"]
                pnl        = 0.0
                pnl_pct    = 0.0

                # Try to get last quote for a better exit price
                try:
                    quote = api.get_latest_trade(symbol)
                    if quote and quote.price:
                        exit_price = float(quote.price)
                        qty = trade["quantity"]
                        direction = trade["direction"]
                        pnl = (exit_price - trade["entry_price"]) * qty
                        if direction == "short":
                            pnl = -pnl
                        pnl_pct = (pnl / trade["position_value"] * 100) \
                                  if trade["position_value"] else 0
                except Exception:
                    pass

                db.close_trade(
                    trade_id    = trade["trade_id"],
                    exit_price  = round(exit_price, 6),
                    exit_reason = "reconciled_on_startup",
                    pnl         = round(pnl, 4),
                    pnl_pct     = round(pnl_pct, 4)
                )
                logger.info(
                    f"[RECONCILE] Ghost stock trade closed: {symbol} "
                    f"| Entry: ${trade['entry_price']:.4f} "
                    f"| Exit: ${exit_price:.4f} "
                    f"| PnL: ${pnl:+.2f} "
                    f"| trade_id: {trade['trade_id']}"
                )
                closed += 1
            else:
                logger.info(f"[RECONCILE] Stock position verified open at Alpaca: {symbol}")

    except ImportError:
        logger.warning("[RECONCILE] alpaca_trade_api not installed — skipping stock reconciliation")
    except Exception as e:
        logger.error(f"[RECONCILE] Stock reconciliation error: {e}")

    return closed


def _reconcile_crypto(trades: List[Dict]) -> int:
    """
    Reconcile open crypto positions against Kraken.

    Two-stage check per trade:

    Stage 1 — position existence (live Kraken only):
        Uses KrakenExecutor.get_open_position() to ask whether the base-currency
        balance still shows a position on the exchange. If no balance remains the
        position was closed by the exchange while the bot was offline (SL/TP hit,
        or manual close), and we mark the trade closed in the DB.

    Stage 2 — price vs SL/TP (paper trades and live fallback):
        Fetches the current market price and compares it against the recorded
        stop-loss and take-profit levels. If either was passed we close the trade.
        This is the correct approach for paper trades (no real position exists)
        and a reliable fallback when stage 1 is unavailable.

    Only Kraken / paper-mode crypto trades are reconciled — any lingering DB
    records with a non-Kraken broker tag are skipped.
    """
    closed = 0

    # Only reconcile Kraken / paper-mode crypto trades
    kraken_trades = [
        t for t in trades
        if t.get("broker", "kraken") in ("kraken", "paper", "")
    ]
    if not kraken_trades:
        logger.info("[RECONCILE] Crypto: no Kraken/paper trades to reconcile.")
        return 0

    try:
        import ccxt
        from core.kraken_executor import KrakenExecutor

        exchange = ccxt.kraken({
            "apiKey":          config.KRAKEN_API_KEY,
            "secret":          config.KRAKEN_SECRET_KEY,
            "enableRateLimit": True,
        })

        # Live mode: create KrakenExecutor for real position checks
        is_live = (getattr(config, "KRAKEN_ENABLED",    False) and
                   not getattr(config, "KRAKEN_PAPER_MODE", True))
        kraken_exec = (
            KrakenExecutor(
                api_key    = config.KRAKEN_API_KEY,
                api_secret = config.KRAKEN_SECRET_KEY,
                paper      = False,
            )
            if is_live else None
        )

        logger.info(
            f"[RECONCILE] Crypto: verifying {len(kraken_trades)} position(s) — "
            f"{'live Kraken (position check + price check)' if is_live else 'paper (price check only)'}"
        )

        for trade in kraken_trades:
            symbol    = trade["symbol"]
            direction = trade["direction"]
            sl        = trade["stop_loss"]
            tp        = trade["take_profit"]
            entry     = trade["entry_price"]
            broker    = trade.get("broker", "kraken")

            # ── Stage 1: position existence check (live Kraken only) ─────────
            if is_live and kraken_exec is not None and broker == "kraken":
                position = kraken_exec.get_open_position(symbol)
                if position is None:
                    # Position gone at exchange — closed while bot was offline.
                    # Try to recover the actual fill from recent closed orders.
                    exit_price = entry   # safe fallback
                    try:
                        entry_ts    = _entry_timestamp(trade)
                        closed_ord  = kraken_exec.get_recent_closed_order(
                            symbol, since_timestamp=entry_ts
                        )
                        if closed_ord and closed_ord.get("fill_price", 0) > 0:
                            exit_price = closed_ord["fill_price"]
                            logger.info(
                                f"[RECONCILE] {symbol}: recovered fill "
                                f"${exit_price:.6f} from closed order "
                                f"{closed_ord.get('order_id', '?')}"
                            )
                        else:
                            # No closed order found — use current market price
                            try:
                                ticker     = exchange.fetch_ticker(
                                    symbol.replace("-", "/")
                                )
                                exit_price = float(ticker["last"])
                            except Exception:
                                pass
                    except Exception:
                        pass

                    qty     = trade["quantity"]
                    pnl     = ((exit_price - entry) * qty if direction == "long"
                               else (entry - exit_price) * qty)
                    pnl_pct = (pnl / trade["position_value"] * 100
                               if trade["position_value"] else 0)

                    db.close_trade(
                        trade_id    = trade["trade_id"],
                        exit_price  = round(exit_price, 6),
                        exit_reason = "reconciled_broker_closed",
                        pnl         = round(pnl, 4),
                        pnl_pct     = round(pnl_pct, 4),
                    )
                    logger.info(
                        f"[RECONCILE] Crypto ghost trade closed (position gone at Kraken): "
                        f"{symbol} | Entry: ${entry:.4f} | Exit: ${exit_price:.4f} "
                        f"| PnL: ${pnl:+.2f} | trade_id: {trade['trade_id']}"
                    )
                    closed += 1
                    continue   # skip stage 2 — we already closed it
                else:
                    logger.info(
                        f"[RECONCILE] Crypto position confirmed at Kraken: {symbol} "
                        f"| size={position['size']:.6f}"
                    )

            # ── Stage 2: price vs SL/TP ────────────────────────────────────
            price = None
            try:
                ticker = exchange.fetch_ticker(symbol.replace("-", "/"))
                price  = float(ticker["last"])
            except Exception:
                try:
                    import yfinance as yf
                    yf_sym = symbol.replace("/", "-")
                    t  = yf.Ticker(yf_sym)
                    h  = t.history(period="1d", interval="1m")
                    price = float(h["Close"].iloc[-1]) if not h.empty else None
                except Exception:
                    price = None

            if price is None:
                logger.warning(
                    f"[RECONCILE] Could not get price for {symbol} — skipping"
                )
                continue

            exit_reason = None
            exit_price  = price

            if direction == "long":
                if price <= sl:
                    exit_reason = "stop_loss"
                    exit_price  = sl
                elif price >= tp:
                    exit_reason = "take_profit"
                    exit_price  = tp
            else:
                if price >= sl:
                    exit_reason = "stop_loss"
                    exit_price  = sl
                elif price <= tp:
                    exit_reason = "take_profit"
                    exit_price  = tp

            if exit_reason:
                qty     = trade["quantity"]
                pnl     = ((exit_price - entry) * qty if direction == "long"
                           else (entry - exit_price) * qty)
                pnl_pct = (pnl / trade["position_value"] * 100
                           if trade["position_value"] else 0)
                db.close_trade(
                    trade_id    = trade["trade_id"],
                    exit_price  = round(exit_price, 6),
                    exit_reason = f"reconciled_{exit_reason}",
                    pnl         = round(pnl, 4),
                    pnl_pct     = round(pnl_pct, 4),
                )
                logger.info(
                    f"[RECONCILE] Crypto trade closed ({exit_reason}): {symbol} "
                    f"| Entry: ${entry:.4f} | Exit: ${exit_price:.4f} "
                    f"| PnL: ${pnl:+.2f} | trade_id: {trade['trade_id']}"
                )
                closed += 1
            else:
                logger.info(
                    f"[RECONCILE] Crypto trade still valid: {symbol} "
                    f"| Price: ${price:.4f} | SL: ${sl:.4f} | TP: ${tp:.4f}"
                )

    except Exception as e:
        logger.error(f"[RECONCILE] Crypto reconciliation error: {e}")

    return closed


def _entry_timestamp(trade: Dict) -> Optional[float]:
    """Parse entry_time from a trade dict to a Unix timestamp (for Kraken order lookups)."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(trade["entry_time"]).timestamp()
    except Exception:
        return None
