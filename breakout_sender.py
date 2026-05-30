"""
breakout_sender.py  (hardened v2)
==================================
Sends validated BreakoutScanner signals to the Trading Bot V2 API server.

Changes from v1:
  - Payload now includes candle OHLC (required by hardened receiver wick check)
  - Tries to extract OHLC from signal.history if not passed as candle_data
  - volume_spike now uses signal.volume_spike (real ratio vs average)
  - Unicode arrows replaced with ASCII for Windows cp1252 compatibility
  - Structured response logging (failure_class aware)
  - TTL-aware: sends UTC ISO timestamp so receiver can compute signal age

Config keys (scanner_config.json):
    bot_api_url         str   e.g. "http://localhost:8181"
    bot_api_key         str   must match BOT_API_KEY in bot's config.py
    bot_min_escalation  int   minimum escalation to send (default: 2)
    bot_enabled         bool  master switch (default: true)
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_BOT_URL        = "http://localhost:8181"
DEFAULT_MIN_ESCALATION = 2
SEND_TIMEOUT_SEC       = 5
MAX_RETRIES            = 1
RETRY_DELAY_SEC        = 1


class BreakoutSender:
    """
    Sends breakout signals to the Trading Bot API.
    Non-blocking — all sends fire in a background thread.
    """

    def __init__(self, config: dict):
        self._url      = config.get("bot_api_url",        DEFAULT_BOT_URL).rstrip("/")
        self._key      = config.get("bot_api_key",        "")
        self._min_esc  = config.get("bot_min_escalation", DEFAULT_MIN_ESCALATION)
        self._enabled  = config.get("bot_enabled",        True)
        self._session  = requests.Session() if REQUESTS_AVAILABLE else None
        self._last_sent: Dict[str, float] = {}
        self._dedup_sec = 30

        if not REQUESTS_AVAILABLE:
            logger.warning("[SENDER] requests not installed — bot injection disabled")
        elif not self._key:
            logger.warning("[SENDER] bot_api_key not set — bot injection disabled")
        elif self._enabled:
            logger.info(
                f"[SENDER] Bot injection enabled -> {self._url} "
                f"(min escalation: {self._min_esc})"
            )

    @property
    def ready(self) -> bool:
        return REQUESTS_AVAILABLE and bool(self._key) and self._enabled

    # Maximum bars since breakout was first detected before we stop sending.
    # On 5m candles: 6 bars = 30 minutes. Signals older than this are stale
    # — the breakout has already played out and entering now is chasing the tail.
    MAX_BREAKOUT_AGE_BARS = 6

    # Minimum average USD volume per candle to accept a signal.
    # Filters out illiquid micro-caps where one seller can cliff-drop the price
    # with no liquidity to absorb the move (e.g. KULA, SUNDOG).
    # avg_volume (units) * price = USD volume per candle.
    # At 500 USD/candle minimum: a $0.01 token needs 50,000 units/candle average.
    MIN_USD_VOLUME_PER_CANDLE = 500.0

    def should_send(self, signal, escalation: int) -> bool:
        if not self.ready:
            return False
        if escalation < self._min_esc:
            return False

        # Liquidity filter — reject illiquid tokens where price can cliff-drop
        # with no warning due to an empty order book.
        avg_vol = getattr(signal, "avg_volume", None)
        price   = getattr(signal, "price",      None)
        if avg_vol is not None and price and price > 0:
            usd_vol = float(avg_vol) * float(price)
            if usd_vol < self.MIN_USD_VOLUME_PER_CANDLE:
                logger.info(
                    f"[SENDER] REJECTED {signal.symbol} — insufficient liquidity "
                    f"(avg {usd_vol:.1f} USD/candle < {self.MIN_USD_VOLUME_PER_CANDLE} min)"
                )
                return False

        # Stale breakout guard — drop signals where the breakout started too long ago.
        # bars_since_breakout is incremented by EscalationTracker on every scan cycle.
        bars_since = getattr(signal, "bars_since_breakout", 0)
        if bars_since > self.MAX_BREAKOUT_AGE_BARS:
            logger.info(
                f"[SENDER] {signal.symbol} breakout is {bars_since} bars old "
                f"(max {self.MAX_BREAKOUT_AGE_BARS}) — stale signal, dropping send"
            )
            return False

        if escalation > 0:
            # EscalationTracker guarantees each level fires exactly once per symbol lifetime.
            # The dedup window only risks blocking esc=3 when price moves fast after esc=2.
            return True
        last = self._last_sent.get(signal.symbol, 0)
        if time.time() - last < self._dedup_sec:
            return False
        return True

    def _resolve_direction(self, signal) -> Optional[str]:
        """
        Resolve direction with a safe fallback chain:
          1. signal.direction (explicit, set by scanner at signal creation)
          2. signal.momentum_score (inferred: negative = short, non-negative = long)
          3. None  → caller drops the send
        Logs at INFO when the fallback is used so it shows in scanner logs.
        """
        direction = getattr(signal, "direction", "") or ""
        if direction:
            return direction.lower()
        ms = getattr(signal, "momentum_score", None)
        if ms is not None:
            inferred = "short" if float(ms) < 0 else "long"
            logger.info(
                f"[SENDER] {signal.symbol} direction inferred from momentum_score "
                f"({float(ms):.3f}) -> {inferred}"
            )
            return inferred
        return None

    def send_signal(self, signal, escalation: int, candle_data: dict = None):
        """
        Non-blocking send in background thread.

        signal:      BreakoutSignal from BreakoutScanner
        escalation:  0-3
        candle_data: optional fallback dict — used only if signal.enrichment absent
        """
        if not self.should_send(signal, escalation):
            return

        direction = self._resolve_direction(signal)
        if not direction:
            logger.warning(
                f"[SENDER] {signal.symbol} missing direction and no momentum_score "
                f"fallback — dropping send (scanner must set signal.direction)"
            )
            return

        self._last_sent[signal.symbol] = time.time()
        payload = self._build_payload(signal, escalation, direction, candle_data)

        t = threading.Thread(
            target=self._send_with_retry,
            args=(payload,),
            daemon=True,
            name=f"sender-{signal.symbol}",
        )
        t.start()

    def _build_payload(self, signal, escalation: int, direction: str,
                       candle_data: dict = None) -> dict:
        """
        Build the API payload.

        Data priority for candle / analytical fields:
          1. signal.enrichment  — scanner-built at signal creation (BREAKOUT 8A)
          2. candle_data arg    — explicit override (AlertSystem compatibility)
          3. signal.history     — legacy fallback, logged when used
          4. None               — field omitted; receiver will SKIP soft checks
        """
        # ── Resolve enrichment source ────────────────────────────────────────
        enrichment = getattr(signal, "enrichment", {}) or {}
        if enrichment:
            cd = enrichment
            src = "enrichment"
        elif candle_data:
            cd = candle_data
            src = "candle_data"
        else:
            cd = {}
            src = "none"
            # Legacy: try to pull OHLC from signal.history if it exists
            if hasattr(signal, "history") and signal.history:
                try:
                    last_bar = signal.history[-1]
                    cd = {
                        "open":  getattr(last_bar, "open_price", None),
                        "high":  getattr(last_bar, "high",       None),
                        "low":   getattr(last_bar, "low",        None),
                        "close": getattr(last_bar, "price",      None),
                    }
                    src = "history_fallback"
                    logger.debug(f"[SENDER] {signal.symbol} using legacy history fallback")
                except Exception:
                    pass

        # ── Compact prep log ─────────────────────────────────────────────────
        has_ohlc   = all(cd.get(k) is not None for k in ("open", "high", "low", "close"))
        has_rsi    = cd.get("rsi") is not None
        has_market = cd.get("market_pct_change") is not None
        logger.info(
            f"[SENDER] PREP {signal.symbol} {direction} esc={escalation} "
            f"src={src} ohlc={'yes' if has_ohlc else 'no'} "
            f"rsi={'yes' if has_rsi else 'no'} "
            f"market={'yes' if has_market else 'no'}"
        )

        asset_class = "crypto" if "/" in signal.symbol else "stock"
        broker      = getattr(signal, "broker", "KRAKEN").lower()
        now_utc     = datetime.now(timezone.utc).isoformat()

        sig_ts = getattr(signal, "timestamp", None)
        source_ts = (
            sig_ts.astimezone(timezone.utc).isoformat()
            if sig_ts and hasattr(sig_ts, "astimezone") else None
        )

        return {
            # Identity
            "symbol":        signal.symbol,
            "broker":        broker,
            "source_broker": broker,
            "direction":     direction,
            "asset_class":   asset_class,
            "signal_source": "breakout_scanner",

            # Time — timestamp must remain send-time for receiver TTL; source_timestamp is audit only
            "timestamp":        now_utc,
            "source_timestamp": source_ts,

            # Price
            "entry_price":   signal.entry_price,
            "current_price": signal.current_price,
            "source_price":  signal.current_price,

            # Move context
            "move_pct":      signal.best_move_pct,
            "best_move_pct": signal.best_move_pct,
            "escalation":    escalation,

            # Quality
            "volume_spike":   getattr(signal, "volume_spike",   cd.get("volume_spike",  0.0)),
            "momentum_score": getattr(signal, "momentum_score", cd.get("momentum_score", 0.0)),
            "confidence":     getattr(signal, "confidence",     0.7),

            # Candle anatomy — from enrichment when available
            "candle_open":  cd.get("open"),
            "candle_high":  cd.get("high"),
            "candle_low":   cd.get("low"),
            "candle_close": cd.get("close"),

            # Analytical fields — from enrichment when available
            "rsi":                   cd.get("rsi"),
            "sma200":                cd.get("sma200"),
            "structural_stop_price": cd.get("structural_stop") or cd.get("structural_stop_price"),
            "market_pct_change":     cd.get("market_pct_change"),
            "pattern":               cd.get("pattern") or getattr(signal, "pattern_detected", None),

            # Breakout metadata (populated by scanner when available)
            "breakout_level":             getattr(signal, "breakout_level",             None),
            "bars_since_breakout":        getattr(signal, "bars_since_breakout",        None),
            "distance_from_breakout_pct": getattr(signal, "distance_from_breakout_pct", None),
        }

    def send_raw_payload(self, payload: dict) -> None:
        """
        Send a pre-built payload dict directly to the bot API.
        Used by gap scanner and other event sources that build their own payloads.
        Non-blocking -- fires in a background thread.
        """
        if not self.ready:
            return
        sym = payload.get("symbol", "?")
        last = self._last_sent.get(sym, 0)
        if time.time() - last < self._dedup_sec:
            logger.debug(f"[SENDER] {sym} dedup suppressed raw send")
            return
        self._last_sent[sym] = time.time()
        logger.info(
            f"[SENDER] RAW SEND {sym} source={payload.get('signal_source','?')} "
            f"dir={payload.get('direction','?')} conf={payload.get('confidence','?')}"
        )
        t = threading.Thread(
            target=self._send_with_retry,
            args=(payload,),
            daemon=True,
            name=f"sender-{sym}-raw",
        )
        t.start()

    def _send_with_retry(self, payload: dict):
        """Background thread: send with one retry on timeout."""
        url     = f"{self._url}/api/breakout_signal"
        headers = {"X-Api-Key": self._key, "Content-Type": "application/json"}
        sym     = payload.get("symbol", "?")
        esc     = payload.get("escalation", 0)

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._session.post(
                    url, json=payload, headers=headers,
                    timeout=SEND_TIMEOUT_SEC,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    fc = result.get("failure_class")

                    if result.get("accepted"):
                        logger.info(
                            f"[SENDER] ACCEPTED {sym} esc={esc} "
                            f"trade_id={result.get('trade_id')}"
                        )
                    elif fc == "invalid_payload":
                        logger.warning(
                            f"[SENDER] INVALID PAYLOAD {sym} — "
                            f"{result.get('reason')} (scanner needs to send more fields)"
                        )
                    elif fc == "injection_exception":
                        logger.error(
                            f"[SENDER] INJECTION ERROR {sym} — "
                            f"{result.get('reason')}"
                        )
                    else:
                        logger.info(
                            f"[SENDER] REJECTED {sym} esc={esc} — "
                            f"{result.get('reason')}"
                        )
                    return
                else:
                    logger.warning(
                        f"[SENDER] {sym} HTTP {resp.status_code}: "
                        f"{resp.text[:120]}"
                    )
                    return

            except requests.exceptions.Timeout:
                if attempt < MAX_RETRIES:
                    logger.debug(f"[SENDER] {sym} timeout, retrying...")
                    time.sleep(RETRY_DELAY_SEC)
                else:
                    logger.warning(
                        f"[SENDER] {sym} timed out after {MAX_RETRIES + 1} attempts"
                    )
            except requests.exceptions.ConnectionError:
                logger.debug(f"[SENDER] {sym} bot not reachable at {self._url}")
                return
            except Exception as e:
                logger.warning(f"[SENDER] {sym} send error: {e}")
                return
