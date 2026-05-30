"""
=============================================================
  BROKER MANAGER
  Manages multiple brokers with per-broker capital tracking.
  Supports: Alpaca (stocks), Coinbase (crypto), Kraken (crypto).
  Features:
  - Per-broker balance tracking
  - Fund injection / withdrawal logging
  - Dynamic broker registration
  - Consolidated and per-broker reporting
=============================================================
"""

import logging
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from data.database import db

logger = logging.getLogger(__name__)


class BrokerManager:
    """
    Central manager for all connected brokers.
    Tracks which brokers are active, their balances,
    and provides consolidated + per-broker capital views.
    """

    def __init__(self):
        self._brokers: Dict[str, Dict] = {}
        self._load_brokers()

    def _load_brokers(self):
        """Load broker configurations from database."""
        saved = db.get_state("registered_brokers")
        if saved:
            self._brokers = saved
        else:
            # Initialize with defaults from config
            self._register_defaults()

    def _register_defaults(self):
        """Register the default brokers from config.py."""
        from config import (ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
                            COINBASE_API_KEY, COINBASE_SECRET_KEY)

        self.register_broker(
            name="alpaca",
            broker_type="stock",
            display_name="Alpaca Markets",
            api_key=ALPACA_API_KEY,
            api_secret=ALPACA_SECRET_KEY,
            base_url=ALPACA_BASE_URL,
            enabled=True,
            paper=("paper" in ALPACA_BASE_URL)
        )

        self.register_broker(
            name="coinbase",
            broker_type="crypto",
            display_name="Coinbase Advanced Trade",
            api_key=COINBASE_API_KEY,
            api_secret=COINBASE_SECRET_KEY,
            enabled=True,
            paper=False
        )

    def register_broker(self, name: str, broker_type: str,
                        display_name: str = "",
                        api_key: str = "", api_secret: str = "",
                        base_url: str = "", enabled: bool = True,
                        paper: bool = False, **extra) -> str:
        """
        Register a new broker or update an existing one.
        broker_type: stock, crypto, or multi
        """
        self._brokers[name] = {
            "name": name,
            "broker_type": broker_type,
            "display_name": display_name or name.title(),
            "api_key": api_key,
            "api_secret": api_secret,
            "base_url": base_url,
            "enabled": enabled,
            "paper": paper,
            "registered_at": datetime.utcnow().isoformat(),
            **extra
        }
        self._save_brokers()
        logger.info(f"Broker registered: {name} ({display_name}) "
                    f"type={broker_type} paper={paper}")
        return f"Broker '{name}' registered successfully."

    def remove_broker(self, name: str) -> str:
        """Remove a broker registration (does not affect open trades)."""
        if name in self._brokers:
            del self._brokers[name]
            self._save_brokers()
            return f"Broker '{name}' removed."
        return f"Broker '{name}' not found."

    def enable_broker(self, name: str) -> str:
        if name in self._brokers:
            self._brokers[name]["enabled"] = True
            self._save_brokers()
            return f"Broker '{name}' enabled."
        return f"Broker '{name}' not found."

    def disable_broker(self, name: str) -> str:
        if name in self._brokers:
            self._brokers[name]["enabled"] = False
            self._save_brokers()
            return f"Broker '{name}' disabled."
        return f"Broker '{name}' not found."

    def get_broker(self, name: str) -> Optional[Dict]:
        return self._brokers.get(name)

    def list_brokers(self) -> List[Dict]:
        """List all registered brokers (redacts secrets)."""
        result = []
        for name, info in self._brokers.items():
            safe = {k: v for k, v in info.items()
                    if k not in ("api_key", "api_secret")}
            # Show masked key hint
            key = info.get("api_key", "")
            safe["api_key_hint"] = f"...{key[-4:]}" if len(key) > 4 else "(not set)"
            result.append(safe)
        return result

    def get_enabled_brokers(self, broker_type: str = None) -> List[Dict]:
        """Get enabled brokers, optionally filtered by type."""
        results = []
        for name, info in self._brokers.items():
            if not info.get("enabled"):
                continue
            if broker_type and info.get("broker_type") != broker_type:
                continue
            results.append(info)
        return results

    def _save_brokers(self):
        """Persist broker configs to database."""
        db.set_state("registered_brokers", self._brokers)

    # ─────────────────────────────────────────────
    #  PER-BROKER CAPITAL TRACKING
    # ─────────────────────────────────────────────

    def get_broker_balance(self, broker_name: str) -> Dict:
        """Get the latest tracked balance for a broker."""
        bal = db.get_state(f"broker_balance_{broker_name}")
        if bal:
            return bal
        return {
            "broker": broker_name,
            "balance": 0.0,
            "invested": 0.0,
            "available": 0.0,
            "last_updated": None
        }

    def update_broker_balance(self, broker_name: str, balance: float,
                              invested: float = 0.0, available: float = None):
        """Update tracked balance for a broker."""
        if available is None:
            available = balance - invested
        record = {
            "broker": broker_name,
            "balance": round(balance, 2),
            "invested": round(invested, 2),
            "available": round(available, 2),
            "last_updated": datetime.utcnow().isoformat()
        }
        db.set_state(f"broker_balance_{broker_name}", record)
        logger.info(f"Broker balance updated: {broker_name} = ${balance:,.2f} "
                    f"(available: ${available:,.2f}, invested: ${invested:,.2f})")

    def get_capital_breakdown(self) -> Dict:
        """
        Get a full breakdown of capital across all brokers.
        Returns per-broker balances plus consolidated totals.
        """
        brokers = []
        total_balance = 0.0
        total_invested = 0.0
        total_available = 0.0

        for name in self._brokers:
            bal = self.get_broker_balance(name)
            info = self._brokers[name]
            entry = {
                "broker": name,
                "display_name": info.get("display_name", name),
                "broker_type": info.get("broker_type", ""),
                "enabled": info.get("enabled", False),
                "paper": info.get("paper", False),
                "balance": bal["balance"],
                "invested": bal["invested"],
                "available": bal["available"],
                "last_updated": bal["last_updated"],
            }
            brokers.append(entry)
            total_balance += bal["balance"]
            total_invested += bal["invested"]
            total_available += bal["available"]

        return {
            "brokers": brokers,
            "total_balance": round(total_balance, 2),
            "total_invested": round(total_invested, 2),
            "total_available": round(total_available, 2),
            "broker_count": len(self._brokers),
            "timestamp": datetime.utcnow().isoformat()
        }

    # ─────────────────────────────────────────────
    #  FUND INJECTION & WITHDRAWAL (informational)
    # ─────────────────────────────────────────────

    def inject_funds(self, broker_name: str, amount: float,
                     reason: str = "Fund deposit") -> Dict:
        """
        Record a fund injection into a specific broker.
        This is INFORMATIONAL ONLY — you deposit the money manually,
        then tell the bot so it updates its capital tracking.
        """
        if amount <= 0:
            return {"success": False, "message": "Amount must be positive."}

        if broker_name not in self._brokers:
            return {"success": False,
                    "message": f"Broker '{broker_name}' not found. "
                               f"Available: {', '.join(self._brokers.keys())}"}

        # Update broker balance
        bal = self.get_broker_balance(broker_name)
        new_balance = bal["balance"] + amount
        new_available = bal["available"] + amount
        self.update_broker_balance(broker_name, new_balance,
                                   bal["invested"], new_available)

        # Log the injection event
        db.record_fund_event(
            broker_name=broker_name,
            event_type="injection",
            amount=amount,
            reason=reason,
            balance_before=bal["balance"],
            balance_after=new_balance
        )

        # Update global capital
        cap = db.get_latest_capital()
        if cap:
            new_total = cap["total_capital"] + amount
            db.log_capital(
                total=new_total,
                available=cap["available_cash"] + amount,
                invested=cap["invested_value"],
                daily_pnl=cap.get("daily_pnl", 0),
                total_pnl=cap.get("total_pnl", 0),
                note=f"Fund injection: ${amount:.2f} into {broker_name} — {reason}"
            )

        logger.info(f"Fund injection: ${amount:.2f} into {broker_name} ({reason})")
        return {
            "success": True,
            "broker": broker_name,
            "amount": amount,
            "balance_before": bal["balance"],
            "balance_after": new_balance,
            "message": f"${amount:.2f} injected into {broker_name}. "
                       f"New broker balance: ${new_balance:,.2f}. "
                       f"Remember to actually deposit the funds in your broker account."
        }

    def withdraw_funds(self, broker_name: str, amount: float,
                       reason: str = "Withdrawal") -> Dict:
        """
        Record a fund withdrawal from a specific broker.
        INFORMATIONAL ONLY — withdraw manually, then tell the bot.
        """
        if amount <= 0:
            return {"success": False, "message": "Amount must be positive."}

        if broker_name not in self._brokers:
            return {"success": False,
                    "message": f"Broker '{broker_name}' not found."}

        bal = self.get_broker_balance(broker_name)
        if amount > bal["balance"]:
            return {"success": False,
                    "message": f"Withdrawal ${amount:.2f} exceeds {broker_name} "
                               f"balance of ${bal['balance']:,.2f}."}

        new_balance = bal["balance"] - amount
        new_available = max(0, bal["available"] - amount)
        self.update_broker_balance(broker_name, new_balance,
                                   bal["invested"], new_available)

        # Log the withdrawal event
        db.record_fund_event(
            broker_name=broker_name,
            event_type="withdrawal",
            amount=amount,
            reason=reason,
            balance_before=bal["balance"],
            balance_after=new_balance
        )

        # Update global capital
        cap = db.get_latest_capital()
        if cap:
            new_total = cap["total_capital"] - amount
            db.log_capital(
                total=new_total,
                available=max(0, cap["available_cash"] - amount),
                invested=cap["invested_value"],
                daily_pnl=cap.get("daily_pnl", 0),
                total_pnl=cap.get("total_pnl", 0),
                note=f"Withdrawal: ${amount:.2f} from {broker_name} — {reason}"
            )

        logger.info(f"Withdrawal: ${amount:.2f} from {broker_name} ({reason})")
        return {
            "success": True,
            "broker": broker_name,
            "amount": amount,
            "balance_before": bal["balance"],
            "balance_after": new_balance,
            "message": f"${amount:.2f} withdrawn from {broker_name}. "
                       f"New broker balance: ${new_balance:,.2f}."
        }

    def sync_all_balances(self):
        """
        Pull live balances from all enabled brokers.
        Called periodically by the capital sync job.
        """
        for name, info in self._brokers.items():
            if not info.get("enabled"):
                continue
            try:
                if name == "alpaca":
                    self._sync_alpaca(info)
                elif name == "coinbase":
                    self._sync_coinbase(info)
                elif name == "kraken":
                    self._sync_kraken(info)
                else:
                    logger.debug(f"No sync handler for broker: {name}")
            except Exception as e:
                logger.debug(f"Balance sync failed for {name}: {e}")

    def _sync_alpaca(self, info: Dict):
        try:
            import alpaca_trade_api as tradeapi
            api = tradeapi.REST(info["api_key"], info["api_secret"],
                                info.get("base_url", "https://paper-api.alpaca.markets"))
            acct = api.get_account()
            balance = float(acct.equity)
            invested = float(acct.portfolio_value) - float(acct.cash)
            available = float(acct.cash)
            self.update_broker_balance("alpaca", balance, invested, available)
        except Exception as e:
            logger.debug(f"Alpaca sync error: {e}")

    def _sync_coinbase(self, info: Dict):
        try:
            import ccxt
            exchange = ccxt.coinbase({
                "apiKey": info["api_key"],
                "secret": info["api_secret"],
                "enableRateLimit": True,
            })
            balance = exchange.fetch_balance()
            usd_total = float(balance.get("USD", {}).get("total", 0))
            usd_free = float(balance.get("USD", {}).get("free", 0))
            self.update_broker_balance("coinbase", usd_total,
                                       usd_total - usd_free, usd_free)
        except Exception as e:
            logger.debug(f"Coinbase sync error: {e}")

    def _sync_kraken(self, info: Dict):
        try:
            import ccxt
            exchange = ccxt.kraken({
                "apiKey": info["api_key"],
                "secret": info["api_secret"],
                "enableRateLimit": True,
            })
            balance = exchange.fetch_balance()
            # Kraken reports in ZUSD for USD
            usd_total = float(balance.get("USD", {}).get("total", 0))
            usd_free = float(balance.get("USD", {}).get("free", 0))
            if usd_total == 0:
                # Try ZUSD (Kraken's internal name)
                usd_total = float(balance.get("ZUSD", {}).get("total", 0))
                usd_free = float(balance.get("ZUSD", {}).get("free", 0))
            self.update_broker_balance("kraken", usd_total,
                                       usd_total - usd_free, usd_free)
        except Exception as e:
            logger.debug(f"Kraken sync error: {e}")


# Singleton
broker_manager = BrokerManager()
