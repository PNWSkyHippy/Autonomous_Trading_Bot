"""
scanners/catalyst_checker.py
=============================
Checks whether a gapping stock has a credible news catalyst behind it.

Uses Alpha Vantage News Sentiment API -- one batch call covers all symbols,
staying well within the 25 req/day free tier.

Catalyst strength:
    hard  -- earnings, FDA, merger/acquisition, clinical trial, IPO
             gap likely holds, Gap & Go candidate
    soft  -- analyst upgrade/downgrade, general financial news
             gap may fade, Gap Fill candidate
    none  -- no recent news found
             skip entirely -- probably noise or thin float manipulation

Usage:
    checker = CatalystChecker(api_key="YOUR_KEY")
    results = checker.check_batch(["AAPL", "NVDA", "SMCI"])
    for sym, result in results.items():
        if result.tradeable:
            print(sym, result.catalyst_type, result.strength, result.headline)
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

logger = logging.getLogger(__name__)

AV_URL       = "https://www.alphavantage.co/query"
MAX_SYMBOLS  = 50     # AV accepts up to ~50 tickers per call
NEWS_WINDOW  = 48     # hours -- look back this far for catalyst news
MIN_REL      = 0.3    # minimum relevance score to count an article


# ---------------------------------------------------------------------------
# Topic → catalyst classification
# ---------------------------------------------------------------------------

HARD_TOPICS = {
    "earnings",
    "ipo",
    "mergers_and_acquisitions",
    "clinical_trials",
    "fda",
    "real_estate",        # REIT earnings etc
    "energy_transportation",
}

SOFT_TOPICS = {
    "financial_markets",
    "economy_macro",
    "economy_fiscal",
    "economy_monetary",
    "finance",
    "technology",
    "manufacturing",
    "retail_wholesale",
    "life_sciences",
}

# Keywords that upgrade a "soft" article to "hard"
HARD_KEYWORDS = [
    "earnings", "beat", "beats", "missed", "miss", "revenue", "profit",
    "eps", "guidance", "raised guidance", "lowered guidance",
    "fda", "approved", "approval", "rejected", "clinical",
    "merger", "acquisition", "buyout", "acquired", "takeover",
    "deal", "contract", "partnership", "record", "all-time",
]

SKIP_KEYWORDS = [
    "predicted", "could", "might", "may gap", "watch list",
    "top stocks", "best stocks", "analyst pick",
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CatalystResult:
    symbol:       str
    has_catalyst: bool
    strength:     str        # "hard", "soft", "none"
    catalyst_type: str       # "earnings", "fda", "merger", "upgrade", "none"
    sentiment:    str        # "Bullish", "Bearish", "Neutral"
    sentiment_score: float   # -1.0 to 1.0
    headline:     str        # best matching headline
    tradeable:    bool       # True if worth registering in gap queue
    skip_reason:  str        # why it was rejected (empty if tradeable)

    @property
    def matches_gap_up(self) -> bool:
        return self.sentiment_score >= 0.05

    @property
    def matches_gap_down(self) -> bool:
        return self.sentiment_score <= -0.05


# ---------------------------------------------------------------------------
# CatalystChecker
# ---------------------------------------------------------------------------

class CatalystChecker:
    """
    Batch news sentiment checker using Alpha Vantage.
    One API call per gap detection window covers all symbols.
    """

    def __init__(self, api_key: str):
        self.api_key   = api_key
        self._enabled  = bool(api_key) and api_key != "YOUR_KEY_HERE" and _REQUESTS_OK
        self._session  = requests.Session() if _REQUESTS_OK else None

        if not _REQUESTS_OK:
            logger.warning("[CATALYST] requests not installed -- catalyst check disabled")
        elif not self._enabled:
            logger.warning("[CATALYST] No API key -- catalyst check disabled, all gaps will pass")
        else:
            logger.info("[CATALYST] Alpha Vantage news sentiment ready")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_batch(self, symbols: List[str]) -> Dict[str, CatalystResult]:
        """
        Check news catalyst for a list of symbols in one API call.
        Returns dict of symbol -> CatalystResult.
        Symbols with no result get a default "none" result.
        """
        if not self._enabled:
            # Pass everything through if disabled
            return {s: self._no_key_result(s) for s in symbols}

        results = {}
        # AV accepts comma-separated tickers, limit 50
        for chunk in self._chunks(symbols, MAX_SYMBOLS):
            chunk_results = self._fetch_batch(chunk)
            results.update(chunk_results)
            if len(symbols) > MAX_SYMBOLS:
                time.sleep(0.5)

        # Fill in any missing symbols with no-news result
        for sym in symbols:
            if sym not in results:
                results[sym] = self._make_result(sym, False, "none", "none",
                                                  "Neutral", 0.0, "", "No news found")
        return results

    def check_single(self, symbol: str) -> CatalystResult:
        """Check a single symbol. Uses one API call."""
        results = self.check_batch([symbol])
        return results.get(symbol, self._make_result(
            symbol, False, "none", "none", "Neutral", 0.0, "", "API error"
        ))

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _fetch_batch(self, symbols: List[str]) -> Dict[str, CatalystResult]:
        tickers_str = ",".join(symbols)
        cutoff_dt   = datetime.now(timezone.utc) - timedelta(hours=NEWS_WINDOW)
        time_from   = cutoff_dt.strftime("%Y%m%dT%H%M")

        params = {
            "function":  "NEWS_SENTIMENT",
            "tickers":   tickers_str,
            "time_from": time_from,
            "limit":     200,
            "sort":      "RELEVANCE",
            "apikey":    self.api_key,
        }

        try:
            resp = self._session.get(AV_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("[CATALYST] API error: %s", e)
            return {}

        if "Note" in data:
            logger.warning("[CATALYST] AV rate limit hit: %s", data["Note"])
            return {}

        if "Information" in data:
            logger.warning("[CATALYST] AV API message: %s", data["Information"])
            return {}

        feed = data.get("feed", [])
        if not feed:
            logger.info("[CATALYST] No news returned for %s", tickers_str)
            return {}

        return self._parse_feed(symbols, feed)

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def _parse_feed(self, symbols: List[str], feed: list) -> Dict[str, CatalystResult]:
        # Group articles by ticker
        by_ticker: Dict[str, list] = {s: [] for s in symbols}

        for article in feed:
            ticker_sentiments = article.get("ticker_sentiment", [])
            for ts in ticker_sentiments:
                sym = ts.get("ticker", "")
                if sym not in by_ticker:
                    continue
                rel = float(ts.get("relevance_score", 0))
                if rel < MIN_REL:
                    continue
                by_ticker[sym].append({
                    "title":     article.get("title", ""),
                    "time":      article.get("time_published", ""),
                    "topics":    [t.get("topic", "").lower() for t in article.get("topics", [])],
                    "sentiment_score": float(ts.get("ticker_sentiment_score", 0)),
                    "sentiment_label": ts.get("ticker_sentiment_label", "Neutral"),
                    "relevance": rel,
                })

        results = {}
        for sym in symbols:
            articles = by_ticker.get(sym, [])
            results[sym] = self._classify(sym, articles)

        return results

    def _classify(self, symbol: str, articles: list) -> CatalystResult:
        if not articles:
            return self._make_result(symbol, False, "none", "none",
                                     "Neutral", 0.0, "", "No recent news")

        # Sort by relevance desc
        articles.sort(key=lambda a: a["relevance"], reverse=True)
        best = articles[0]

        title  = best["title"]
        topics = best["topics"]
        score  = best["sentiment_score"]
        label  = best["sentiment_label"]

        title_lower = title.lower()

        # Skip generic prediction/watchlist articles
        if any(kw in title_lower for kw in SKIP_KEYWORDS):
            return self._make_result(symbol, False, "none", "none",
                                     label, score, title,
                                     "Generic prediction article -- skipped")

        # Determine catalyst type and strength
        strength      = "none"
        catalyst_type = "none"

        # Check hard topics first
        for topic in topics:
            if topic in HARD_TOPICS:
                strength      = "hard"
                catalyst_type = topic
                break

        # Check hard keywords in title if no hard topic
        if strength == "none":
            for kw in HARD_KEYWORDS:
                if kw in title_lower:
                    strength      = "hard"
                    catalyst_type = kw
                    break

        # Fall back to soft topics
        if strength == "none":
            for topic in topics:
                if topic in SOFT_TOPICS:
                    strength      = "soft"
                    catalyst_type = topic
                    break

        has_catalyst = strength in ("hard", "soft")
        tradeable    = has_catalyst
        skip_reason  = "" if tradeable else "No identifiable catalyst"

        logger.info(
            "[CATALYST] %s  strength=%s  type=%s  sentiment=%s(%.2f)  '%s'",
            symbol, strength, catalyst_type, label, score,
            title[:80],
        )

        return self._make_result(symbol, has_catalyst, strength, catalyst_type,
                                 label, score, title, skip_reason)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_result(self, symbol, has_catalyst, strength, catalyst_type,
                     sentiment, score, headline, skip_reason) -> CatalystResult:
        return CatalystResult(
            symbol        = symbol,
            has_catalyst  = has_catalyst,
            strength      = strength,
            catalyst_type = catalyst_type,
            sentiment     = sentiment,
            sentiment_score = score,
            headline      = headline,
            tradeable     = has_catalyst,
            skip_reason   = skip_reason,
        )

    def _no_key_result(self, symbol: str) -> CatalystResult:
        """Pass-through result when checker is disabled."""
        return self._make_result(symbol, True, "unknown", "unknown",
                                 "Neutral", 0.0, "", "")

    @staticmethod
    def _chunks(lst: list, n: int):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]
