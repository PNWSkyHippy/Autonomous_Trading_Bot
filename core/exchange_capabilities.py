"""
=============================================================
  EXCHANGE CAPABILITIES
  Checks whether a broker supports shorting a given symbol.

  Kraken — public API, no auth needed. Checks leverage_sell
    field per pair. Cached 4h. Refreshed on next can_short()
    call after TTL expires.

  Alpaca — checks asset shortable + easy_to_borrow flags.
    Cached 4h.

  IBKR — not yet implemented; defaults to ALLOW short
    (TWS manages availability at execution time).

  Usage:
      from core.exchange_capabilities import exchange_capabilities

      if not exchange_capabilities.can_short("BTC/USD", "kraken"):
          logger.info("BTC/USD not margin-eligible on Kraken — skip short")
          return None
=============================================================
"""

import logging
import time
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# How long to cache results before re-fetching (seconds)
CACHE_TTL_SECONDS = 4 * 3600  # 4 hours


class ExchangeCapabilities:
    """
    Singleton that caches per-broker shortability data and
    exposes can_short(symbol, broker) → bool.
    """

    def __init__(self):
        # { broker_name: {"loaded_at": float, "shortable": set[str]} }
        self._cache: Dict[str, Dict] = {}

    # ── Public interface ─────────────────────────────────────────────────

    def can_short(self, symbol: str, broker: str) -> bool:
        """
        Return True if the broker allows shorting this symbol.

        On any fetch error the method logs a warning and returns True
        (fail-open) so a temporary API outage does not block all shorts.
        """
        broker = broker.lower()

        if broker == "kraken":
            return self._can_short_kraken(symbol)
        elif broker in ("alpaca",):
            return self._can_short_alpaca(symbol)
        elif broker in ("ibkr", "paper", "coinbase"):
            # IBKR enforces at execution; paper/coinbase are always fine
            return True
        else:
            logger.debug(
                f"[ExchangeCapabilities] Unknown broker '{broker}' — "
                f"defaulting to allow short for {symbol}"
            )
            return True

    def refresh(self, broker: str):
        """Force a cache refresh for a specific broker."""
        broker = broker.lower()
        if broker in self._cache:
            del self._cache[broker]
        logger.info(f"[ExchangeCapabilities] Cache cleared for {broker}")

    def shortable_symbols(self, broker: str) -> Set[str]:
        """Return the full set of shortable symbols for a broker (loads cache if needed)."""
        broker = broker.lower()
        if broker == "kraken":
            self._ensure_kraken_cache()
            return self._cache.get("kraken", {}).get("shortable", set())
        return set()

    def get_max_short_leverage(self, symbol: str, broker: str) -> int:
        """
        Return the maximum leverage available for shorting this symbol on the
        given broker. Returns 1 (no leverage) on any error or unknown broker.

        Kraken: reads the highest value from leverage_sell in the cached pair data.
        Others: returns 1 (treat as spot — no leverage decision needed).
        """
        broker = broker.lower()
        if broker != "kraken":
            return 1

        try:
            self._ensure_kraken_cache()
        except Exception:
            return 1

        lev_map: Dict[str, int] = self._cache.get("kraken", {}).get("lev_map", {})
        if not lev_map:
            return 1

        candidates = self._kraken_symbol_candidates(symbol)
        for cand in candidates:
            if cand in lev_map:
                return lev_map[cand]
        return 1

    # ── Kraken ───────────────────────────────────────────────────────────

    def _can_short_kraken(self, symbol: str) -> bool:
        """
        Returns True if the symbol has leverage_sell available on Kraken.

        Kraken uses non-standard pair names (XBTUSD, ETHUSD, XXBTZUSD …).
        We normalize our symbol several ways and check against the cached set.
        """
        try:
            self._ensure_kraken_cache()
        except Exception as e:
            logger.warning(
                f"[ExchangeCapabilities] Kraken cache load failed: {e} — "
                f"defaulting to ALLOW short for {symbol}"
            )
            return True

        shortable: Set[str] = self._cache.get("kraken", {}).get("shortable", set())
        if not shortable:
            # Empty set after successful fetch = API returned data but pair
            # not found; log once and fail-open
            logger.debug(
                f"[ExchangeCapabilities] Kraken shortable set is empty — "
                f"fail-open for {symbol}"
            )
            return True

        # Build candidate normalized names to check
        candidates = self._kraken_symbol_candidates(symbol)
        for cand in candidates:
            if cand in shortable:
                return True

        logger.info(
            f"[ExchangeCapabilities] {symbol} NOT margin-eligible on Kraken "
            f"(checked {candidates})"
        )
        return False

    def _ensure_kraken_cache(self):
        """Load or refresh the Kraken shortable-pairs cache."""
        entry = self._cache.get("kraken", {})
        age = time.time() - entry.get("loaded_at", 0)
        if age < CACHE_TTL_SECONDS and "shortable" in entry:
            return  # cache still warm

        logger.info("[ExchangeCapabilities] Fetching Kraken margin pairs…")
        shortable, lev_map = self._fetch_kraken_shortable()
        self._cache["kraken"] = {
            "loaded_at": time.time(),
            "shortable":  shortable,
            "lev_map":    lev_map,   # pair_name -> max_leverage (int)
        }
        logger.info(
            f"[ExchangeCapabilities] Kraken cache loaded: "
            f"{len(shortable)} margin-eligible pairs"
        )

    def _fetch_kraken_shortable(self):
        """
        Calls Kraken public API (no auth).
        Returns (shortable: Set[str], lev_map: Dict[str, int]).
        - shortable: all pair name variants where leverage_sell has a level > 1
        - lev_map:   pair_name -> max leverage_sell value for that pair
        """
        import ccxt
        exchange = ccxt.kraken({"enableRateLimit": True, "timeout": 15000})

        # publicGetAssetPairs() returns the raw Kraken REST response
        resp = exchange.publicGetAssetPairs()
        pairs: Dict = resp.get("result", {})

        shortable: Set[str] = set()
        lev_map: Dict[str, int] = {}

        for pair_id, info in pairs.items():
            lev_sell = info.get("leverage_sell", [])
            # leverage_sell = [] or [0] means no margin; [2,3,4,5] means eligible
            if not lev_sell or not any(int(v) > 1 for v in lev_sell):
                continue

            max_lev = max(int(v) for v in lev_sell)

            # Register all name variants in shortable and lev_map
            def _register(name: str):
                shortable.add(name)
                # Keep the highest leverage seen for this name
                if name not in lev_map or lev_map[name] < max_lev:
                    lev_map[name] = max_lev

            _register(pair_id)

            altname = info.get("altname", "")
            if altname:
                _register(altname)

            wsname = info.get("wsname", "")
            if wsname:
                _register(wsname)
                _register(self._wsname_to_ccxt(wsname))

        return shortable, lev_map

    @staticmethod
    def _wsname_to_ccxt(wsname: str) -> str:
        """
        Convert Kraken wsname "XBT/USD" → ccxt symbol "BTC/USD".
        Handles the XBT→BTC mapping that Kraken uses internally.
        """
        xbt_map = {
            "XBT": "BTC",
            "XDG": "DOGE",
        }
        if "/" in wsname:
            base, quote = wsname.split("/", 1)
            base  = xbt_map.get(base, base)
            quote = xbt_map.get(quote, quote)
            return f"{base}/{quote}"
        return wsname

    @staticmethod
    def _kraken_symbol_candidates(symbol: str) -> Set[str]:
        """
        Generate all the name variants we should check against the
        Kraken shortable set for a given symbol string.

        Input examples:
            "BTC/USD"   "BTC/USDT"  "BTCUSDT"   "ETH/USD"
        """
        candidates: Set[str] = {symbol, symbol.upper()}

        # Handle slash-separated symbols like "BTC/USD"
        if "/" in symbol:
            base, quote = symbol.upper().split("/", 1)
            # USDT → USD (Kraken uses USD not USDT for most pairs)
            quote_usd = "USD" if quote in ("USDT", "USDC") else quote
            candidates.update({
                f"{base}/{quote}",
                f"{base}/{quote_usd}",
                f"{base}{quote}",
                f"{base}{quote_usd}",
                # Kraken XBT mapping
                f"{'XBT' if base == 'BTC' else base}/{quote_usd}",
                f"{'XBT' if base == 'BTC' else base}{quote_usd}",
                f"XBT/USD" if base == "BTC" else f"{base}/{quote_usd}",
            })
        else:
            # Symbol without slash e.g. "BTCUSDT" — try to split on common quotes
            su = symbol.upper()
            for quote_suffix in ("USDT", "USDC", "USD", "EUR"):
                if su.endswith(quote_suffix):
                    base = su[: -len(quote_suffix)]
                    quote_usd = "USD" if quote_suffix in ("USDT", "USDC") else quote_suffix
                    candidates.update({
                        f"{base}/{quote_usd}",
                        f"{base}{quote_usd}",
                        f"{'XBT' if base == 'BTC' else base}/{quote_usd}",
                        f"{'XBT' if base == 'BTC' else base}{quote_usd}",
                    })
                    break

        return candidates

    # ── Alpaca ───────────────────────────────────────────────────────────

    def _can_short_alpaca(self, symbol: str) -> bool:
        """
        Returns True if Alpaca marks the asset as shortable and easy-to-borrow.
        Falls back to True on any error.
        """
        try:
            self._ensure_alpaca_cache()
        except Exception as e:
            logger.warning(
                f"[ExchangeCapabilities] Alpaca cache load failed: {e} — "
                f"defaulting to ALLOW short for {symbol}"
            )
            return True

        shortable: Set[str] = self._cache.get("alpaca", {}).get("shortable", set())
        sym_upper = symbol.upper().replace("/", "").replace("-", "")
        result = sym_upper in shortable
        if not result:
            logger.info(
                f"[ExchangeCapabilities] {symbol} NOT shortable on Alpaca"
            )
        return result

    def _ensure_alpaca_cache(self):
        """Load or refresh the Alpaca shortable assets cache."""
        entry = self._cache.get("alpaca", {})
        age = time.time() - entry.get("loaded_at", 0)
        if age < CACHE_TTL_SECONDS and "shortable" in entry:
            return

        logger.info("[ExchangeCapabilities] Fetching Alpaca shortable assets…")
        shortable = self._fetch_alpaca_shortable()
        self._cache["alpaca"] = {
            "loaded_at": time.time(),
            "shortable": shortable,
        }
        logger.info(
            f"[ExchangeCapabilities] Alpaca cache loaded: "
            f"{len(shortable)} shortable assets"
        )

    def _fetch_alpaca_shortable(self) -> Set[str]:
        """
        Fetch assets from Alpaca and return symbols that are
        tradeable + shortable + easy_to_borrow.
        """
        try:
            import alpaca_trade_api as tradeapi
            import config
            api = tradeapi.REST(
                config.ALPACA_API_KEY,
                config.ALPACA_SECRET_KEY,
                config.ALPACA_BASE_URL,
            )
            assets = api.list_assets(status="active", asset_class="us_equity")
            shortable: Set[str] = set()
            for asset in assets:
                if (getattr(asset, "tradable", False) and
                        getattr(asset, "shortable", False) and
                        getattr(asset, "easy_to_borrow", False)):
                    shortable.add(asset.symbol.upper())
            return shortable
        except Exception as e:
            logger.warning(f"[ExchangeCapabilities] Alpaca asset fetch error: {e}")
            return set()


# ── Singleton ────────────────────────────────────────────────────────────────
exchange_capabilities = ExchangeCapabilities()
