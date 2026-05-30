"""
=============================================================
  BROKER MANAGER INTEGRATION GUIDE
=============================================================

FILES TO ADD:
  core/broker_manager.py    — New file (manages all brokers)
  core/kraken_executor.py   — New file (Kraken trading via CCXT)

FILES TO REPLACE:
  data/database.py          — Use database_v2.py (adds fund_events table + methods)

FILES TO EDIT:
  config.py                 — Add Kraken API keys
  core/trade_executor.py    — Add Kraken executor support
  intelligence/chat_interface.py — Add broker management commands
  requirements.txt          — Already has ccxt (Kraken uses it too)


=============================================
  1. config.py — Add Kraken keys
=============================================

Add after the Coinbase keys:

    KRAKEN_API_KEY      = os.getenv("KRAKEN_API_KEY",    "YOUR_KRAKEN_KEY")
    KRAKEN_SECRET_KEY   = os.getenv("KRAKEN_SECRET_KEY", "YOUR_KRAKEN_SECRET")
    KRAKEN_PAPER        = True   # Set to False for live trading


=============================================
  2. .env — Add Kraken keys
=============================================

    KRAKEN_API_KEY=your_kraken_api_key_here
    KRAKEN_SECRET_KEY=your_kraken_secret_here


=============================================
  3. core/trade_executor.py — Add Kraken
=============================================

At the top, add import:

    from core.kraken_executor import KrakenExecutor

In TradeExecutor.__init__(), add:

    from config import KRAKEN_API_KEY, KRAKEN_SECRET_KEY, KRAKEN_PAPER
    self.kraken = KrakenExecutor(
        api_key=KRAKEN_API_KEY,
        api_secret=KRAKEN_SECRET_KEY,
        paper=KRAKEN_PAPER
    )

In _submit(), add a kraken case:

    elif signal.asset_class == "crypto" and signal.indicators.get("broker") == "kraken":
        return self.kraken.submit_order(
            signal.symbol, approval.quantity, side,
            approval.stop_loss, approval.take_profit
        )

In close_trade(), add kraken case in the close section:

    elif asset_class == "crypto" and trade.get("broker") == "kraken":
        side = "buy" if direction == "long" else "sell"
        success = self.kraken.close_position(symbol, qty, side)


=============================================
  4. config.py — Add Kraken pairs to scanner
=============================================

In ScannerConfig.crypto_pairs, the existing pairs work on both
Coinbase and Kraken. The broker_manager decides which broker
to route each pair to. No changes needed unless you want
Kraken-only pairs.


=============================================
  5. chat_interface.py — Add broker commands
=============================================

Add to SYSTEM_PROMPT:

    {"action": "list_brokers"}
    {"action": "inject_funds", "broker": "kraken", "amount": 500.00, "reason": "initial deposit"}
    {"action": "withdraw_funds", "broker": "alpaca", "amount": 200.00, "reason": "rent"}
    {"action": "capital_breakdown"}
    {"action": "add_broker", "name": "kraken", "type": "crypto", "display_name": "Kraken"}
    {"action": "enable_broker", "name": "kraken"}
    {"action": "disable_broker", "name": "kraken"}

Add to _parse_and_execute_actions():

                elif action == "list_brokers":
                    from core.broker_manager import broker_manager
                    brokers = broker_manager.list_brokers()
                    info = ""
                    for b in brokers:
                        status = "ON" if b["enabled"] else "OFF"
                        paper = " (PAPER)" if b.get("paper") else ""
                        info += (
                            f"\\n  {'\\u2705' if b['enabled'] else '\\u274c'} "
                            f"{b['display_name']} [{b['name']}] — "
                            f"{b['broker_type']}{paper} — Key: {b['api_key_hint']}"
                        )
                    results.append(f"Registered Brokers:{info}")

                elif action == "inject_funds":
                    from core.broker_manager import broker_manager
                    broker = cmd.get("broker", "")
                    amount = float(cmd.get("amount", 0))
                    reason = cmd.get("reason", "Fund deposit")
                    result = broker_manager.inject_funds(broker, amount, reason)
                    results.append(result["message"])

                elif action == "withdraw_funds":
                    from core.broker_manager import broker_manager
                    broker = cmd.get("broker", "")
                    amount = float(cmd.get("amount", 0))
                    reason = cmd.get("reason", "Withdrawal")
                    result = broker_manager.withdraw_funds(broker, amount, reason)
                    results.append(result["message"])

                elif action == "capital_breakdown":
                    from core.broker_manager import broker_manager
                    breakdown = broker_manager.get_capital_breakdown()
                    info = f"\\nTotal Capital: ${breakdown['total_balance']:,.2f}"
                    info += f"\\nTotal Invested: ${breakdown['total_invested']:,.2f}"
                    info += f"\\nTotal Available: ${breakdown['total_available']:,.2f}"
                    info += f"\\n\\nPer Broker:"
                    for b in breakdown["brokers"]:
                        paper = " (PAPER)" if b.get("paper") else ""
                        info += (
                            f"\\n  {b['display_name']}{paper}: "
                            f"${b['balance']:,.2f} "
                            f"(invested: ${b['invested']:,.2f}, "
                            f"available: ${b['available']:,.2f})"
                        )
                    results.append(f"Capital Breakdown:{info}")

                elif action == "add_broker":
                    from core.broker_manager import broker_manager
                    msg = broker_manager.register_broker(
                        name=cmd.get("name", ""),
                        broker_type=cmd.get("type", "crypto"),
                        display_name=cmd.get("display_name", ""),
                    )
                    results.append(msg)

                elif action == "enable_broker":
                    from core.broker_manager import broker_manager
                    msg = broker_manager.enable_broker(cmd.get("name", ""))
                    results.append(msg)

                elif action == "disable_broker":
                    from core.broker_manager import broker_manager
                    msg = broker_manager.disable_broker(cmd.get("name", ""))
                    results.append(msg)


=============================================
  6. bot_engine.py — Sync broker balances
=============================================

In _sync_capital(), add broker sync:

    from core.broker_manager import broker_manager
    broker_manager.sync_all_balances()


=============================================
  WITHDRAWAL / INJECTION CLARIFICATION
=============================================

All fund movements are INFORMATIONAL ONLY:

  - "Inject $500 into Kraken" → Updates the bot's internal
    tracking. YOU must actually deposit the money in Kraken.

  - "Withdraw $200 from Alpaca" → Updates the bot's tracking.
    YOU must actually withdraw from Alpaca.

The bot NEVER moves real money between accounts. It only
tracks balances so it knows how much capital is available
for trading at each broker.

"""
