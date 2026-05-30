"""
scanners/funding_scanner.py
============================
Funding Rate Extreme Scanner for perpetual futures.

What is funding rate?
---------------------
Perpetual swaps have no expiry. Instead, longs pay shorts (or vice-versa)
every 8 hours to keep the contract price anchored to spot. This is the
"funding rate."

When funding is EXTREMELY positive  → longs are heavily crowded → mean-revert
short bias (smart money bets the crowded side gets squeezed).

When funding is EXTREMELY negative  → shorts are heavily crowded → mean-revert
long bias (squeeze coming for overcrowded shorts).

Thresholds (empirical, Bybit/Binance norms):
    LONG signal  : rate ≤ -0.05%  (shorts so dominant that squeeze is likely)
    SHORT signal : rate ≥  0.10%  (longs paying heavily, reversal probable)

These fire INFREQUENTLY -- typically 1-3 times per month per symbol.
That rarity is the whole point: when funding is extreme, the edge is high.

Data sources (tried in order):
    1. Bybit REST   GET /v5/market/tickers           (no auth, free)
    2. Kraken REST  GET /derivatives/api/v3/tickers  (no auth, free)
       Kraken Futures is available to Washington-state residents.

Cooldown: 8 hours per symbol (one extreme-funding signal per session)

References:
    https://www.bybit.com/en-US/help-center/s/article/What-Are-Funding-Fees
    https://binance-docs.github.io/apidocs/futures/en/#mark-price
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict

from scanners.base_scanner import BaseEventScanner

try:
    import requests as _requests
    _REQ_OK = True
except ImportError:
    _REQ_OK = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds & tuning
# ---------------------------------------------------------------------------

FUNDING_LONG_THRESHOLD  = -0.0005   # ≤ -0.05%  → crowded shorts → go long
FUNDING_SHORT_THRESHOLD =  0.0010   #  ≥  0.10%  → crowded longs  → go short

COOLDOWN_HOURS      = 8.0
CHECK_INTERVAL_SEC  = 300           # check every 5 minutes

SCORE_BASE          = 0.72
SL_PCT              = 2.0           # 2% stop loss (tight -- mean reversion)
TP_PCT              = 4.0           # 4% TP (2:1 RR)

# Symbols to scan -- Bybit perp format: BTCUSDT, ETHUSDT, etc.
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT",
    "LINKUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT", "NEARUSDT",
    "AAVEUSDT", "FTMUSDT", "SANDUSDT", "MANAUSDT", "ALGOUSDT",
]

BYBIT_TICKERS_URL   = "https://api.bybit.com/v5/market/tickers"
KRAKEN_TICKERS_URL  = "https://futures.kraken.com/derivatives/api/v3/tickers"
COINBASE_SPOT_URL   = "https://api.coinbase.com/v2/prices/{pair}/spot"

# Kraken uses different base names for some assets vs Coinbase
_COINBASE_BASE_MAP = {
    "XBT": "BTC",
    "XDG": "DOGE",
}


class FundingScanner(BaseEventScanner):
    """
    Scans perpetual futures funding rates on Bybit (fallback: Binance).
    Fires when a symbol's rate crosses extreme thresholds.
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
    ):
        super().__init__(
            name               = "FundingScanner",
            cooldown_hours     = COOLDOWN_HOURS,
            check_interval_sec = CHECK_INTERVAL_SEC,
        )
        self.symbols  = symbols or DEFAULT_SYMBOLS
        self._session = _requests.Session() if _REQ_OK else None
        self._bybit_cache: Dict[str, float] = {}   # sym → last funding rate
        self._last_fetch = 0.0

        if not _REQ_OK:
            logger.warning("[FUNDING] requests not installed -- scanner disabled")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run_once(self) -> None:
        if not _REQ_OK:
            return
        self._refresh_rates()
        self._evaluate_all()

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _refresh_rates(self) -> None:
        """Pull latest funding rates from Kraken Futures.
        Bybit is geo-blocked for US IPs — skipped entirely."""
        rates = self._fetch_kraken()
        if rates:
            self._bybit_cache = rates
            self._last_fetch  = time.time()

    def _fetch_bybit(self) -> Dict[str, float]:
        """
        GET /v5/market/tickers?category=linear
        Returns {symbol: funding_rate_float} for USDT perps.
        """
        try:
            resp = self._session.get(
                BYBIT_TICKERS_URL,
                params={"category": "linear"},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()

            rates = {}
            for item in data.get("result", {}).get("list", []):
                sym    = item.get("symbol", "")
                fr_str = item.get("fundingRate", "")
                if sym and fr_str:
                    try:
                        rates[sym] = float(fr_str)
                    except ValueError:
                        pass
            logger.debug("[FUNDING] Bybit: fetched %d funding rates", len(rates))
            return rates

        except Exception as e:
            logger.warning("[FUNDING] Bybit fetch error: %s", e)
            return {}

    def _fetch_kraken(self) -> Dict[str, float]:
        """
        GET /derivatives/api/v3/tickers  (Kraken Futures, no auth required)
        Returns {symbol: fundingRate_float}.

        Kraken perp symbols look like: PF_XBTUSD, PF_ETHUSD, etc.
        We normalise them to BTCUSDT / ETHUSDT style so the same DEFAULT_SYMBOLS
        list works for both Bybit and Kraken fallback.
        """
        # Map Kraken base → standard ticker
        KRAKEN_BASE_MAP = {
            "XBT": "BTC", "ETH": "ETH", "SOL": "SOL", "BNB": "BNB",
            "XRP": "XRP", "ADA": "ADA", "DOGE": "DOGE", "AVAX": "AVAX",
            "DOT": "DOT", "MATIC": "MATIC", "LINK": "LINK", "LTC": "LTC",
            "UNI": "UNI", "ATOM": "ATOM", "NEAR": "NEAR",
        }
        try:
            resp = self._session.get(KRAKEN_TICKERS_URL, timeout=8)
            resp.raise_for_status()
            data = resp.json()

            rates = {}
            for item in data.get("tickers", []):
                sym = item.get("symbol", "")          # e.g. "PF_XBTUSD"
                fr  = item.get("fundingRate")
                if not sym or fr is None:
                    continue
                # Only perpetuals (prefix PF_)
                if not sym.startswith("PF_"):
                    continue
                base = sym[3:-3]                       # strip PF_ and USD
                std  = KRAKEN_BASE_MAP.get(base, base) + "USDT"
                try:
                    rates[std] = float(fr)
                except (ValueError, TypeError):
                    pass

            logger.debug("[FUNDING] Kraken fallback: fetched %d rates", len(rates))
            return rates

        except Exception as e:
            logger.warning("[FUNDING] Kraken fallback error: %s", e)
            return {}

    # ------------------------------------------------------------------
    # Evaluate
    # ------------------------------------------------------------------

    def _evaluate_all(self) -> None:
        if not self._bybit_cache:
            return

        for sym in self.symbols:
            rate = self._bybit_cache.get(sym)
            if rate is None:
                continue

            if rate >= FUNDING_SHORT_THRESHOLD:
                self._fire(sym, rate, "short")
            elif rate <= FUNDING_LONG_THRESHOLD:
                self._fire(sym, rate, "long")

    def _fetch_spot_price(self, kraken_symbol: str) -> Optional[float]:
        """
        Fetch live spot price with three-source fallback chain:
          1. ccxt Kraken spot  — most reliable, already used elsewhere in the bot
          2. Coinbase v2 REST  — unauthenticated, sometimes rate-limited or unavailable
          3. ccxt Binance      — final fallback for symbols not on Kraken spot

        Coinbase v2 was the sole source previously but proved unreliable:
        BNB, AAVE and others are not listed there, and the API occasionally
        returns non-200 or malformed JSON causing all signals to arrive with
        entry_price=0 and get rejected by the breakout receiver.
        """
        base = kraken_symbol.split("/")[0]
        base_std = _COINBASE_BASE_MAP.get(base, base)   # XBT → BTC, etc.

        # ── 1. ccxt Kraken spot ──────────────────────────────────────────────
        try:
            import ccxt
            exchange = ccxt.kraken({"enableRateLimit": True})
            ticker = exchange.fetch_ticker(f"{base_std}/USD")
            price  = ticker.get("last") or ticker.get("close")
            if price and float(price) > 0:
                logger.debug("[FUNDING] Price %s via Kraken spot: %.6f", kraken_symbol, price)
                return float(price)
        except Exception as e:
            logger.debug("[FUNDING] Kraken spot price failed for %s: %s", kraken_symbol, e)

        # ── 2. Coinbase v2 REST ──────────────────────────────────────────────
        try:
            url  = COINBASE_SPOT_URL.format(pair=f"{base_std}-USD")
            resp = self._session.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            amount_str = data.get("data", {}).get("amount")
            if amount_str:
                price = float(amount_str)
                if price > 0:
                    logger.debug("[FUNDING] Price %s via Coinbase v2: %.6f", kraken_symbol, price)
                    return price
        except Exception as e:
            logger.debug("[FUNDING] Coinbase v2 price failed for %s: %s", kraken_symbol, e)

        # ── 3. ccxt Binance ─────────────────────────────────────────────────
        try:
            import ccxt
            exchange = ccxt.binance({"enableRateLimit": True})
            ticker = exchange.fetch_ticker(f"{base_std}/USDT")
            price  = ticker.get("last") or ticker.get("close")
            if price and float(price) > 0:
                logger.debug("[FUNDING] Price %s via Binance: %.6f", kraken_symbol, price)
                return float(price)
        except Exception as e:
            logger.debug("[FUNDING] Binance price failed for %s: %s", kraken_symbol, e)

        logger.warning("[FUNDING] All price sources failed for %s — signal skipped", kraken_symbol)
        return None

    @staticmethod
    def _to_kraken_symbol(bybit_sym: str) -> Optional[str]:
        """
        Convert Bybit perp format (BTCUSDT) → Kraken spot format (BTC/USD).
        Returns None if the symbol can't be mapped (e.g. non-USDT pairs).
        """
        if bybit_sym.endswith("USDT"):
            base = bybit_sym[:-4]           # strip USDT
            # Kraken renames a handful of bases
            _renames = {"BTC": "XBT", "DOGE": "XDG", "MATIC": "MATIC"}
            base = _renames.get(base, base)
            return f"{base}/USD"
        if bybit_sym.endswith("USD"):
            base = bybit_sym[:-3]
            return f"{base}/USD"
        return None

    def _fire(self, symbol: str, rate: float, direction: str) -> None:
        if self.is_on_cooldown(symbol):
            return

        # Convert Bybit USDT symbol → Kraken USD symbol before routing.
        # Without this conversion every signal hits the "non-USD quote currency"
        # rejection wall in the breakout receiver, spamming the log and wasting cycles.
        kraken_symbol = self._to_kraken_symbol(symbol)
        if kraken_symbol is None:
            logger.debug("[FUNDING] %s — cannot map to Kraken USD symbol, skipping", symbol)
            return
        symbol = kraken_symbol   # use the converted symbol from here on

        rate_pct = rate * 100
        score    = min(0.90, SCORE_BASE + abs(rate_pct) * 2.0)

        live_price = self._fetch_spot_price(symbol)
        if live_price is None:
            logger.warning("[FUNDING] %s — could not fetch live price, skipping signal", symbol)
            return

        logger.info(
            "[FUNDING] %s  %s  rate=%.4f%%  score=%.2f",
            symbol, direction.upper(), rate_pct, score,
        )

        now_utc = self.utc_now()

        if direction == "long":
            reason = "Funding extreme negative %.4f%% -- crowded shorts, squeeze likely" % rate_pct
        else:
            reason = "Funding extreme positive %.4f%% -- crowded longs, reversal likely" % rate_pct

        payload = {
            "symbol":                symbol,
            "asset_class":           "crypto",
            "direction":             direction,
            "entry_price":           live_price,
            "current_price":         live_price,
            "move_pct":              abs(rate_pct),
            "volume_spike":          1.0,
            "confidence":            round(score, 3),
            "escalation":            1,
            "timestamp":             now_utc.isoformat(),
            "signal_source":         "funding_scanner",
            "strategy_name":         "funding_scanner",
            "broker":                "coinbase",
            "stop_loss_pct":         SL_PCT,
            "take_profit_pct":       TP_PCT,
            "structural_stop_price": None,
            "preferred_trail_mode":  "none",
            "reason":                reason,
            "bars_since_breakout":   0,
            "funding_rate_pct":      round(rate_pct, 5),
            "funding_threshold":     FUNDING_SHORT_THRESHOLD * 100 if direction == "short"
                                     else FUNDING_LONG_THRESHOLD * 100,
        }

        result = self.push_signal(payload)
        if result.get("accepted"):
            self.mark_fired(symbol)
