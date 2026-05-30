"""
=============================================================
  INTEGRATION GUIDE
  How to wire the 8 strategies into your existing codebase.
  4 files need changes + database additions.
=============================================================

OVERVIEW:
  New folder:  strategies/
    __init__.py
    base_strategy.py
    strategy_engine.py
    rsi_momentum.py
    bollinger_breakout.py
    ema_crossover.py
    mean_reversion.py
    scalp_master.py
    swing_trader.py
    grid_bot.py
    dca_accumulator.py

  Files to modify:
    1. data/database.py      — Add strategy_results table + methods
    2. scanners/market_scanner.py — Use strategy engine instead of hardcoded scoring
    3. core/trade_executor.py     — Record strategy name + custom SL/TP overrides
    4. intelligence/chat_interface.py — Add strategy commands


=============================================
  1. data/database.py CHANGES
=============================================

In _create_tables(), add:

    self._execute('''
        CREATE TABLE IF NOT EXISTS strategy_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name   TEXT NOT NULL,
            trade_date      TEXT NOT NULL,
            pnl             REAL NOT NULL,
            won             INTEGER NOT NULL,
            recorded_at     TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Add strategy_name column to trades table (safe to run multiple times)
    try:
        self._execute("ALTER TABLE trades ADD COLUMN strategy_name TEXT DEFAULT 'original'")
    except:
        pass

Add these methods to the Database class:

    def record_strategy_result(self, strategy_name, trade_date, pnl, won):
        self._execute(
            "INSERT INTO strategy_results (strategy_name, trade_date, pnl, won) VALUES (?, ?, ?, ?)",
            (strategy_name, trade_date, pnl, int(won))
        )

    def get_strategy_results(self, strategy_name, limit=500):
        rows = self._fetchall(
            "SELECT strategy_name, trade_date, pnl, won FROM strategy_results "
            "WHERE strategy_name = ? ORDER BY id DESC LIMIT ?",
            (strategy_name, limit)
        )
        return [{"strategy_name": r[0], "trade_date": r[1], "pnl": r[2], "won": bool(r[3])}
                for r in rows]


=============================================
  2. scanners/market_scanner.py CHANGES
=============================================

At the top, add import:

    from strategies.strategy_engine import strategy_engine

In MarketScanner.scan_all(), REPLACE the stock and crypto scanning sections
with strategy-engine-powered scanning. Here's the updated scan_all():

    def scan_all(self) -> List[Signal]:
        all_signals = []

        # Check market condition
        market_condition = self.condition_detector.get_spy_condition(self.stock_scanner)
        logger.info(
            f"Market condition: {market_condition.condition.value} | "
            f"ADX={market_condition.adx} | "
            f"Position scalar: {market_condition.position_scalar:.0%} | "
            f"{market_condition.reason}"
        )
        if not market_condition.should_trade:
            logger.warning(f"Market condition says skip: {market_condition.reason}")
            return []

        # ── Scan stocks with all strategies ──
        logger.info(">> Scanning stocks with strategy engine...")
        try:
            for symbol in self.stock_scanner.cfg_s.stock_watchlist:
                try:
                    bars = self.stock_scanner.get_bars(symbol)
                    if bars is None:
                        continue
                    price = bars["close"].iloc[-1]
                    # Basic filters
                    if price < self.stock_scanner.cfg_s.min_price_stock:
                        continue
                    if price > self.stock_scanner.cfg_s.max_price_stock:
                        continue
                    if bars["volume"].iloc[-20:].mean() < self.stock_scanner.cfg_s.min_volume_threshold:
                        continue

                    strat_signals = strategy_engine.run_strategies(symbol, "stock", bars, price)
                    for ss in strat_signals:
                        # Convert StrategySignal to Signal for compatibility
                        all_signals.append(Signal(
                            symbol=ss.symbol,
                            asset_class=ss.asset_class,
                            direction=ss.direction,
                            score=ss.score,
                            current_price=ss.current_price,
                            indicators={**ss.indicators,
                                        "strategy_name": ss.strategy_name,
                                        "custom_stop_loss_pct": ss.custom_stop_loss_pct,
                                        "custom_take_profit_pct": ss.custom_take_profit_pct,
                                        "custom_position_pct": ss.custom_position_pct},
                            reason=f"[{ss.strategy_name}] {ss.reason}",
                        ))
                except Exception as e:
                    logger.debug(f"Error scanning {symbol}: {e}")
        except Exception as e:
            logger.error(f"Stock scan error: {e}")

        # ── Scan crypto with all strategies ──
        if config.schedule.crypto_trading:
            logger.info(">> Scanning crypto with strategy engine...")
            try:
                for pair in self.crypto_scanner.cfg_s.crypto_pairs:
                    try:
                        df = self.crypto_scanner.get_ohlcv(pair)
                        if df is None:
                            continue
                        price = df["close"].iloc[-1]

                        strat_signals = strategy_engine.run_strategies(pair, "crypto", df, price)
                        for ss in strat_signals:
                            all_signals.append(Signal(
                                symbol=ss.symbol,
                                asset_class=ss.asset_class,
                                direction=ss.direction,
                                score=ss.score,
                                current_price=ss.current_price,
                                indicators={**ss.indicators,
                                            "strategy_name": ss.strategy_name,
                                            "custom_stop_loss_pct": ss.custom_stop_loss_pct,
                                            "custom_take_profit_pct": ss.custom_take_profit_pct,
                                            "custom_position_pct": ss.custom_position_pct},
                                reason=f"[{ss.strategy_name}] {ss.reason}",
                            ))
                    except Exception as e:
                        logger.debug(f"Error scanning {pair}: {e}")
            except Exception as e:
                logger.error(f"Crypto scan error: {e}")

        # ── Deduplicate: one signal per symbol (highest score wins) ──
        from strategies.base_strategy import StrategySignal as SS
        # Convert back temporarily for dedup
        by_symbol = {}
        for sig in all_signals:
            existing = by_symbol.get(sig.symbol)
            if existing is None or sig.score > existing.score:
                by_symbol[sig.symbol] = sig
        all_signals = list(by_symbol.values())

        # ── ML scoring + condition scalar ──
        for signal in all_signals:
            enhanced_score = self.ml_scorer.score(
                indicators=signal.indicators,
                base_score=signal.score,
                condition_adx=market_condition.adx
            )
            signal.score = round(enhanced_score * market_condition.position_scalar, 3)
            signal.indicators["market_condition"] = market_condition.condition.value
            signal.indicators["condition_scalar"] = market_condition.position_scalar

        all_signals = [s for s in all_signals
                       if s.score >= config.risk.min_signal_confidence]
        all_signals.sort(key=lambda x: x.score, reverse=True)
        logger.info(f">> Final signals: {len(all_signals)}")
        return all_signals


=============================================
  3. core/trade_executor.py CHANGES
=============================================

In execute_signal(), after the risk approval section, check for
custom stop/take-profit from the strategy:

    # After: approval = risk_manager.approve_trade(...)
    # Add custom overrides from strategy
    custom_sl = signal.indicators.get("custom_stop_loss_pct")
    custom_tp = signal.indicators.get("custom_take_profit_pct")
    custom_pos = signal.indicators.get("custom_position_pct")

    # Recalculate levels if strategy provides custom values
    if custom_sl is not None:
        if signal.direction == "long":
            stop_loss = entry_price * (1 - custom_sl)
        else:
            stop_loss = entry_price * (1 + custom_sl)
    if custom_tp is not None:
        if signal.direction == "long":
            take_profit = entry_price * (1 + custom_tp)
        else:
            take_profit = entry_price * (1 - custom_tp)

In the trade_record dict, add:
    "strategy_name": signal.indicators.get("strategy_name", "original"),

In close_trade(), after recording the result with risk_manager,
also record the strategy result:

    # After: risk_manager.record_trade_result(pnl, trade_won)
    strategy_name = trade.get("strategy_name", "original")
    if strategy_name != "original":
        from strategies.strategy_engine import strategy_engine
        strategy_engine.record_trade_result(strategy_name, pnl, trade_won)


=============================================
  4. intelligence/chat_interface.py CHANGES
=============================================

Add strategy commands to the SYSTEM_PROMPT:

    {"action": "list_strategies"}
    {"action": "enable_strategy", "name": "scalp_master"}
    {"action": "disable_strategy", "name": "grid_bot"}
    {"action": "strategy_stats"}

In _parse_and_execute_actions(), add handlers:

    elif action == "list_strategies":
        from strategies.strategy_engine import strategy_engine
        strategies = strategy_engine.list_strategies()
        info = "\\n".join(
            f"  {'✅' if s['enabled'] else '❌'} {s['name']}: {s['description']}"
            for s in strategies
        )
        results.append(f"Strategies:\\n{info}")

    elif action == "enable_strategy":
        from strategies.strategy_engine import strategy_engine
        msg = strategy_engine.enable_strategy(cmd.get("name", ""))
        results.append(msg)

    elif action == "disable_strategy":
        from strategies.strategy_engine import strategy_engine
        msg = strategy_engine.disable_strategy(cmd.get("name", ""))
        results.append(msg)

    elif action == "strategy_stats":
        from strategies.strategy_engine import strategy_engine
        all_stats = strategy_engine.get_all_stats()
        info = ""
        for s in all_stats:
            status = "✅" if s["enabled"] else "❌"
            info += (
                f"\\n{status} {s['strategy_name']}: "
                f"{s['total_trades']} trades | "
                f"Win rate: {s['win_rate']:.1f}% | "
                f"P&L: ${s['total_pnl']:+.2f} | "
                f"PF: {s['profit_factor']:.2f}"
            )
        results.append(f"Strategy Performance:{info}")

"""
