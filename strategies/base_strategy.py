"""
base_strategy.py — Abstract base class for all 8 trading strategies.
Trading Bot v2

All strategies inherit from BaseStrategy and must implement:
  - name (property)
  - analyze(symbol, candles, market_condition) -> TradeSignal or None

The verbose_log() method is available to all strategies and will log
detailed signal condition pass/fail data when --verbose mode is active.
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Iterable
import pandas as pd

import config

_INJECTED_CACHE = {"time": 0.0, "symbols": set()}


def _symbol_variants(symbol: str) -> set:
    s = str(symbol or "").strip().upper()
    if not s:
        return set()
    variants = {s, s.replace("-", "/")}
    if "/" in s:
        base, quote = s.split("/", 1)
        variants.update({base, f"{base}-{quote}"})
    elif "-" in s:
        base, quote = s.split("-", 1)
        variants.update({base, f"{base}/{quote}"})
    else:
        variants.update({f"{s}/USD", f"{s}/USDT", f"{s}-USD", f"{s}-USDT"})
    return variants


def _read_symbol_file(path: str) -> set:
    try:
        with open(path, "r") as f:
            raw = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    except FileNotFoundError:
        return set()
    except Exception:
        return set()

    symbols = set()
    for item in raw:
        symbols.update(_symbol_variants(item))
    return symbols


def _injected_symbols() -> set:
    now = time.time()
    if now - _INJECTED_CACHE["time"] < 30:
        return _INJECTED_CACHE["symbols"]

    paths = [
        getattr(config, "TEMP_STOCKS_FILE", "watchlist/scanned_stocks.txt"),
        getattr(config, "TEMP_CRYPTO_FILE", "watchlist/scanned_crypto.txt"),
        "watchlist/scanned_stocks.txt",
        "watchlist/scanned_crypto.txt",
    ]
    symbols = set()
    for path in dict.fromkeys(paths):
        symbols.update(_read_symbol_file(path))

    _INJECTED_CACHE["time"] = now
    _INJECTED_CACHE["symbols"] = symbols
    return symbols


def _normalise_whitelist(whitelist: Iterable[str]) -> set:
    symbols = set()
    for item in whitelist:
        symbols.update(_symbol_variants(item))
    return symbols


# =============================================================================
# TRADE SIGNAL — returned by strategies when a trade opportunity is found
# =============================================================================

@dataclass
class TradeSignal:
    """Represents a trade opportunity identified by a strategy."""
    symbol:         str
    direction:      str         # "long" or "short"
    score:          float       # Confidence score 0.0–1.0
    strategy_name:  str
    stop_loss_pct:  float       # Strategy-specific stop loss %
    take_profit_pct:float       # Strategy-specific take profit %
    reason:         str = ""    # Human-readable reason for the signal
    metadata:       dict = field(default_factory=dict)


# =============================================================================
# BASE STRATEGY
# =============================================================================

class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.

    Subclasses must define:
      - self.strategy_name (str)
      - self.stop_loss_pct (float)
      - self.take_profit_pct (float)
      - analyze(symbol, candles, market_condition) -> Optional[TradeSignal]

    Optional flags (set in subclass __init__):
      - self.ml_exempt (bool, default False)
            When True, this strategy's signals bypass ML score blending.
            Use for strategies that are the sole provider for an asset class
            (e.g. bollinger_breakout for crypto) where ML blending on a small
            or bad training batch would suppress signals below threshold and
            cut off all trading in that asset class.

      - self.reviewer_exempt (bool, default False)
            When True, this strategy's signals skip the Claude reviewer gate.
            Use for strategies that have been backtested and validated and
            whose signals would reliably fail reviewer scoring due to unusual
            metadata shape (e.g. strategies with ATR-based stops instead of
            a standard stop_loss_pct).

      - self.auto_disable_exempt (bool, default False)
            When True, the strategy engine will never auto-disable this
            strategy based on win rate, regardless of how many trades it
            has taken. Use when a strategy is the only coverage for an asset
            class and disabling it would halt all trading there entirely.
    """

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.enabled = True

        # These must be set by each subclass
        self.strategy_name   = "BaseStrategy"
        self.stop_loss_pct   = config.DEFAULT_STOP_LOSS_PCT
        self.take_profit_pct = config.DEFAULT_TAKE_PROFIT_PCT

        # Protection flags — override in subclass __init__ as needed
        self.ml_exempt          = False  # Skip ML score blending for this strategy
        self.reviewer_exempt    = False  # Skip Claude reviewer gate for this strategy
        self.auto_disable_exempt= False  # Never auto-disable this strategy

        # ── Candle timeframe ─────────────────────────────────────────────
        # The timeframe and bar count this strategy needs to analyze correctly.
        # The scanner fetches bars per timeframe and reuses them across strategies
        # that share the same timeframe — only ONE API call per timeframe per symbol.
        #
        # Stock timeframes (Alpaca): "1Min", "5Min", "15Min", "1Hour", "1Day"
        # Crypto timeframes (Kraken/CCXT): "1m", "5m", "15m", "1h", "1d"
        #
        # Strategies that need 1hr bars for proper signal generation should set:
        #   self.stock_candle_timeframe  = "1Hour"
        #   self.crypto_candle_timeframe = "1h"
        #
        # Default is 5min for stocks and 1hr for crypto (matches current behavior).
        self.stock_candle_timeframe  = "5Min"   # Alpaca timeframe string
        self.crypto_candle_timeframe = "1h"     # CCXT timeframe string
        self.candle_limit            = 100       # Number of bars to fetch

        # Per-asset-class enable flags
        # Set stock_enabled=False to skip for stocks
        # Set crypto_enabled=False to skip for crypto
        # Both default True — runs on all asset classes
        self.stock_enabled  = True
        self.crypto_enabled = True

    def _passes_symbol_whitelist(
        self,
        symbol: str,
        whitelist: Iterable[str],
        whitelist_name: str,
    ) -> bool:
        symbol_keys = _symbol_variants(symbol)
        if symbol_keys & _normalise_whitelist(whitelist):
            return True

        if (
            getattr(config, "BYPASS_STRATEGY_WHITELIST_FOR_INJECTED_SYMBOLS", True)
            and symbol_keys & _injected_symbols()
        ):
            self.verbose_log(
                symbol,
                f"{whitelist_name} bypassed for injected/manual symbol",
                True,
                symbol,
                "in scanned injection list",
            )
            return True

        return False

    @property
    def name(self) -> str:
        return self.strategy_name

    @abstractmethod
    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:
        """
        Analyze candle data for a symbol and return a TradeSignal if
        conditions are met, or None if no trade opportunity exists.

        Args:
            symbol:           Ticker symbol, e.g. "BTC/USD" or "AAPL"
            candles:          DataFrame with columns: open, high, low, close, volume
                              Sorted oldest-first. At least 60 rows recommended.
            market_condition: "trending", "ranging", "volatile", or "unknown"

        Returns:
            TradeSignal if conditions are met, None otherwise.
        """
        pass

    # =========================================================================
    # VERBOSE LOGGING — call this for every condition check in analyze()
    # =========================================================================

    def verbose_log(
        self,
        symbol: str,
        condition_name: str,
        passed: bool,
        actual_value,
        threshold_value,
        direction: str = "",
        extra: str = ""
    ):
        """
        Log the result of a single signal condition check.

        Only writes to the log when VERBOSE_MODE is True (--verbose flag).
        Each call produces one line in bot.log showing whether the condition
        passed or failed, what the actual value was, and what was required.

        Example log output:
            [VERBOSE][RSIMomentum][BTC/USD] FAIL: RSI oversold |
                actual=52.31 | required=<30 | direction=long

        Args:
            symbol:          The symbol being analyzed, e.g. "BTC/USD"
            condition_name:  Short description, e.g. "RSI oversold"
            passed:          True if the condition was satisfied
            actual_value:    The actual indicator value
            threshold_value: The required threshold (for display)
            direction:       "long", "short", or "" if not directional
            extra:           Any additional context string
        """
        if not config.VERBOSE_MODE:
            return

        status = "PASS" if passed else "FAIL"
        dir_str = f" | direction={direction}" if direction else ""
        extra_str = f" | {extra}" if extra else ""

        # Format actual_value — handle both floats and other types
        try:
            actual_str = f"{float(actual_value):.4f}"
        except (TypeError, ValueError):
            actual_str = str(actual_value)

        message = (
            f"[VERBOSE][{self.strategy_name}][{symbol}] "
            f"{status}: {condition_name} | "
            f"actual={actual_str} | "
            f"required={threshold_value}"
            f"{dir_str}"
            f"{extra_str}"
        )

        # Always log at DEBUG level — visible in log file during verbose mode
        self.logger.debug(message)

    def verbose_log_score(self, symbol: str, score: float, threshold: float):
        """
        Log the final computed score vs the minimum required score.
        Call this at the end of analyze() before returning the signal.
        """
        if not config.VERBOSE_MODE:
            return

        passed = score >= threshold
        status = "PASS — SIGNAL GENERATED" if passed else "FAIL — below threshold, no trade"

        try:
            score_str = f"{score:.4f}"
        except (TypeError, ValueError):
            score_str = str(score)

        message = (
            f"[VERBOSE][{self.strategy_name}][{symbol}] "
            f"FINAL SCORE: {score_str} | "
            f"min_required={threshold:.4f} | "
            f"{status}"
        )
        self.logger.debug(message)

    def check_custom_exit(
        self,
        symbol: str,
        bars: pd.DataFrame,
        direction: str,
        entry_metadata: Optional[dict] = None
    ) -> Optional[str]:
        """
        Strategy-specific exit logic hook — called by the backtester on every
        bar while a position is open, AFTER the standard SL/TP checks pass.

        Override this in subclasses that have exits beyond SL and TP
        (e.g. EMA cross, BB midline reversion target, RSI threshold).

        Returns an exit reason string if the trade should close on this bar,
        or None to continue holding (standard SL/TP checks remain active).

        Args:
            symbol:         Ticker symbol, e.g. "BTC/USD" or "AAPL"
            bars:           OHLCV DataFrame up to and including the current bar
                            (oldest-first, same orientation as analyze())
            direction:      "long" or "short"
            entry_metadata: The signal.metadata dict captured at entry time.
                            Use to recover mode flags (e.g. adaptive_exit_mode).
        """
        return None

    def verbose_log_skip(self, symbol: str, reason: str):
        """
        Log when a strategy skips analysis entirely (e.g. wrong market condition,
        not enough candle data, strategy disabled).
        """
        if not config.VERBOSE_MODE:
            return

        message = (
            f"[VERBOSE][{self.strategy_name}][{symbol}] "
            f"SKIP: {reason}"
        )
        self.logger.debug(message)

    # =========================================================================
    # UTILITY HELPERS — shared calculations used by multiple strategies
    # =========================================================================

    def _check_enough_candles(self, symbol: str, candles: pd.DataFrame, required: int) -> bool:
        """Return True if there are enough candles; log skip if not."""
        if len(candles) < required:
            self.verbose_log_skip(
                symbol,
                f"Not enough candles: have {len(candles)}, need {required}"
            )
            return False
        return True

    def _make_signal(
        self,
        symbol: str,
        direction: str,
        score: float,
        reason: str,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        metadata: Optional[dict] = None
    ) -> TradeSignal:
        """
        Convenience constructor for TradeSignal using this strategy's defaults.

        Automatically injects the following fields into signal metadata so
        downstream consumers (scanner, ML scorer, reviewer gate) receive them
        without each strategy needing to wire them in manually:

          strategy_name   — mirrors TradeSignal.strategy_name; convenient for
                            dict-only consumers that never touch the dataclass
          ml_exempt       — from self.ml_exempt; scanner skips ML blending when True
          reviewer_exempt — from self.reviewer_exempt; scanner skips Claude
                            reviewer gate when True
          asset_class     — "crypto" if "/" in symbol, else "stock"
                            (the framework uses "/" as the universal crypto separator)
          entry_timeframe — self.crypto_candle_timeframe or self.stock_candle_timeframe
                            matching the inferred asset class; strategy engine may
                            overwrite this with the authoritative tf_key after the
                            signal is returned, but having it here guarantees
                            presence even in paths that bypass the engine stamp

        Strategy-supplied metadata keys always take precedence over auto-injected
        values, so explicit overrides in subclasses work as expected.
        """
        # Infer asset class and matching candle timeframe from symbol format.
        # The framework uses "/" as the universal separator for crypto pairs
        # (e.g. "BTC/USD", "ETH/USDT"); stock symbols never contain "/".
        is_crypto      = "/" in symbol
        asset_class    = "crypto" if is_crypto else "stock"
        entry_tf       = (self.crypto_candle_timeframe if is_crypto
                          else self.stock_candle_timeframe)

        # Build base metadata; strategy-supplied keys overwrite on any overlap.
        base_meta: dict = {
            "strategy_name":   self.strategy_name,
            "ml_exempt":       self.ml_exempt,
            "reviewer_exempt": self.reviewer_exempt,
            "asset_class":     asset_class,
            "entry_timeframe": entry_tf,
        }
        base_meta.update(metadata or {})

        return TradeSignal(
            symbol          = symbol,
            direction       = direction,
            score           = score,
            strategy_name   = self.strategy_name,
            stop_loss_pct   = stop_loss_pct if stop_loss_pct is not None else self.stop_loss_pct,
            take_profit_pct = take_profit_pct if take_profit_pct is not None else self.take_profit_pct,
            reason          = reason,
            metadata        = base_meta,
        )
