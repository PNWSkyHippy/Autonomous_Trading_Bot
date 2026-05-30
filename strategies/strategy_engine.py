"""
=============================================================
  STRATEGY ENGINE
  Orchestrates all trading strategies. For each scan cycle:
  1. Runs every enabled strategy against every symbol
  2. Deduplicates signals (same symbol from multiple strategies)
  3. Picks the highest-scoring signal per symbol
  4. Tracks per-strategy performance (win rate, profit factor)
  5. Auto-disables strategies below minimum win rate
     (skipped for strategies with auto_disable_exempt=True)
  6. Provides performance dashboard data
=============================================================
"""

import logging
from datetime import date
from typing import List, Dict, Optional

import pandas as pd

import config
from strategies.base_strategy import BaseStrategy, TradeSignal
from strategies.rsi_momentum import RSIMomentum
from strategies.bollinger_breakout import BollingerBreakout
from strategies.ema_crossover import EMACrossover
from strategies.mean_reversion import MeanReversion
from strategies.scalp_master import ScalpMaster
from strategies.swing_trader import SwingTrader
from strategies.grid_bot import GridBot
from strategies.dca_accumulator import DCAAccumulator
from strategies.vwap_momentum import VWAPMomentum
from strategies.vwap_confirmed_orb import VwapConfirmedOrb
from strategies.hammer_reversal import HammerReversal
from strategies.orb_breakout import ORBBreakout
from strategies.adaptive_regime import AdaptiveRegime
from strategies.ecb_strategy import ECBStrategy
from strategies.vdmr_strategy import VDMRStrategy
from strategies.rsi_dip_spike_v4 import RSIDipSpikeV4Strategy
from strategies.bollinger_squeeze import BollingerSqueeze
from strategies.mr_02_vef_strategy import MR02VEFStrategy
from strategies.mr_03_fbs_strategy import MR03FBSStrategy
from strategies.mr_04_fvg_strategy import MR04FVGStrategy
from strategies.btc_v6_chandelier import BTCV6ChandelierStrategy
from strategies.rsi_dip_simple import RSIDipSimpleStrategy
from strategies.pll_cycle import PLLCycleStrategy
from strategies.kds_mean_reversion import KDSMeanReversionStrategy
from strategies.ema_ribbon_breakout import EMARibbonBreakoutStrategy
from strategies.rcr_mean_reversion import RCRMeanReversionStrategy
from strategies.cbae_strategy import CBAEStrategy
from strategies.rare_strategy import RAREStrategy
from strategies.fels_strategy import FELSStrategy
from strategies.map_strategy import MAPStrategy
from strategies.sfr_structural_fakeout import SFRStrategy
from data.database import db

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_EVAL = config.STRATEGY_MIN_TRADES
MIN_WIN_RATE        = config.STRATEGY_DISABLE_WIN_RATE / 100
REENABLE_WIN_RATE   = config.STRATEGY_REENABLE_WIN_RATE / 100


def _state_enabled(value) -> bool:
    """Accept legacy string states and newer JSON boolean states."""
    if isinstance(value, bool):
        return value
    return str(value).strip().strip('"').lower() == "true"


class StrategyEngine:
    """
    Master strategy coordinator.
    Runs all enabled strategies, deduplicates, ranks, and tracks performance.
    """

    def __init__(self, db_ref=None):
        self._db = db_ref or db

        # All strategies
        self.strategies: List[BaseStrategy] = [
            RSIMomentum(),
            BollingerBreakout(),
            EMACrossover(),
            MeanReversion(),
            ScalpMaster(),
            SwingTrader(),
            GridBot(),
            DCAAccumulator(),
            VWAPMomentum(),             # Strategy 9
            VwapConfirmedOrb(),         # Strategy 10 — VWAP-confirmed ORB
            HammerReversal(),           # Strategy 11 — disabled, backtest first
            ORBBreakout(),              # Strategy 12 — disabled, backtest first
            AdaptiveRegime(),           # Strategy 13 — dual-mode trend/mean-rev (1h bars)
            ECBStrategy(),              # Strategy 14 — Entropy Collapse Breakout (CANDIDATE)
            VDMRStrategy(),             # Strategy 15 — Velocity-Deceleration Mean Reversion (INCUBATE)
            RSIDipSpikeV4Strategy(),    # Strategy 16 — RSI Dip & Spike v4c (INCUBATE/CANDIDATE)
            BollingerSqueeze(),         # Strategy 17 — Bollinger Squeeze release (INCUBATE)
            MR02VEFStrategy(),          # Strategy 18 — Volatility Exhaustion Fade 1h (INCUBATE)
            MR03FBSStrategy(),          # Strategy 19 — False Breakout Snap 1h (INCUBATE)
            MR04FVGStrategy(),          # Strategy 20 — Fair Value Gap Fill 1h (INCUBATE)
                                        # Backtest May2025-May2026 1h crypto:
                                        # ETH PF=1.32 +7.3% Sharpe=1.21 | BNB PF=1.15 +0.9%
                                        # BTC PF=1.12 +0.2% | SOL longs disabled (bled)
            BTCV6ChandelierStrategy(),  # Strategy 21 — BTC V6 Chandelier (INCUBATE) — RSI+EMA+ADX, chandelier trailing stop
            RSIDipSimpleStrategy(),     # Strategy 22 — RSI Dip Simple (INCUBATE) — naked RSI(7) cross, no filters
            PLLCycleStrategy(use_martingale=False),  # Strategy 23 — PLL Cycle base (INCUBATE) — PLL cycle detector long/short
            PLLCycleStrategy(use_martingale=True),   # Strategy 24 — PLL Cycle + Martingale (INCUBATE) — F41d variant
            KDSMeanReversionStrategy(),              # Strategy 25 — KDS Mean Reversion (INCUBATE) — 1d crypto/stock, selective edge
            EMARibbonBreakoutStrategy(),             # Strategy 26 — EMA Ribbon Breakout (INCUBATE) — 1d daily only, short edge strong
            RCRMeanReversionStrategy(),              # Strategy 27 — RCR Mean Reversion (INCUBATE) — 1d crypto/stock, AGIX/BONK edge
            CBAEStrategy(),                          # Strategy 28 — Candle Body Asymmetry Exhaustion (WATCHLIST)
            RAREStrategy(),                          # Strategy 29 — Regime Autocorrelation Reversal Entry (WATCHLIST)
            FELSStrategy(),                          # Strategy 30 — Failed Extension Liquidity Sweep (WATCHLIST)
            MAPStrategy(),                           # Strategy 31 — MAP Multi-Period Alignment (CANDIDATE) BTC 4h PF1.54 WR44%
            SFRStrategy(),                           # Strategy 32 — Structural Fakeout Reversal (WATCHLIST) — fade ORB/prior-day false breaks
        ]

        self._load_strategy_states()

        logger.info(
            f"Strategy engine initialized with {len(self.strategies)} strategies:"
        )
        for s in self.strategies:
            status  = "ENABLED"  if s.enabled           else "DISABLED"
            ml_flag = " [ML-EXEMPT]"       if getattr(s, "ml_exempt",           False) else ""
            ad_flag = " [NO-AUTO-DISABLE]" if getattr(s, "auto_disable_exempt", False) else ""
            logger.info(f"  [{status}] {s.name}{ml_flag}{ad_flag}")

    def get_all_strategies(self) -> List[BaseStrategy]:
        return self.strategies

    def _load_strategy_states(self):
        for strategy in self.strategies:
            key   = f"strategy_{strategy.name}_enabled"
            state = self._db.get_state(key, default=None)
            if state is not None:
                strategy.enabled = _state_enabled(state)
                if getattr(strategy, "auto_disable_exempt", False):
                    logger.info(
                        f"[STRATEGY LOAD] {strategy.name}: manual DB state "
                        f"loaded despite auto_disable_exempt "
                        f"(enabled={strategy.enabled})"
                    )
            else:
                # Seed the row so the DB browser always shows all strategies
                self._db.set_state(key, bool(strategy.enabled))

    def _save_strategy_state(self, strategy: BaseStrategy):
        key   = f"strategy_{strategy.name}_enabled"
        self._db.set_state(key, bool(strategy.enabled))

    def run_strategies(self, symbol: str, asset_class: str,
                       bars: pd.DataFrame,
                       price: float) -> List["StrategySignalCompat"]:
        results = []

        for strategy in self.strategies:
            if not strategy.enabled:
                continue
            # Per-asset-class enable check
            # Allows disabling a strategy for crypto but keeping it for stocks
            if asset_class == "crypto" and not getattr(strategy, "crypto_enabled", True):
                continue
            if asset_class == "stock" and not getattr(strategy, "stock_enabled", True):
                continue
            try:
                market_condition = "unknown"
                trade_signal = strategy.analyze(symbol, bars, market_condition)

                if trade_signal is None:
                    continue

                compat = StrategySignalCompat(
                    symbol                 = symbol,
                    asset_class            = asset_class,
                    direction              = trade_signal.direction,
                    score                  = trade_signal.score,
                    current_price          = price,
                    indicators             = trade_signal.metadata or {},
                    reason                 = trade_signal.reason,
                    strategy_name          = strategy.strategy_name,
                    custom_stop_loss_pct   = trade_signal.stop_loss_pct,
                    custom_take_profit_pct = trade_signal.take_profit_pct,
                    custom_position_pct    = None,
                    ml_exempt              = getattr(strategy, "ml_exempt",        False),
                    reviewer_exempt        = getattr(strategy, "reviewer_exempt",  False),
                )
                results.append(compat)

                logger.info(
                    f"[{strategy.name}] Signal: {symbol} "
                    f"{trade_signal.direction.upper()} "
                    f"score={trade_signal.score:.3f} | {trade_signal.reason}"
                )

            except Exception as e:
                logger.error(
                    f"Strategy {strategy.name} error on {symbol}: {e}",
                    exc_info=config.VERBOSE_MODE
                )

        return results

    def run_strategies_multi_tf(
        self,
        symbol: str,
        asset_class: str,
        bar_cache: Dict[str, pd.DataFrame],
        price: float
    ) -> List["StrategySignalCompat"]:
        """
        Multi-timeframe version of run_strategies.
        Each strategy declares its preferred candle timeframe via:
          stock_candle_timeframe  (Alpaca format, e.g. "5Min", "1Hour")
          crypto_candle_timeframe (CCXT format, e.g. "5m", "1h")

        bar_cache is a dict of {timeframe_string: DataFrame} pre-fetched
        by the scanner. Each strategy gets the bars for its declared timeframe.
        If a strategy's timeframe isn't in bar_cache (fetch failed), it is
        skipped gracefully rather than crashing.
        """
        results = []

        for strategy in self.strategies:
            if not strategy.enabled:
                continue
            if asset_class == "crypto" and not getattr(strategy, "crypto_enabled", True):
                continue
            if asset_class == "stock" and not getattr(strategy, "stock_enabled", True):
                continue

            # Look up the correct timeframe key for this asset class
            if asset_class == "stock":
                tf_key = getattr(strategy, "stock_candle_timeframe", "5Min")
            else:
                tf_key = getattr(strategy, "crypto_candle_timeframe", "1h")

            bars = bar_cache.get(tf_key)
            if bars is None:
                # Bars for this timeframe weren't fetched (API error or too few bars)
                logger.debug(
                    f"[{strategy.name}] Skipping {symbol} — "
                    f"no bars available for timeframe {tf_key}"
                )
                continue

            try:
                market_condition = "unknown"
                trade_signal = strategy.analyze(symbol, bars, market_condition)

                if trade_signal is None:
                    continue

                compat = StrategySignalCompat(
                    symbol                 = symbol,
                    asset_class            = asset_class,
                    direction              = trade_signal.direction,
                    score                  = trade_signal.score,
                    current_price          = price,
                    indicators             = {**(trade_signal.metadata or {}), "timeframe": tf_key},
                    reason                 = trade_signal.reason,
                    strategy_name          = strategy.strategy_name,
                    custom_stop_loss_pct   = trade_signal.stop_loss_pct,
                    custom_take_profit_pct = trade_signal.take_profit_pct,
                    custom_position_pct    = None,
                    ml_exempt              = getattr(strategy, "ml_exempt",        False),
                    reviewer_exempt        = getattr(strategy, "reviewer_exempt",  False),
                )
                results.append(compat)

                logger.info(
                    f"[{strategy.name}] Signal: {symbol} "
                    f"{trade_signal.direction.upper()} "
                    f"score={trade_signal.score:.3f} | "
                    f"tf={tf_key} | {trade_signal.reason}"
                )

            except Exception as e:
                logger.error(
                    f"Strategy {strategy.name} error on {symbol}: {e}",
                    exc_info=config.VERBOSE_MODE
                )

        return results

    def record_trade_result(self, strategy_name: str, pnl: float,
                             won: bool, exit_reason: str = ""):
        """
        Record a trade result for win/loss tracking.
        Infrastructure failures are excluded from strategy evaluation —
        these are plumbing problems not strategy failures and would corrupt
        the win rate used for auto-disable decisions.
        """
        # Exit reasons that are NOT strategy failures — never count against win rate.
        # These are infrastructure events, plumbing failures, or forced closes that
        # have nothing to do with whether the strategy logic was correct.
        EXCLUDED_REASONS = {
            "force_close",
            "force_close_stuck_stock",
            "force_close_stuck_crypto",
            "manual_close",
            "eod_close",              # EOD forced close is not a strategy signal
            "stuck",
            "broker_fallback",
            "reconcile_close",        # legacy name — kept as guard
            "broker_closed_sl_tp",    # position_monitor broker-reconcile path
            "invalid_pair",           # data/routing error — not strategy outcome
            "reconciled_ghost",       # trade opened in DB but broker never held the
                                      # position — infrastructure failure, not strategy
                                      # outcome. Was being counted as a loss ($0 pnl)
                                      # and dragging WR from 20.8% → 15.8% on bollinger.
        }
        if exit_reason.lower() in EXCLUDED_REASONS:
            logger.debug(
                f"[STRATEGY STATS] Skipping {strategy_name} result — "
                f"exit_reason='{exit_reason}' is excluded from win rate tracking"
            )
            return

        today = date.today().isoformat()
        self._db.record_strategy_result(strategy_name, today, pnl, won)
        logger.info(
            f"Strategy result: {strategy_name} — "
            f"{'WIN' if won else 'LOSS'} ${pnl:+.2f}"
        )
        self._evaluate_strategy(strategy_name)

    def _evaluate_strategy(self, strategy_name: str):
        strategy = self._get_strategy(strategy_name)
        if strategy is None:
            return

        if getattr(strategy, "auto_disable_exempt", False):
            return

        stats = self.get_strategy_stats(strategy_name)
        total = stats["total_trades"]

        if total < MIN_TRADES_FOR_EVAL:
            return

        # Opus 2026-05-29: disable on PROFITABILITY, not win rate. Edge here is
        # asymmetry (big wins / small losses), so a low win rate can still be
        # net positive — and a high win rate can still bleed. A strategy is a
        # loser only if it is BOTH net-negative AND has profit factor < 1.0
        # (gross wins < gross losses). Re-enable once it is net positive again.
        avg_pnl       = stats["avg_pnl"]        # expectancy per trade
        total_pnl     = stats["total_pnl"]
        profit_factor = stats["profit_factor"]  # 999 sentinel when no losses

        is_loser   = (total_pnl <= 0.0) and (profit_factor < 1.0)
        is_winner  = (total_pnl > 0.0)  and (profit_factor >= 1.0)

        if is_loser:
            strategy.enabled = False
            self._save_strategy_state(strategy)
            logger.warning(
                f"[AUTO-DISABLE] '{strategy_name}' disabled — net-negative "
                f"after {total} trades: total_pnl=${total_pnl:+.2f} "
                f"expectancy=${avg_pnl:+.2f}/trade PF={profit_factor:.2f} "
                f"(win rate {stats['win_rate']:.1f}% is NOT the gate)"
            )
        elif not strategy.enabled and is_winner:
            strategy.enabled = True
            self._save_strategy_state(strategy)
            logger.info(
                f"[AUTO-ENABLE] '{strategy_name}' re-enabled — profitable again: "
                f"total_pnl=${total_pnl:+.2f} expectancy=${avg_pnl:+.2f}/trade "
                f"PF={profit_factor:.2f}"
            )

    def get_strategy_stats(self, strategy_name: str) -> Dict:
        results = self._db.get_strategy_results(strategy_name, limit=500)
        if not results:
            return {
                "strategy_name": strategy_name,
                "total_trades":  0,
                "wins":          0,
                "losses":        0,
                "win_rate":      0.0,
                "total_pnl":     0.0,
                "avg_pnl":       0.0,
                "profit_factor": 0.0,
                "avg_win":       0.0,
                "avg_loss":      0.0,
                "largest_win":   0.0,
                "largest_loss":  0.0,
            }

        wins         = [r for r in results if r["won"]]
        losses       = [r for r in results if not r["won"]]
        total_pnl    = sum(r["pnl"] for r in results)
        gross_wins   = sum(r["pnl"] for r in wins)        if wins   else 0
        gross_losses = abs(sum(r["pnl"] for r in losses)) if losses else 0

        return {
            "strategy_name": strategy_name,
            "total_trades":  len(results),
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      round(len(wins) / len(results) * 100, 1),
            "total_pnl":     round(total_pnl, 2),
            "avg_pnl":       round(total_pnl / len(results), 2),
            "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses else 999,
            "avg_win":       round(gross_wins   / len(wins),   2) if wins   else 0,
            "avg_loss":      round(-gross_losses / len(losses), 2) if losses else 0,
            "largest_win":   round(max((r["pnl"] for r in wins),   default=0), 2),
            "largest_loss":  round(min((r["pnl"] for r in losses), default=0), 2),
        }

    def get_all_stats(self) -> List[Dict]:
        all_stats = []
        for strategy in self.strategies:
            stats = self.get_strategy_stats(strategy.name)
            stats["enabled"]            = strategy.enabled
            stats["ml_exempt"]          = getattr(strategy, "ml_exempt",           False)
            stats["auto_disable_exempt"]= getattr(strategy, "auto_disable_exempt", False)
            all_stats.append(stats)
        return all_stats

    def enable_strategy(self, name: str) -> str:
        strategy = self._get_strategy(name)
        if strategy is None:
            return f"Strategy '{name}' not found. Available: {self._strategy_names()}"
        strategy.enabled = True
        self._save_strategy_state(strategy)
        return f"Strategy '{name}' enabled."

    def disable_strategy(self, name: str) -> str:
        strategy = self._get_strategy(name)
        if strategy is None:
            return f"Strategy '{name}' not found. Available: {self._strategy_names()}"
        strategy.enabled = False
        self._save_strategy_state(strategy)
        return f"Strategy '{name}' disabled."

    def list_strategies(self) -> List[Dict]:
        return [
            {
                "name":                s.name,
                "enabled":             s.enabled,
                "ml_exempt":           getattr(s, "ml_exempt",           False),
                "auto_disable_exempt": getattr(s, "auto_disable_exempt", False),
                "description":         s.__class__.__doc__.strip().split("\n")[0]
                                       if s.__class__.__doc__ else s.name,
            }
            for s in self.strategies
        ]

    def _get_strategy(self, name: str) -> Optional[BaseStrategy]:
        for s in self.strategies:
            if s.name == name or s.strategy_name == name:
                return s
        return None

    def _strategy_names(self) -> str:
        return ", ".join(s.name for s in self.strategies)


class StrategySignalCompat:
    __slots__ = [
        "symbol", "asset_class", "direction", "score",
        "current_price", "indicators", "reason",
        "strategy_name", "custom_stop_loss_pct",
        "custom_take_profit_pct", "custom_position_pct",
        "ml_exempt", "reviewer_exempt",
    ]

    def __init__(self, symbol, asset_class, direction, score,
                 current_price, indicators, reason,
                 strategy_name, custom_stop_loss_pct,
                 custom_take_profit_pct, custom_position_pct,
                 ml_exempt=False, reviewer_exempt=False):
        self.symbol                 = symbol
        self.asset_class            = asset_class
        self.direction              = direction
        self.score                  = score
        self.current_price          = current_price
        self.indicators             = {
            **indicators,
            "strategy_name":           strategy_name,
            "custom_stop_loss_pct":    custom_stop_loss_pct,
            "custom_take_profit_pct":  custom_take_profit_pct,
            "custom_position_pct":     custom_position_pct,
            "ml_exempt":               ml_exempt,
            "reviewer_exempt":         reviewer_exempt,
        }
        self.reason                 = reason
        self.strategy_name          = strategy_name
        self.custom_stop_loss_pct   = custom_stop_loss_pct
        self.custom_take_profit_pct = custom_take_profit_pct
        self.custom_position_pct    = custom_position_pct
        self.ml_exempt              = ml_exempt
        self.reviewer_exempt        = reviewer_exempt


# Singleton
strategy_engine = StrategyEngine()
