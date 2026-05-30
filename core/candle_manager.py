"""
=============================================================
  CANDLE MANAGER  v1.0
  Unified candle data layer for the entire trading system.

  Replaces scattered yfinance/CCXT/Alpaca calls across:
    - scanners/market_scanner.py
    - core/position_monitor.py
    - logs/intelligence/backtester.py
    - logs/intelligence/optimizer.py
    - breakout_receiver.py
    - web_dashboard.py

  Architecture
  ------------
  1. CandleStore SQLite (existing) — persistent OHLCV cache
  2. symbol_source_registry table — which source works for each symbol
  3. In-memory registry dict — loaded at boot, updated on fetch
  4. 5-minute refresh loop — background daemon pre-fetches all watchlist symbols
  5. Multi-source fallback — CCXT→yfinance (crypto), Alpaca→yfinance (stocks)

  Consumer interface
  ------------------
    from core.candle_manager import candle_manager
    df = candle_manager.get("BTC/USD", "1h", limit=200)
    df = candle_manager.get("AAPL", "5Min", limit=100)

  Threading
  ---------
  Only the refresh loop ever calls external APIs. All consumers read
  from the SQLite cache via CandleStore (WAL mode, safe for concurrent reads).
  Eliminates the ccxt thread-safety issue in position_monitor.
=============================================================
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timeframe helpers
# ---------------------------------------------------------------------------

# Map between different API timeframe formats
_TF_CCXT_TO_YF  = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h","4h":"4h","1d":"1d"}
_TF_ALPACA_TO_YF = {"1Min":"1m","5Min":"5m","15Min":"15m","1Hour":"1h","1Day":"1d"}
_TF_BARS_PER_DAY = {"1m":1440,"5m":288,"15m":96,"1h":24,"4h":6,"1d":1,
                    "1Min":1440,"5Min":288,"15Min":96,"1Hour":24,"1Day":1}

def _bars_to_days(limit: int, timeframe: str) -> int:
    """Convert a bar limit to approximate number of days needed."""
    bpd = _TF_BARS_PER_DAY.get(timeframe, 24)
    return max(2, int(limit / bpd) + 2)

def _normalise_tf(timeframe: str) -> str:
    """Normalise timeframe to CCXT/yfinance format (e.g. '5Min' → '5m')."""
    mapping = {
        "1Min":"1m","5Min":"5m","15Min":"15m","30Min":"30m",
        "1Hour":"1h","4Hour":"4h","1Day":"1d",
        "1H":"1h","1D":"1d",
    }
    return mapping.get(timeframe, timeframe)

def _alpaca_tf(timeframe: str) -> str:
    """Convert any timeframe to Alpaca format."""
    mapping = {
        "1m":"1Min","5m":"5Min","15m":"15Min","30m":"30Min",
        "1h":"1Hour","4h":"4Hour","1d":"1Day",
    }
    return mapping.get(timeframe, timeframe)


# ---------------------------------------------------------------------------
# Source constants
# ---------------------------------------------------------------------------
SRC_KRAKEN  = "kraken"
SRC_ALPACA  = "alpaca"
SRC_YFINANCE= "yfinance"

_CRYPTO_SOURCES = [SRC_KRAKEN, SRC_YFINANCE]
_STOCK_SOURCES  = [SRC_ALPACA,  SRC_YFINANCE]


# ---------------------------------------------------------------------------
# CandleManager
# ---------------------------------------------------------------------------

class CandleManager:
    """
    Single source of truth for OHLCV data.
    All consumers call get() — cache hit returns instantly,
    cache miss triggers a multi-source fetch and caches the result.
    """

    def __init__(self):
        # Import CandleStore (lives in logs/intelligence — path added to sys.path by bot)
        try:
            from intelligence.candle_store import get_store
            self._store = get_store()
        except ImportError:
            logger.warning("CandleStore unavailable — candle_manager running without persistence")
            self._store = None

        # In-memory source registry: "SYMBOL|asset_class" → preferred_source
        self._registry: Dict[str, str] = {}
        self._registry_lock = threading.Lock()

        self._init_registry_table()
        self._load_registry()

        # Refresh loop state
        self._refresh_thread: Optional[threading.Thread] = None
        self._refresh_stop   = threading.Event()
        self._refresh_symbols: List[Tuple[str, str]] = []  # [(symbol, asset_class)]

        # Unreachable log path
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._unreachable_log = os.path.join(_root, "logs", "unreachable_symbols.log")

        logger.info("CandleManager initialised")

    # -----------------------------------------------------------------------
    # Registry DB
    # -----------------------------------------------------------------------

    def _init_registry_table(self):
        if self._store is None:
            return
        try:
            with self._store._conn() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS symbol_source_registry (
                        symbol           TEXT NOT NULL,
                        asset_class      TEXT NOT NULL,
                        preferred_source TEXT,
                        last_success_ts  INTEGER,
                        last_fail_ts     INTEGER,
                        fail_count       INTEGER DEFAULT 0,
                        PRIMARY KEY (symbol, asset_class)
                    )
                """)
        except Exception as e:
            logger.warning(f"CandleManager: registry table init failed: {e}")

    def _load_registry(self):
        if self._store is None:
            return
        try:
            with self._store._conn() as conn:
                rows = conn.execute(
                    "SELECT symbol, asset_class, preferred_source FROM symbol_source_registry"
                ).fetchall()
            with self._registry_lock:
                for symbol, asset_class, preferred in rows:
                    if preferred:
                        self._registry[f"{symbol}|{asset_class}"] = preferred
            logger.info(f"CandleManager: loaded {len(rows)} symbol sources from registry")
        except Exception as e:
            logger.warning(f"CandleManager: registry load failed: {e}")

    def _update_registry(self, symbol: str, asset_class: str, source: str, success: bool):
        """Update both in-memory dict and DB row."""
        key = f"{symbol}|{asset_class}"
        now = int(datetime.utcnow().timestamp())

        with self._registry_lock:
            if success:
                self._registry[key] = source

        if self._store is None:
            return
        try:
            with self._store._conn() as conn:
                if success:
                    conn.execute("""
                        INSERT INTO symbol_source_registry
                            (symbol, asset_class, preferred_source, last_success_ts, fail_count)
                        VALUES (?, ?, ?, ?, 0)
                        ON CONFLICT(symbol, asset_class) DO UPDATE SET
                            preferred_source = excluded.preferred_source,
                            last_success_ts  = excluded.last_success_ts,
                            fail_count       = 0
                    """, (symbol, asset_class, source, now))
                else:
                    conn.execute("""
                        INSERT INTO symbol_source_registry
                            (symbol, asset_class, last_fail_ts, fail_count)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(symbol, asset_class) DO UPDATE SET
                            last_fail_ts = excluded.last_fail_ts,
                            fail_count   = fail_count + 1
                    """, (symbol, asset_class, now))
        except Exception as e:
            logger.debug(f"CandleManager: registry update failed for {symbol}: {e}")

    def _get_source_order(self, symbol: str, asset_class: str) -> List[str]:
        """Return sources in preferred order — try last-known-good first."""
        key = f"{symbol}|{asset_class}"
        with self._registry_lock:
            preferred = self._registry.get(key)

        defaults = _CRYPTO_SOURCES if asset_class == "crypto" else _STOCK_SOURCES
        if preferred and preferred in defaults:
            rest = [s for s in defaults if s != preferred]
            return [preferred] + rest
        return defaults

    # -----------------------------------------------------------------------
    # Fetchers (one per source)
    # -----------------------------------------------------------------------

    def _fetch_kraken(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        """Fetch from Kraken via CCXT."""
        try:
            import ccxt
            import config
            tf = _normalise_tf(timeframe)
            ex = ccxt.kraken({
                "apiKey":          config.KRAKEN_API_KEY,
                "secret":          config.KRAKEN_SECRET_KEY,
                "enableRateLimit": True,
                "timeout":         10000,
            })
            ohlcv = ex.fetch_ohlcv(symbol, tf, limit=limit + 1)
            if not ohlcv:
                return None
            df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_localize(None)
            df.set_index("timestamp", inplace=True)
            if len(df) > 1:
                df = df.iloc[:-1]   # drop forming bar
            return df if not df.empty else None
        except Exception as e:
            logger.debug(f"[CandleManager] Kraken fetch failed for {symbol}/{timeframe}: {e}")
            return None

    def _fetch_alpaca(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        """Fetch from Alpaca."""
        try:
            import alpaca_trade_api as tradeapi
            import config
            api = tradeapi.REST(
                config.ALPACA_API_KEY,
                config.ALPACA_SECRET_KEY,
                base_url=config.ALPACA_BASE_URL
            )
            tf_alpaca = _alpaca_tf(_normalise_tf(timeframe))
            bars = api.get_bars(symbol, tf_alpaca, limit=limit + 1, adjustment="raw").df
            if bars.empty:
                return None
            bars.columns = [c.lower() for c in bars.columns]
            if len(bars) > 1:
                bars = bars.iloc[:-1]   # drop forming bar
            return bars
        except Exception as e:
            logger.debug(f"[CandleManager] Alpaca fetch failed for {symbol}/{timeframe}: {e}")
            return None

    def _fetch_yfinance(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        """Fetch from yfinance as last resort."""
        try:
            import yfinance as yf
            tf = _normalise_tf(timeframe)
            # Convert symbol format: BTC/USD → BTC-USD
            yf_sym = symbol.replace("/", "-")
            days = _bars_to_days(limit, tf)
            # yfinance interval strings
            interval_map = {"1m":"1m","5m":"5m","15m":"15m","30m":"30m",
                            "1h":"1h","4h":"4h","1d":"1d"}
            interval = interval_map.get(tf, tf)
            period = f"{min(days, 60)}d" if tf in ("1m","5m","15m","30m") else f"{days}d"
            df = yf.download(yf_sym, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                return None
            df.columns = [c.lower() if isinstance(c, str) else c[0].lower()
                          for c in df.columns]
            df.index = pd.to_datetime(df.index).tz_localize(None)
            if len(df) > 1:
                df = df.iloc[:-1]   # drop forming bar
            return df if not df.empty else None
        except Exception as e:
            logger.debug(f"[CandleManager] yfinance fetch failed for {symbol}/{timeframe}: {e}")
            return None

    def _fetch_from_source(self, symbol: str, timeframe: str,
                           limit: int, source: str) -> Optional[pd.DataFrame]:
        """Dispatch to the correct fetcher."""
        if source == SRC_KRAKEN:
            return self._fetch_kraken(symbol, timeframe, limit)
        if source == SRC_ALPACA:
            return self._fetch_alpaca(symbol, timeframe, limit)
        if source == SRC_YFINANCE:
            return self._fetch_yfinance(symbol, timeframe, limit)
        logger.warning(f"[CandleManager] Unknown source: {source}")
        return None

    def _fetch_multi(self, symbol: str, asset_class: str,
                     timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        """Try all sources in preferred order. Cache on first success."""
        sources = self._get_source_order(symbol, asset_class)
        for source in sources:
            df = self._fetch_from_source(symbol, timeframe, limit, source)
            if df is not None and not df.empty:
                if self._store:
                    try:
                        self._store.save(symbol, timeframe, df)
                    except Exception as e:
                        logger.debug(f"[CandleManager] Cache save failed for {symbol}: {e}")
                self._update_registry(symbol, asset_class, source, success=True)
                return df
            self._update_registry(symbol, asset_class, source, success=False)

        # All sources failed
        self._log_unreachable(symbol, timeframe)
        return None

    def _log_unreachable(self, symbol: str, timeframe: str):
        """Append to unreachable_symbols.log — for manual cleanup."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{ts} | {symbol} | {timeframe} | all sources failed\n"
            os.makedirs(os.path.dirname(self._unreachable_log), exist_ok=True)
            with open(self._unreachable_log, "a") as f:
                f.write(line)
            logger.warning(f"[CandleManager] {symbol}/{timeframe} unreachable on all sources — logged")
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def get(self, symbol: str, timeframe: str, limit: int = 200) -> Optional[pd.DataFrame]:
        """
        Get OHLCV candles for a symbol/timeframe.
        Returns cached data if fresh, otherwise fetches and caches.
        """
        asset_class = "crypto" if "/" in symbol else "stock"
        tf = _normalise_tf(timeframe)

        # Cache hit?
        if self._store:
            days = _bars_to_days(limit, tf)
            if self._store.has_data(symbol, tf, days):
                df = self._store.load(symbol, tf, days)
                if df is not None and not df.empty:
                    return df.tail(limit)

        # Cache miss — fetch
        return self._fetch_multi(symbol, asset_class, tf, limit)

    def prefetch(self, symbol: str, timeframe: str, limit: int = 300) -> bool:
        """Force-fetch and cache. Returns True if successful."""
        asset_class = "crypto" if "/" in symbol else "stock"
        tf = _normalise_tf(timeframe)
        df = self._fetch_multi(symbol, asset_class, tf, limit)
        return df is not None

    def invalidate(self, symbol: str, timeframe: str):
        """Force next get() to re-fetch (e.g. after a known stale period)."""
        if self._store:
            try:
                self._store.delete(symbol, _normalise_tf(timeframe))
            except Exception:
                pass

    def get_registry(self) -> Dict[str, str]:
        """Return copy of in-memory source registry."""
        with self._registry_lock:
            return dict(self._registry)

    # -----------------------------------------------------------------------
    # Background refresh loop
    # -----------------------------------------------------------------------

    def start_refresh_loop(self,
                           crypto_symbols: List[str],
                           stock_symbols:  List[str],
                           crypto_tfs:     List[str] = None,
                           stock_tfs:      List[str] = None,
                           interval_sec:   int = 300):
        """
        Start a background daemon thread that refreshes all watchlist symbols
        every `interval_sec` seconds (default 5 min = matches crypto scan interval).

        Call once from bot_engine.py after startup.
        """
        if self._refresh_thread and self._refresh_thread.is_alive():
            logger.warning("CandleManager refresh loop already running")
            return

        crypto_tfs = crypto_tfs or ["5m", "15m", "1h"]
        stock_tfs  = stock_tfs  or ["5Min", "15Min", "1Hour"]

        self._refresh_symbols = (
            [(s, "crypto") for s in crypto_symbols] +
            [(s, "stock")  for s in stock_symbols]
        )
        self._refresh_tfs = {
            "crypto": [_normalise_tf(tf) for tf in crypto_tfs],
            "stock":  [_normalise_tf(tf) for tf in stock_tfs],
        }
        self._refresh_interval = interval_sec
        self._refresh_stop.clear()

        self._refresh_thread = threading.Thread(
            target=self._refresh_worker,
            name="candle-refresh",
            daemon=True,
        )
        self._refresh_thread.start()
        logger.info(
            f"CandleManager refresh loop started — "
            f"{len(crypto_symbols)} crypto + {len(stock_symbols)} stock symbols, "
            f"every {interval_sec}s"
        )

    def stop_refresh_loop(self):
        self._refresh_stop.set()

    def _refresh_worker(self):
        """Background worker — refreshes all symbols on interval."""
        logger.info("[CandleManager] Refresh worker started")
        while not self._refresh_stop.is_set():
            t0 = time.time()
            ok = fail = 0
            for symbol, asset_class in self._refresh_symbols:
                if self._refresh_stop.is_set():
                    break
                if symbol in _CRYPTO_BLACKLIST and asset_class == "crypto":
                    continue
                tfs = self._refresh_tfs.get(asset_class, ["1h"])
                for tf in tfs:
                    try:
                        df = self._fetch_multi(symbol, asset_class, tf, limit=300)
                        if df is not None:
                            ok += 1
                        else:
                            fail += 1
                    except Exception as e:
                        logger.debug(f"[CandleManager] Refresh error {symbol}/{tf}: {e}")
                        fail += 1
                    # Small sleep between fetches to avoid rate limits
                    time.sleep(0.1)

            elapsed = time.time() - t0
            logger.info(
                f"[CandleManager] Refresh complete — "
                f"{ok} ok / {fail} failed in {elapsed:.1f}s"
            )
            # Wait for next interval
            self._refresh_stop.wait(timeout=max(0, self._refresh_interval - elapsed))

        logger.info("[CandleManager] Refresh worker stopped")


# ---------------------------------------------------------------------------
# Stablecoin / pegged token blacklist (never trade these)
# ---------------------------------------------------------------------------
_CRYPTO_BLACKLIST = {
    "USDT/USD", "USDC/USD", "DAI/USD", "BUSD/USD", "TUSD/USD",
    "USDS/USD", "USDE/USD", "FDUSD/USD", "USDP/USD", "FRAX/USD",
    "LUSD/USD", "PYUSD/USD", "USDD/USD", "GUSD/USD", "SUSD/USD",
    "PAXG/USD", "XAUT/USD", "STETH/USD", "CBETH/USD", "RETH/USD",
    "WSTETH/USD", "WBTC/USD",
}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
candle_manager = CandleManager()
