"""
=============================================================
  MARKET SCANNER
  Scans stocks and crypto for high-probability setups.
  Routes all signals through the 8-strategy engine.
  Only signals scoring >= min_signal_confidence are forwarded.
=============================================================
"""

import logging
import threading
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from dataclasses import dataclass
try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

# Suppress Alpaca's verbose retry warnings — they spam the log on rate limits.
# We handle rate limiting via time.sleep() between symbols instead.
logging.getLogger("alpaca_trade_api.rest").setLevel(logging.ERROR)

import alpaca_trade_api as tradeapi
import ccxt

import config
from core.inverse_etf_mapper import map_signal, is_leveraged_etf
from core.stop_engine import stop_engine as _stop_engine
from strategies.strategy_engine import strategy_engine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candle Integrity — closed-bar contract (Phase 2)
# ---------------------------------------------------------------------------

# Maps timeframe strings (lower-cased) to their duration in seconds.
# Covers both CCXT style ("5m", "1h") and Alpaca style ("5Min", "1Hour").
_TF_SECONDS: Dict[str, int] = {
    "1m":    60,    "1min":   60,    "1minute":  60,
    "5m":    300,   "5min":   300,   "5minute":  300,
    "15m":   900,   "15min":  900,
    "30m":   1800,  "30min":  1800,
    "1h":    3600,  "1hour":  3600,  "60m":      3600,  "60min":  3600,
    "2h":    7200,  "2hour":  7200,
    "4h":    14400, "4hour":  14400,
    "1d":    86400, "1day":   86400, "daily":    86400,
}


def _tf_to_seconds(timeframe: str) -> int:
    """Return the number of seconds in one bar for the given timeframe string."""
    return _TF_SECONDS.get(timeframe.lower().replace(" ", ""), 300)


def _strip_forming_bar(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Remove the last bar from df if it is still forming (not yet closed).

    A bar starting at T is closed when: now_utc >= T + timeframe_duration.
    Both Kraken CCXT (ms-epoch naive UTC) and Alpaca (tz-aware UTC) are
    handled by normalising the last index timestamp to naive UTC before
    comparing against datetime.utcnow().

    Returns df unchanged when the last bar is already closed.
    Returns df[:-1] when the last bar is still forming, and logs the event.
    """
    if df is None or df.empty:
        return df

    tf_sec = _tf_to_seconds(timeframe)

    try:
        ts = pd.Timestamp(df.index[-1])
        # Normalise to naive UTC
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)

        bar_close = ts + pd.Timedelta(seconds=tf_sec)
        now_utc   = pd.Timestamp(datetime.utcnow())

        if now_utc < bar_close:
            remaining = (bar_close - now_utc).total_seconds()
            logger.debug(
                f"[CANDLE STRIP] Forming bar removed — "
                f"tf={timeframe} started={ts} closes_in={remaining:.0f}s"
            )
            return df.iloc[:-1]

    except Exception as e:
        # On timestamp parse failure, keep df unchanged (conservative choice).
        # Dropping the last bar on failure risks missing the most recent CLOSED
        # bar and silencing a valid signal.  A leaked forming bar is less
        # dangerous for entry logic than a missed closed bar; for exits the
        # same tradeoff applies — false intrabar EMA cross is transient and
        # will self-correct on the next monitor cycle.  Any such events are
        # visible in the debug log.
        logger.debug(f"[CANDLE STRIP] Could not check bar freshness — keeping last bar: {e}")

    return df


@dataclass
class Signal:
    symbol:        str
    asset_class:   str      # "stock" or "crypto"
    direction:     str      # "long" or "short"
    score:         float    # 0.0 – 1.0 confidence
    current_price: float
    indicators:    Dict
    reason:        str


class TechnicalAnalysis:
    """Pure technical indicator calculations. No external dependencies."""

    @staticmethod
    def ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta    = series.diff()
        gain     = delta.clip(lower=0)
        loss     = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
        avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
        rs       = avg_gain / avg_loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(series: pd.Series, fast: int = 12, slow: int = 26,
             signal: int = 9) -> tuple:
        ema_fast    = series.ewm(span=fast,   adjust=False).mean()
        ema_slow    = series.ewm(span=slow,   adjust=False).mean()
        macd_line   = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram   = macd_line - signal_line
        return macd_line, signal_line, histogram

    @staticmethod
    def bollinger_bands(series: pd.Series, period: int = 20,
                        std_dev: float = 2.0) -> tuple:
        sma   = series.rolling(period).mean()
        std   = series.rolling(period).std()
        upper = sma + std_dev * std
        lower = sma - std_dev * std
        pct_b = (series - lower) / (upper - lower).replace(0, float("nan"))
        return upper, lower, pct_b

    @staticmethod
    def vwap(df: pd.DataFrame) -> pd.Series:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        cum_tp_vol    = (typical_price * df["volume"]).cumsum()
        cum_vol       = df["volume"].cumsum()
        return cum_tp_vol / cum_vol.replace(0, float("nan"))

    @staticmethod
    def obv(df: pd.DataFrame) -> pd.Series:
        direction = df["close"].diff().apply(
            lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
        )
        return (direction * df["volume"]).cumsum()

    @staticmethod
    def stochastic(df: pd.DataFrame, k_period: int = 14,
                   d_period: int = 3) -> tuple:
        low_min  = df["low"].rolling(k_period).min()
        high_max = df["high"].rolling(k_period).max()
        k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, float("nan"))
        d = k.rolling(d_period).mean()
        return k, d


class StockScanner:

    def __init__(self):
        self.api = tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL
        )
        # Increase the HTTP connection pool size so concurrent callers
        # (scan thread + position monitor thread) don't exhaust the default
        # pool of 10 and spam "Connection pool is full, discarding connection"
        # warnings every 30-60s.  pool_maxsize=30 covers the stock watchlist
        # size plus a margin for simultaneous calls from other threads.
        try:
            from requests.adapters import HTTPAdapter
            _adapter = HTTPAdapter(pool_connections=10, pool_maxsize=30)
            self.api._session.mount("https://", _adapter)
            self.api._session.mount("http://",  _adapter)
        except Exception:
            pass  # defensive — don't break on unexpected alpaca_trade_api changes
        self.ta = TechnicalAnalysis()
        # Candle integrity: tracks the last closed-bar timestamp evaluated
        # per symbol+timeframe so we never re-run strategies against the same bar.
        # Key: "SYMBOL_TF"  Value: str(bars.index[-1])
        self._last_bar_evaluated: Dict[str, str] = {}

    def get_bars(self, symbol: str, timeframe: str = "5Min",
                 limit: int = 100) -> Optional[pd.DataFrame]:
        # Try CandleManager cache first
        try:
            from core.candle_manager import candle_manager
            df = candle_manager.get(symbol, timeframe, limit=limit)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass

        # Fallback: direct Alpaca call
        try:
            bars = self.api.get_bars(
                symbol, timeframe,
                limit=limit + 1, adjustment="raw"
            ).df
            if bars.empty:
                return None
            bars.columns = [c.lower() for c in bars.columns]
            bars = bars.rename(columns={"vwap": "vwap_raw"}) if "vwap" in bars.columns else bars
            bars = _strip_forming_bar(bars, timeframe)
            return bars if bars is not None and not bars.empty else None
        except Exception as e:
            logger.debug(f"Bars fetch failed for {symbol}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            trade = self.api.get_latest_trade(symbol)
            return float(trade.price)
        except Exception:
            try:
                bars = self.get_bars(symbol, "1Min", 1)
                if bars is not None and not bars.empty:
                    return float(bars["close"].iloc[-1])
            except Exception:
                pass
        return None

    def scan(self) -> List[Signal]:
        """
        Scan all stocks in the watchlist.
        Fetches candles once per timeframe per symbol and reuses them
        across all strategies that share the same timeframe.
        This avoids redundant API calls while giving each strategy
        the candle resolution it was designed and backtested for.
        """
        # Build timeframe → strategies map once (only enabled stock strategies)
        tf_groups: dict = {}
        for strategy in strategy_engine.get_all_strategies():
            if not strategy.enabled:
                continue
            if not getattr(strategy, "stock_enabled", True):
                continue
            tf = getattr(strategy, "stock_candle_timeframe", "5Min")
            tf_groups.setdefault(tf, [])
            if strategy not in tf_groups[tf]:
                tf_groups[tf].append(strategy)

        signals = []
        for symbol in config.STOCK_WATCHLIST:
            try:
                price = self.get_current_price(symbol)
                if not price:
                    continue
                if not (config.STOCK_MIN_PRICE <= price <= config.STOCK_MAX_PRICE):
                    continue

                # ── Fetch bars once per unique timeframe ─────────────────────
                # Skip a timeframe if its last closed bar timestamp is unchanged
                # from the previous scan cycle (same bar → same signal, no point).
                #
                # FIX: provisional_evals accumulates what we intend to record but
                # does NOT write to _last_bar_evaluated until AFTER strategy_engine
                # returns successfully.  Previously the timestamp was committed
                # before execution, so a strategy crash or process kill could mark
                # a bar "already evaluated" that was never actually processed.
                #
                # RESTART NOTE: _last_bar_evaluated is in-memory only.  After a
                # restart the most recent closed bar will be re-evaluated once per
                # symbol/timeframe.  The duplicate-signal risk is mitigated by the
                # open-position gate in _process_signals (open_symbols check).
                # Persisting to DB is a future improvement tracked in CLAUDE.md.
                bar_cache: dict        = {}
                provisional_evals: dict = {}   # commit to _last_bar_evaluated after success

                for tf in tf_groups:
                    limit = max(
                        getattr(s, "candle_limit", 100)
                        for s in tf_groups[tf]
                    )
                    bars = self.get_bars(symbol, tf, limit)
                    if bars is not None and len(bars) >= 20:
                        last_bar_ts = str(bars.index[-1])
                        cache_key   = f"{symbol}_{tf}"
                        if self._last_bar_evaluated.get(cache_key) == last_bar_ts:
                            logger.debug(
                                f"[CANDLE SKIP] {symbol} {tf}: "
                                f"closed bar {last_bar_ts} already evaluated"
                            )
                            continue
                        provisional_evals[cache_key] = last_bar_ts  # tentative
                        bar_cache[tf] = bars

                if not bar_cache:
                    continue

                # Run strategies against their correct candle timeframe
                strategy_signals = strategy_engine.run_strategies_multi_tf(
                    symbol, "stock", bar_cache, price
                )

                # Commit evaluated timestamps only after successful strategy run
                for cache_key, ts in provisional_evals.items():
                    self._last_bar_evaluated[cache_key] = ts

                for ss in strategy_signals:
                    # ── Structural initial stop ───────────────────────────────
                    # strategy_engine.run_strategies_multi_tf() always embeds
                    # indicators["timeframe"] = strategy.stock_candle_timeframe,
                    # so we can use it directly without any guessing.
                    #
                    # Safe fallback: if for any reason the key is absent or maps
                    # to a timeframe not in bar_cache, skip the structural stop
                    # rather than falling back to list(bar_cache.keys())[0], which
                    # could embed a stop computed from the wrong bar resolution.
                    tf_key = ss.indicators.get("timeframe")
                    if tf_key and tf_key not in bar_cache:
                        logger.warning(
                            f"[STOP EMBED] {symbol}: strategy timeframe {tf_key!r} "
                            f"not in bar_cache — no structural stop embedded"
                        )
                        tf_key = None
                    _bars_for_stop = bar_cache.get(tf_key) if tf_key else None
                    if _bars_for_stop is not None and len(_bars_for_stop) >= 3:
                        try:
                            ss.indicators["structural_stop_price"] = round(
                                _stop_engine.initial_stop_from_tail(
                                    _bars_for_stop, ss.direction
                                ), 6
                            )
                        except Exception:
                            pass

                    signals.append(Signal(
                        symbol        = ss.symbol,
                        asset_class   = ss.asset_class,
                        direction     = ss.direction,
                        score         = ss.score,
                        current_price = ss.current_price,
                        indicators    = ss.indicators,
                        reason        = ss.reason,
                    ))

                # Delay between symbols to respect Alpaca rate limits.
                # With 137 stocks × 2 timeframes + price = ~400 API calls per scan.
                # 0.4s gives ~150 requests/min, well under Alpaca's 200/min limit.
                time.sleep(0.4)

            except Exception as e:
                logger.debug(f"Stock scan error {symbol}: {e}")
        return signals


# Stablecoins and pegged tokens that should never be traded
# Grid_bot requires 1.5%+ movement — stablecoins are designed to NOT move
CRYPTO_BLACKLIST = {
    "USDT/USD", "USDC/USD", "DAI/USD", "BUSD/USD", "TUSD/USD",
    "USDS/USD", "USDE/USD", "FDUSD/USD", "USDP/USD", "FRAX/USD",
    "LUSD/USD", "PYUSD/USD", "USDD/USD", "GUSD/USD", "SUSD/USD",
    "PAXG/USD", "XAUT/USD",  # gold-backed, near-zero volatility
    "STETH/USD", "CBETH/USD", "RETH/USD", "WSTETH/USD",  # ETH-pegged
    "WBTC/USD",  # BTC-pegged
}


# ---------------------------------------------------------------------------
# CoinGecko — real-time aggregated price lookup (no API key required)
# Maps base currency of a watchlist symbol to its CoinGecko coin ID.
# ---------------------------------------------------------------------------
_COINGECKO_IDS: Dict[str, str] = {
    "BTC":   "bitcoin",       "ETH":   "ethereum",
    "SOL":   "solana",        "ADA":   "cardano",
    "MATIC": "matic-network", "DOT":   "polkadot",
    "AVAX":  "avalanche-2",   "LINK":  "chainlink",
    "LTC":   "litecoin",      "XRP":   "ripple",
    "DOGE":  "dogecoin",      "TRX":   "tron",
    "UNI":   "uniswap",       "ATOM":  "cosmos",
    "XLM":   "stellar",       "ALGO":  "algorand",
    "NEAR":  "near",          "FTM":   "fantom",
    "SAND":  "the-sandbox",   "MANA":  "decentraland",
    "SHIB":  "shiba-inu",     "PEPE":  "pepe",
    "ARB":   "arbitrum",      "OP":    "optimism",
}

_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


def _coingecko_price(symbol: str) -> Optional[float]:
    """Return current USD price from CoinGecko for a /USD watchlist symbol.
    Returns None on any error so callers can fall back to ccxt."""
    if not _REQUESTS_OK:
        return None
    try:
        base  = symbol.split("/")[0].upper()
        cg_id = _COINGECKO_IDS.get(base, base.lower())
        resp  = _requests.get(
            _COINGECKO_URL,
            params={"ids": cg_id, "vs_currencies": "usd"},
            timeout=5,
        )
        price = resp.json().get(cg_id, {}).get("usd")
        return float(price) if price else None
    except Exception:
        return None


class CryptoScanner:

    def __init__(self):
        self.exchange = ccxt.kraken({
            "apiKey":          config.KRAKEN_API_KEY,
            "secret":          config.KRAKEN_SECRET_KEY,
            "enableRateLimit": True,
            "timeout":         10000,   # 10 second hard timeout — prevents scan hangs
        })
        self.ta = TechnicalAnalysis()
        # Candle integrity: tracks the last closed-bar timestamp evaluated
        # per symbol+timeframe so we never re-run strategies against the same bar.
        # Key: "SYMBOL_TF"  Value: str(df.index[-1])
        self._last_bar_evaluated: Dict[str, str] = {}

    def get_ohlcv(self, symbol: str, timeframe: str = "5m",
                  limit: int = 100) -> Optional[pd.DataFrame]:
        # Try CandleManager cache first — avoids redundant CCXT calls and
        # eliminates the thread-safety issue (only one thread ever calls ccxt).
        try:
            from core.candle_manager import candle_manager
            df = candle_manager.get(symbol, timeframe, limit=limit)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass

        # Fallback: direct CCXT call (candle_manager unavailable or not yet warmed)
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit + 1)
            if not ohlcv:
                return None
            df = pd.DataFrame(
                ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            df = _strip_forming_bar(df, timeframe)
            return df if df is not None and not df.empty else None
        except Exception as e:
            logger.debug(f"OHLCV fetch failed for {symbol}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        price, _ = self.get_current_price_with_broker(symbol)
        return price

    def get_current_price_with_broker(self, symbol: str):
        # CoinGecko first — aggregated real-time price, no API key, no geo-block.
        # Eliminates stale last-trade prices on low-liquidity Kraken pairs.
        cg = _coingecko_price(symbol)
        if cg:
            return cg, "COINGECKO"

        # Fallback: ccxt exchange(s)
        named = getattr(self, "exchanges", {})
        pairs = list(named.items())
        if self.exchange and self.exchange not in named.values():
            pairs = [(getattr(self, "exchange_name", "KRAKEN"), self.exchange)] + pairs
        for name, exc in pairs:
            try:
                ticker = exc.fetch_ticker(symbol)
                price  = ticker.get("last") or ticker.get("close")
                if price and float(price) > 0:
                    return float(price), name
            except Exception:
                continue
        return None, None

    def scan(self) -> List[Signal]:
        """
        Scan all crypto pairs in the watchlist.
        Fetches candles once per timeframe per symbol and reuses them
        across all strategies that share the same timeframe.
        """
        # Build timeframe → strategies map once (only enabled crypto strategies)
        tf_groups: dict = {}
        for strategy in strategy_engine.get_all_strategies():
            if not strategy.enabled:
                continue
            if not getattr(strategy, "crypto_enabled", True):
                continue
            tf = getattr(strategy, "crypto_candle_timeframe", "1h")
            tf_groups.setdefault(tf, [])
            if strategy not in tf_groups[tf]:
                tf_groups[tf].append(strategy)

        signals = []
        for symbol in config.CRYPTO_WATCHLIST:
            try:
                # Skip stablecoins and pegged tokens — they can't generate
                # the 1.5%+ moves needed for profitable grid_bot trades
                if symbol in CRYPTO_BLACKLIST:
                    logger.debug(f"Skipping blacklisted symbol: {symbol}")
                    continue

                price = self.get_current_price(symbol)
                if not price:
                    continue
                if not (config.CRYPTO_MIN_PRICE <= price <= config.CRYPTO_MAX_PRICE):
                    continue

                # ── Fetch bars once per unique timeframe ─────────────────────
                # See StockScanner.scan() for full explanation of the
                # provisional_evals pattern (fix for premature timestamp commit).
                bar_cache: dict        = {}
                provisional_evals: dict = {}

                for tf in tf_groups:
                    limit = max(
                        getattr(s, "candle_limit", 100)
                        for s in tf_groups[tf]
                    )
                    bars = self.get_ohlcv(symbol, tf, limit)
                    if bars is not None and len(bars) >= 20:
                        last_bar_ts = str(bars.index[-1])
                        cache_key   = f"{symbol}_{tf}"
                        if self._last_bar_evaluated.get(cache_key) == last_bar_ts:
                            logger.debug(
                                f"[CANDLE SKIP] {symbol} {tf}: "
                                f"closed bar {last_bar_ts} already evaluated"
                            )
                            continue
                        provisional_evals[cache_key] = last_bar_ts
                        bar_cache[tf] = bars

                if not bar_cache:
                    continue

                # Run strategies against their correct candle timeframe
                strategy_signals = strategy_engine.run_strategies_multi_tf(
                    symbol, "crypto", bar_cache, price
                )

                # Commit evaluated timestamps only after successful strategy run
                for cache_key, ts in provisional_evals.items():
                    self._last_bar_evaluated[cache_key] = ts

                for ss in strategy_signals:
                    # ── Structural initial stop ───────────────────────────────
                    # strategy_engine always embeds indicators["timeframe"].
                    # Use it directly; safe fallback skips stop embed rather
                    # than guessing with list(bar_cache.keys())[0].
                    tf_key = ss.indicators.get("timeframe")
                    if tf_key and tf_key not in bar_cache:
                        logger.warning(
                            f"[STOP EMBED] {symbol}: strategy timeframe {tf_key!r} "
                            f"not in bar_cache — no structural stop embedded"
                        )
                        tf_key = None
                    _bars_for_stop = bar_cache.get(tf_key) if tf_key else None
                    if _bars_for_stop is not None and len(_bars_for_stop) >= 3:
                        try:
                            ss.indicators["structural_stop_price"] = round(
                                _stop_engine.initial_stop_from_tail(
                                    _bars_for_stop, ss.direction
                                ), 6
                            )
                        except Exception:
                            pass
                    signals.append(Signal(
                        symbol        = ss.symbol,
                        asset_class   = ss.asset_class,
                        direction     = ss.direction,
                        score         = ss.score,
                        current_price = ss.current_price,
                        indicators    = ss.indicators,
                        reason        = ss.reason,
                    ))
            except Exception as e:
                logger.debug(f"Crypto scan error {symbol}: {e}")
        return signals


WIN_REENTRY_DELAY_SEC = 5


class MarketScanner:

    def __init__(self):
        self.stock_scanner  = StockScanner()
        self.crypto_scanner = CryptoScanner()
        self._receiver_ref             = None
        self._pending_reentry: set     = set()
        self._pending_lock             = threading.Lock()

    def set_reentry_receiver(self, receiver) -> None:
        self._receiver_ref = receiver

    def schedule_reentry(
        self, symbol: str, exit_price: float, direction: str
    ) -> None:
        with self._pending_lock:
            if symbol in self._pending_reentry:
                logger.info(f"[REENTRY] {symbol}: already pending — skipped duplicate")
                return
            self._pending_reentry.add(symbol)
        t = threading.Timer(
            WIN_REENTRY_DELAY_SEC,
            self._safe_reentry_scan,
            args=[symbol, exit_price, direction],
        )
        t.daemon = True
        t.start()
        logger.info(
            f"[REENTRY] {symbol} {direction} re-entry scan "
            f"scheduled in {WIN_REENTRY_DELAY_SEC}s"
        )

    def _safe_reentry_scan(
        self, symbol: str, exit_price: float, direction: str
    ) -> None:
        with self._pending_lock:
            self._pending_reentry.discard(symbol)
        try:
            self._do_reentry_scan(symbol, exit_price, direction)
        except Exception as e:
            logger.error(f"[REENTRY] {symbol}: scan failed: {e}", exc_info=True)

    def _do_reentry_scan(
        self, symbol: str, exit_price: float, direction: str
    ) -> None:
        if not self._receiver_ref:
            logger.warning("[REENTRY] No receiver wired — skipping")
            return

        asset_class   = "crypto" if "/" in symbol else "stock"
        current_price = self.get_current_price(symbol, asset_class)

        if not current_price:
            logger.info(f"[REENTRY] {symbol}: could not fetch live price — aborting")
            return

        drift_pct = ((current_price - exit_price) / exit_price) * 100

        if direction == "long"  and drift_pct < -0.3:
            logger.info(f"[REENTRY] {symbol}: reversed {drift_pct:.2f}% since exit — skip")
            return
        if direction == "short" and drift_pct >  0.3:
            logger.info(f"[REENTRY] {symbol}: reversed {drift_pct:+.2f}% since exit — skip")
            return

        payload = {
            "symbol":                      symbol,
            "asset_class":                 asset_class,
            "direction":                   direction,
            "entry_price":                 current_price,
            "current_price":               current_price,
            "move_pct":                    round(drift_pct, 4),
            "volume_spike":                1.0,
            "confidence":                  0.70,
            "escalation":                  1,
            "timestamp":                   datetime.now(timezone.utc).isoformat(),
            "broker":                      "kraken" if asset_class == "crypto" else "alpaca",
            "signal_source":               "breakout_scanner",
            "bypass_win_cooldown":         True,
            "bars_since_breakout":         1,
            "distance_from_breakout_pct":  abs(drift_pct),
        }

        logger.info(
            f"[REENTRY] Injecting {symbol} {direction} @ ${current_price:.4f} "
            f"(exited @ ${exit_price:.4f}, drift={drift_pct:+.2f}%)"
        )

        result   = self._receiver_ref.receive_signal(payload)
        accepted = result.get("accepted", False)
        logger.info(
            f"[REENTRY] {symbol}: "
            + (
                f"ACCEPTED -> trade_id={result.get('trade_id')}"
                if accepted
                else f"REJECTED — {result.get('reason', '?')}"
            )
        )

    def _get_executor(self):
        from core.trade_executor import executor
        return executor

    def _get_ml_scorer(self):
        from intelligence.ml_scorer import ml_scorer
        return ml_scorer

    def _get_condition_detector(self):
        from intelligence.condition_detector import condition_detector
        return condition_detector

    def scan_stocks(self):
        signals = self.stock_scanner.scan()
        if signals:
            self._process_signals(signals)

    def scan_crypto(self):
        signals = self.crypto_scanner.scan()
        if signals:
            self._process_signals(signals)

    def _review_signal_advisory_async(
        self,
        signal: Signal,
        trade_id: Optional[str] = None,
        market_context=None,
    ) -> None:
        """Log Claude/Haiku review in the background without delaying execution."""
        def _worker():
            try:
                from intelligence.claude_reviewer import claude_reviewer
                context = market_context or claude_reviewer.get_morning_context()
                decision = claude_reviewer.review_signal(signal, context)
                signal.indicators["claude_review_decision"] = decision.decision
                signal.indicators["claude_review_confidence"] = decision.confidence
                signal.indicators["claude_review_reasoning"] = decision.reasoning
                signal.indicators["claude_review_elapsed_ms"] = decision.elapsed_ms
                if decision.warnings:
                    signal.indicators["claude_review_warnings"] = list(decision.warnings)
                try:
                    from data.database import db
                    db.record_ai_signal_review({
                        "trade_id":            trade_id,
                        "symbol":              signal.symbol,
                        "asset_class":         signal.asset_class,
                        "direction":           signal.direction,
                        "strategy_name":       signal.indicators.get("strategy_name", "unknown"),
                        "signal_score":        signal.score,
                        "reviewer":            "claude",
                        "mode":                "advisory",
                        "decision":            decision.decision,
                        "confidence":          decision.confidence,
                        "reasoning":           decision.reasoning,
                        "suggested_size_pct":  decision.suggested_size_pct,
                        "warnings":            decision.warnings,
                        "elapsed_ms":          decision.elapsed_ms,
                    })
                except Exception as db_err:
                    logger.warning(
                        f"Claude reviewer advisory DB log failed for "
                        f"{signal.symbol}: {db_err}"
                    )
                logger.info(
                    f"Claude reviewer advisory: {decision.decision} {signal.symbol} "
                    f"(confidence={decision.confidence}) — {decision.reasoning}"
                )
            except Exception as e:
                logger.warning(
                    f"Claude reviewer advisory error for {signal.symbol}: {e} — continuing"
                )

        threading.Thread(
            target=_worker,
            name=f"claude-review-{signal.symbol}",
            daemon=True,
        ).start()

    def _process_signals(self, signals: List[Signal]):
        if not signals:
            return

        # Determine asset class FIRST — before any gate checks
        # is_crypto must be known before the morning briefing gate
        is_crypto = all(s.asset_class == "crypto" for s in signals)

        # ── Claude morning briefing ───────────────────────────────────────
        # Get or refresh market context — cached 120 min, conservative on failure.
        #
        # CRITICAL: allow_trading=False only blocks STOCKS.
        # Crypto trades 24/7 — the morning briefing gate is irrelevant for crypto.
        # Crypto signals bypass this gate and go straight to per-signal review.
        #
        # FALLBACK: if reviewer unavailable, market_context = None and signals
        # proceed through mechanical execution only (no Claude veto).
        market_context = None
        reviewer_enabled = False
        reviewer_mode = str(
            getattr(config, "CLAUDE_REVIEWER_MODE", "advisory")
        ).strip().lower()
        reviewer_strict = reviewer_mode in ("strict", "veto", "hard_veto")
        try:
            from intelligence.claude_reviewer import claude_reviewer
            reviewer_enabled = claude_reviewer.is_enabled()
            if reviewer_enabled and reviewer_strict:
                market_context = claude_reviewer.get_morning_context()
                if not market_context.allow_trading and not is_crypto:
                    if reviewer_strict:
                        # Stock trading blocked by morning briefing — return early
                        logger.info(
                            f"Claude reviewer: stock trading blocked — "
                            f"{market_context.briefing_text[:120]}"
                        )
                        return
                    logger.info(
                        f"Claude reviewer advisory: stock context says block — "
                        f"{market_context.briefing_text[:120]} — continuing"
                    )
                elif not market_context.allow_trading and is_crypto:
                    # Crypto bypasses the allow_trading gate — log and continue
                    logger.debug(
                        "Claude reviewer: allow_trading=False but this is a crypto scan "
                        "— bypassing market hours gate, proceeding to signal review"
                    )
        except Exception as e:
            logger.warning(
                f"Claude reviewer unavailable: {e} — "
                f"proceeding with mechanical execution only"
            )
            market_context = None

        condition_value = "unknown"
        position_scalar = 1.0
        adx             = 25

        if not is_crypto:
            try:
                market_condition = self._get_condition_detector().get_spy_condition(
                    self.stock_scanner
                )
                if not market_condition.should_trade:
                    logger.warning(
                        f"Market condition says skip stocks: {market_condition.reason}"
                    )
                    return
                condition_value = market_condition.condition.value
                position_scalar = market_condition.position_scalar
                adx             = market_condition.adx
                logger.info(
                    f"Market condition: {condition_value} | "
                    f"ADX={adx} | scalar={position_scalar:.0%}"
                )
            except Exception as e:
                logger.warning(f"Condition detector error: {e} — using defaults")
        else:
            try:
                market_condition = self._get_condition_detector().get_spy_condition(
                    self.stock_scanner
                )
                condition_value = market_condition.condition.value
                adx             = market_condition.adx
                position_scalar = market_condition.position_scalar
            except Exception:
                pass

        # Deduplicate: one signal per symbol, highest score wins
        by_symbol: Dict[str, Signal] = {}
        for sig in signals:
            existing = by_symbol.get(sig.symbol)
            if existing is None or sig.score > existing.score:
                by_symbol[sig.symbol] = sig
        unique_signals = list(by_symbol.values())

        min_confidence = config.SIGNAL_TUNING.get(
            "min_signal_confidence", config.MIN_SIGNAL_CONFIDENCE
        )

        for signal in unique_signals:
            ml_exempt = signal.indicators.get("ml_exempt", False)

            if ml_exempt:
                logger.debug(
                    f"[ML-EXEMPT] {signal.symbol} [{signal.indicators.get('strategy_name', '?')}] "
                    f"— keeping raw score {signal.score:.3f} (ML blending skipped)"
                )
            else:
                try:
                    enhanced = self._get_ml_scorer().score(
                        indicators    = signal.indicators,
                        base_score    = signal.score,
                        condition_adx = adx
                    )
                    signal.score = round(enhanced, 3)
                except Exception:
                    pass

            signal.indicators["market_condition"] = condition_value
            signal.indicators["condition_scalar"] = position_scalar

        final = [s for s in unique_signals if s.score >= min_confidence]
        final.sort(key=lambda x: x.score, reverse=True)

        logger.info(f"Signals after ML/filter: {len(final)}")

        executor     = self._get_executor()
        from data.database import db
        open_trades  = db.get_open_trades()
        open_symbols = set(t["symbol"] for t in open_trades)

        for signal in final:
            if signal.symbol in open_symbols:
                logger.info(
                    f"Skipping {signal.symbol} — already have an open position"
                )
                continue

            # ── Claude per-signal review ──────────────────────────────────
            # Advisory mode runs in a background thread and builds its own
            # context so trade execution is not delayed.
            # Skipped for reviewer_exempt strategies (e.g. Grid Bot) which use
            # non-standard indicators that Claude can't evaluate meaningfully.
            # Default mode is advisory: log Claude's opinion, but do not veto.
            # Set config.CLAUDE_REVIEWER_MODE="strict" to restore hard vetoes.
            reviewer_exempt = signal.indicators.get("reviewer_exempt", False)

            if market_context is not None and not reviewer_exempt and reviewer_strict:
                try:
                    from intelligence.claude_reviewer import claude_reviewer
                    decision = claude_reviewer.review_signal(signal, market_context)
                    signal.indicators["claude_review_decision"] = decision.decision
                    signal.indicators["claude_review_confidence"] = decision.confidence
                    signal.indicators["claude_review_reasoning"] = decision.reasoning
                    signal.indicators["claude_review_elapsed_ms"] = decision.elapsed_ms
                    if decision.warnings:
                        signal.indicators["claude_review_warnings"] = list(decision.warnings)

                    if decision.decision != "APPROVE":
                        logger.info(
                            f"Claude reviewer: {decision.decision} {signal.symbol} — "
                            f"{decision.reasoning} "
                            f"({'vetoing' if reviewer_strict else 'advisory only, continuing'})"
                        )
                        if reviewer_strict:
                            continue
                    if (
                        decision.suggested_size_pct
                        and getattr(config, "CLAUDE_REVIEWER_APPLY_SIZE_ADVICE", False)
                    ):
                        signal.indicators["condition_scalar"] = (
                            decision.suggested_size_pct / config.MAX_POSITION_PCT
                        )
                    logger.info(
                        f"Claude reviewer: {decision.decision} {signal.symbol} "
                        f"(confidence={decision.confidence}) — {decision.reasoning}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Claude reviewer error for {signal.symbol}: {e} "
                        f"— {'SKIPPING signal' if reviewer_strict else 'advisory only, continuing'}"
                    )
                    signal.indicators["claude_review_decision"] = "ERROR"
                    signal.indicators["claude_review_reasoning"] = str(e)
                    if reviewer_strict:
                        continue

            # ── Execute signal ────────────────────────────────────────────
            try:
                trade_id = executor.execute_signal(signal)
                if trade_id:
                    open_symbols.add(signal.symbol)
                    if reviewer_enabled and not reviewer_exempt and not reviewer_strict:
                        self._review_signal_advisory_async(signal, trade_id=trade_id)
            except Exception as e:
                logger.error(f"Executor error for {signal.symbol}: {e}")

    def scan_all(self) -> List[Signal]:
        return self.stock_scanner.scan() + self.crypto_scanner.scan()

    def get_current_price(self, symbol: str, asset_class: str) -> Optional[float]:
        try:
            if asset_class == "stock":
                return self.stock_scanner.get_current_price(symbol)
            elif asset_class == "crypto":
                return self.crypto_scanner.get_current_price(symbol)
        except Exception as e:
            logger.error(f"Price lookup failed for {symbol}: {e}")
        return None


# Singleton
scanner = MarketScanner()
