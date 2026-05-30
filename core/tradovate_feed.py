"""
Tradovate real-time market data feed via WebSocket.

Connects to Tradovate's md WebSocket, authenticates with a JWT token,
subscribes to chart bars for a given symbol, and caches the latest bars
so web_dashboard.py can serve them to Chronos AI.

Usage:
    feed = TradovateFeed(token="<jwt>")
    feed.subscribe("MESM6", symbol_id=3961353, element_size=5)
    bars = feed.get_bars("MESM6")   # returns list of OHLCV dicts

Token refresh:
    The JWT from your Tradovate session lasts ~30 min.
    Drop a fresh token at runtime:  feed.update_token("<new_jwt>")

Security:
    Never hard-code the token here. Pass it from a temp file or env var.
    See web_dashboard.py /api/tradovate_token endpoint.
"""

import json
import logging
import threading
import time
from collections import deque
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants (reverse-engineered from DevTools)
# ---------------------------------------------------------------------------
WS_URL_DEMO = "wss://md-demo.tradovateapi.com/v1/websocket"
WS_URL_LIVE  = "wss://md-live.tradovateapi.com/v1/websocket"

MAX_BARS_CACHED = 500   # per symbol

# ---------------------------------------------------------------------------
# Bar cache  (symbol_name -> deque of bar dicts)
# ---------------------------------------------------------------------------
_bar_cache: Dict[str, deque] = {}
_cache_lock = threading.Lock()


def _make_frame(endpoint: str, msg_id: int, payload) -> str:
    """
    Tradovate WS text-frame format:
        <endpoint>\\n<msg_id>\\n\\n<json_or_empty>
    """
    body = json.dumps(payload) if payload is not None else ""
    return f"{endpoint}\n{msg_id}\n\n{body}"


class TradovateFeed:
    """
    Persistent WebSocket client for Tradovate market data.
    Runs a background thread; thread-safe bar cache.
    """

    def __init__(self, token: str = "", use_live: bool = False):
        self._token = token
        self._ws_url = WS_URL_LIVE if use_live else WS_URL_DEMO
        self._ws = None
        self._msg_id = 1
        self._subscriptions: Dict[str, dict] = {}   # name -> {symbol_id, element_size}
        self._chart_id_map: Dict[int, str] = {}     # server chart_id -> sub_name
        self._authenticated = False
        self._running = False
        self._auth_failed = False    # set True on 401/403 — prevents reconnect loop
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()         # signals reconnect sleep to wake early
        self._reconnect_delay   = 5    # seconds before first reconnect after drop
        self._max_reconnect_delay = 300  # cap at 5 min for exponential backoff
        self._last_client_activity = time.time()   # updated on every get_bars() call
        self._inactivity_stop_seconds = 1800       # stop feed after 30 min of no bar requests

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_token(self, token: str):
        """Hot-swap the JWT token (call when user refreshes session)."""
        self._token = token
        logger.info("TradovateFeed: token updated")

    def subscribe(self, name: str, symbol_id, element_size: int = 5,
                  num_bars: int = 300):
        """
        Register a chart subscription.
        name        : friendly name, e.g. "MESM6" or "MNQM6"
        symbol_id   : Tradovate numeric contract ID (int) OR string symbol name.
                      md/getChart accepts both — string names work when numeric ID
                      is unknown (e.g. when REST lookup returns 401 with limited ACL).
        element_size: bar size in minutes (1, 3, 5, 15, …)
        num_bars    : how many historical bars to fetch on connect
        """
        self._subscriptions[name] = {
            "symbol_id": symbol_id,
            "element_size": element_size,
            "num_bars": num_bars,
        }
        with _cache_lock:
            if name not in _bar_cache:
                _bar_cache[name] = deque(maxlen=MAX_BARS_CACHED)
        logger.info("TradovateFeed: registered %s (id=%s, %dm)", name,
                    symbol_id, element_size)

    def start(self):
        """Start the background WebSocket thread."""
        if self._running:
            return
        # Wait for any previous thread to exit before starting a new one.
        # _stop_event was set by stop(); old thread woke from sleep and should
        # exit quickly.  2s timeout is generous — avoids a zombie race.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop,
                                        name="tradovate-feed", daemon=True)
        self._thread.start()
        logger.info("TradovateFeed: started")

    def stop(self):
        """Stop the feed and WebSocket connection."""
        self._running = False
        self._stop_event.set()   # wake any sleeping reconnect immediately
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("TradovateFeed: stopped")

    def reset_auth(self, token: str):
        """
        Clear the auth-failed flag and update token so the feed can restart.
        Call this when the user posts a fresh JWT via /api/tradovate_token.
        """
        self._auth_failed = False
        self._token = token
        self._last_client_activity = time.time()

    def get_bars(self, name: str) -> List[dict]:
        """Return cached bars for a symbol (newest last). Updates activity timestamp."""
        self._last_client_activity = time.time()
        with _cache_lock:
            q = _bar_cache.get(name)
            return list(q) if q else []

    def is_connected(self) -> bool:
        return self._authenticated

    def is_auth_failed(self) -> bool:
        """True if the last auth attempt was rejected — feed has stopped reconnecting."""
        return self._auth_failed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        mid = self._msg_id
        self._msg_id += 1
        return mid

    def _try_renew_token(self) -> bool:
        """
        Attempt to get a fresh JWT by calling Tradovate's renewaccesstoken endpoint.
        Returns True if the token was successfully renewed.
        Called automatically before giving up on a 401/403 or before reconnect.
        """
        import urllib.request
        rest_base = (
            "https://live-d.tradovateapi.com/v1"
            if "live" in self._ws_url
            else "https://demo-d.tradovateapi.com/v1"
        )
        url = f"{rest_base}/auth/renewaccesstoken"

        token = self._token
        if "eyJ" in token:
            token = token[token.index("eyJ"):]

        try:
            req = urllib.request.Request(
                url,
                data=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
                new_token = data.get("accessToken") or data.get("token")
                if new_token:
                    self._token = new_token
                    self._auth_failed = False
                    logger.info("TradovateFeed: token auto-renewed ✓ (expires ~20m)")
                    return True
                logger.warning(
                    "TradovateFeed: renewaccesstoken returned no token: %s",
                    str(data)[:200]
                )
        except Exception as exc:
            logger.warning("TradovateFeed: token renewal failed: %s", exc)
        return False

    def _run_loop(self):
        """Reconnect loop — exits on auth failure or inactivity.
        Uses exponential backoff (30s → 60s → 120s → … → 300s max) to avoid
        hammering Tradovate servers on repeated connection failures.
        On 401/403, tries auto-renewal first before halting.
        """
        current_delay = self._reconnect_delay
        while self._running:
            # Auth failed — try auto-renewal before giving up
            if self._auth_failed:
                logger.info("TradovateFeed: attempting token auto-renewal…")
                if self._try_renew_token():
                    # Renewed — clear flag and reconnect immediately
                    logger.info("TradovateFeed: reconnecting with renewed token")
                    current_delay = self._reconnect_delay
                    continue
                logger.warning(
                    "TradovateFeed: auto-renewal failed — feed halted. "
                    "Post a fresh token to /api/tradovate_token to restart."
                )
                self._running = False
                break

            # Stop if no chart has requested bars in the inactivity window
            idle_seconds = time.time() - self._last_client_activity
            if idle_seconds > self._inactivity_stop_seconds:
                logger.info(
                    "TradovateFeed: no bar requests in %.0fs — stopping feed "
                    "(indicator likely closed). Restart via /api/tradovate_token.",
                    idle_seconds,
                )
                self._running = False
                break

            conn_start = time.time()
            try:
                self._connect()
            except Exception as exc:
                logger.warning("TradovateFeed: connection error: %s", exc)
            conn_duration = time.time() - conn_start

            self._authenticated = False

            # Only sleep and retry if not flagged to stop
            if self._running and not self._auth_failed:
                # If the session lasted >10s it was a normal server-side drop
                # (Tradovate demo enforces session time limits) — reset backoff
                # so we don't accumulate 5→10→20→40s delays for routine drops.
                if conn_duration > 10:
                    current_delay = self._reconnect_delay
                delay = current_delay
                logger.warning("TradovateFeed: reconnecting in %ds", delay)
                # Use event.wait() instead of time.sleep() so stop() can
                # interrupt the sleep immediately (prevents zombie threads).
                self._stop_event.wait(timeout=delay)
                if not self._running:
                    break
                # Exponential backoff: double delay each attempt, cap at max
                current_delay = min(current_delay * 2, self._max_reconnect_delay)
            else:
                # Successful session resets backoff for next disconnect
                current_delay = self._reconnect_delay

    def _connect(self):
        try:
            import websocket  # websocket-client package
        except ImportError:
            logger.error(
                "TradovateFeed: websocket-client not installed. "
                "Run: pip install websocket-client"
            )
            time.sleep(30)
            return

        ws = websocket.WebSocketApp(
            self._ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        # No WS-level ping — SockJS sends its own 'h' heartbeat frames.
        # WS pings cause Tradovate's SockJS server to close the connection.
        ws.run_forever()

    def _on_open(self, ws):
        logger.info("TradovateFeed: WS connected, authenticating…")
        if not self._token:
            logger.warning("TradovateFeed: no token set — cannot authenticate")
            return
        # Extract raw JWT — HTTP auth header may be "Bearer <username> eyJ..."
        # WebSocket needs just the eyJ... part
        token = self._token
        if 'eyJ' in token:
            token = token[token.index('eyJ'):]
        # Auth frame: authorize\n<id>\n\n<raw_jwt>
        mid = self._next_id()
        frame = f"authorize\n{mid}\n\n{token}"
        ws.send(frame)
        logger.warning("TradovateFeed: sent auth msg_id=%d token_len=%d token_prefix=%s",
                       mid, len(token), token[:10])

    def _on_message(self, ws, raw: str):
        # Tradovate can batch multiple frames in one message with \n---\n
        for chunk in raw.split("\n---\n"):
            chunk = chunk.strip()
            if not chunk:
                continue
            self._handle_frame(ws, chunk)

    def _handle_frame(self, ws, text: str):
        # ---- heartbeat / SockJS control frames ----
        if text == "o":           # socket open
            return
        if text.startswith("h"):  # heartbeat
            return
        if text.startswith("c"):  # close
            return

        logger.debug("TradovateFeed: frame: %s", text[:300])

        # ---- SockJS array format: a[msg, ...] ----
        # Server wraps messages as: a["{...}", "{...}"] or a[{...}]
        if text.startswith("a["):
            try:
                arr = json.loads(text[1:])  # strip leading 'a', parse as JSON array
                for item in arr:
                    if isinstance(item, str):
                        try:
                            self._handle_frame(ws, item)
                        except Exception:
                            pass
                    elif isinstance(item, dict):
                        try:
                            self._process_msg(ws, item)
                        except Exception as _e:
                            logger.error("TradovateFeed: _process_msg error: %s",
                                         _e, exc_info=True)
            except Exception as _e:
                logger.error("TradovateFeed: frame parse error: %s", _e, exc_info=True)
            return

        # ---- plain JSON envelope ----
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return

        self._process_msg(ws, msg)

    def _process_msg(self, ws, msg: dict):
        status = msg.get("s")
        event  = msg.get("e")
        data   = msg.get("d")

        # ---- chart event (real-time bar updates) ----
        if event == "chart" and isinstance(data, dict):
            for chart in data.get("charts", []):
                self._handle_chart_data(chart)
            return

        # Auth response — accept any 200 while not yet authenticated
        if status == 200 and not self._authenticated:
            self._authenticated = True
            print("TRADOVATE AUTH SUCCESS ✓", flush=True)
            logger.warning("TradovateFeed: authenticated ✓")
            try:
                self._send_subscriptions(ws)
            except Exception as _e:
                logger.error("TradovateFeed: _send_subscriptions error: %s",
                             _e, exc_info=True)
            return

        # Auth failure — try renewal before halting
        if status in (401, 403):
            logger.warning(
                "TradovateFeed: auth rejected (%s) — token expired, "
                "attempting auto-renewal…", status
            )
            self._authenticated = False
            self._auth_failed   = True   # _run_loop sees this and calls _try_renew_token
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
            return

        # Log unexpected responses for debugging
        if not self._authenticated:
            logger.warning("TradovateFeed: unexpected msg before auth: %s",
                           str(msg)[:200])

        # Chart data
        if isinstance(data, dict):
            self._handle_chart_data(data)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._handle_chart_data(item)

    def _send_one_subscription(self, ws, name: str, cfg: dict):
        """Send a single subscription frame on a live WS (for dynamic subscriptions)."""
        sym = cfg["symbol_id"]
        payload = {
            "symbol": sym,
            "chartDescription": {
                "underlyingType": "MinuteBar",
                "elementSize": cfg["element_size"],
                "elementSizeUnit": "UnderlyingUnits",
                "withHistogram": False,
            },
            "timeRange": {
                "asMuchAsElements": cfg["num_bars"],
            },
        }
        mid = self._next_id()
        frame = _make_frame("md/getChart", mid, payload)
        ws.send(frame)
        logger.info("TradovateFeed: dynamic subscribe %s → symbol=%s (msg %d)",
                    name, sym, mid)

    def _send_subscriptions(self, ws):
        for name, cfg in self._subscriptions.items():
            # symbol_id can be a numeric contract ID or a string contract name.
            # Tradovate's md/getChart accepts both.
            sym = cfg["symbol_id"]
            payload = {
                "symbol": sym,
                "chartDescription": {
                    "underlyingType": "MinuteBar",
                    "elementSize": cfg["element_size"],
                    "elementSizeUnit": "UnderlyingUnits",
                    "withHistogram": False,
                },
                "timeRange": {
                    "asMuchAsElements": cfg["num_bars"],
                },
            }
            mid = self._next_id()
            frame = _make_frame("md/getChart", mid, payload)
            ws.send(frame)
            logger.info("TradovateFeed: subscribed %s → symbol=%s (msg %d)",
                        name, sym, mid)

    def _register_chart_id(self, chart_id: int, bars_raw: list) -> Optional[str]:
        """
        Detect element_size from bar timestamps, find the matching subscription,
        and register chart_id → sub_name mapping.
        """
        # Detect element_size from bar timestamps (minutes between bars)
        element_size = None
        if len(bars_raw) >= 2:
            try:
                from datetime import datetime, timezone
                t0 = datetime.fromisoformat(
                    bars_raw[0]["timestamp"].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(
                    bars_raw[1]["timestamp"].replace("Z", "+00:00"))
                diff_min = abs(int((t1 - t0).total_seconds() / 60))
                if diff_min > 0:
                    element_size = diff_min
            except Exception:
                pass

        # Match to subscription by element_size
        matched = None
        for name, cfg in self._subscriptions.items():
            if element_size is None or cfg["element_size"] == element_size:
                if name not in self._chart_id_map.values():
                    matched = name
                    break

        if matched is None:
            # Fallback: assign to first unregistered subscription
            for name in self._subscriptions:
                if name not in self._chart_id_map.values():
                    matched = name
                    break

        if matched:
            self._chart_id_map[chart_id] = matched
            logger.warning("TradovateFeed: chart_id %s → %s (element_size=%s)",
                           chart_id, matched, element_size)
        return matched

    def _handle_chart_data(self, data: dict):
        """Parse incoming chart bars and push to cache.

        Response format:
          {"id": 579672, "s": "", "td": 20260526,
           "bars": [{"timestamp": "...", "open": ..., ...}]}
        """
        chart_id = data.get("id")
        bars_raw = data.get("bars") or data.get("bp") or []

        if not bars_raw:
            return

        # Map chart_id → sub_name using element_size detected from bar spacing
        # First time we see a chart_id, register it
        sub_name = self._chart_id_map.get(chart_id)
        if sub_name is None:
            sub_name = self._register_chart_id(chart_id, bars_raw)
        if sub_name is None:
            return

        parsed = []
        for b in bars_raw:
            bar = _parse_bar(b)
            if bar:
                parsed.append(bar)

        if not parsed:
            return

        with _cache_lock:
            q = _bar_cache.setdefault(sub_name, deque(maxlen=MAX_BARS_CACHED))
            for bar in parsed:
                # avoid exact duplicates by timestamp
                if q and q[-1].get("time") == bar.get("time"):
                    q[-1] = bar  # update last bar (live tick update)
                else:
                    q.append(bar)

        logger.debug("TradovateFeed: +%d bars for %s (total %d)",
                     len(parsed), sub_name, len(_bar_cache[sub_name]))

    def _find_sub_name(self, symbol_id) -> Optional[str]:
        if symbol_id is None:
            return None
        for name, cfg in self._subscriptions.items():
            if str(cfg["symbol_id"]) == str(symbol_id):
                return name
        return None

    def lookup_symbol_id(self, name: str) -> Optional[int]:
        """
        Look up a Tradovate numeric contract ID via the REST API.
        Called lazily when a symbol is requested but has no known ID.
        Result is returned to the caller for caching in TRADOVATE_SYMBOL_IDS.
        """
        import urllib.request
        import urllib.parse

        rest_base = (
            "https://live-d.tradovateapi.com/v1"
            if "live" in self._ws_url
            else "https://demo-d.tradovateapi.com/v1"
        )
        url = f"{rest_base}/contract/find?name={urllib.parse.quote(name)}"

        token = self._token
        if "eyJ" in token:
            token = token[token.index("eyJ"):]

        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                sym_id = data.get("id")
                if sym_id:
                    logger.info(
                        "TradovateFeed: REST lookup %s → id=%d", name, sym_id
                    )
                    return int(sym_id)
                logger.warning(
                    "TradovateFeed: REST lookup for %s returned no 'id': %s",
                    name, str(data)[:200]
                )
        except Exception as exc:
            logger.warning(
                "TradovateFeed: symbol REST lookup failed for %s: %s", name, exc
            )
        return None

    def _on_error(self, ws, error):
        logger.warning("TradovateFeed: WS error: %s", error)

    def _on_close(self, ws, code, msg):
        self._authenticated = False
        self._chart_id_map.clear()
        logger.warning("TradovateFeed: WS closed (%s %s)", code, msg)


def _parse_bar(b: dict) -> Optional[dict]:
    """Normalise a Tradovate bar dict to our standard OHLCV format."""
    try:
        # Tradovate bar keys: timestamp, open, high, low, close, upVolume,
        #                     downVolume, upTicks, downTicks, bidVolume, offerVolume
        ts_raw = b.get("timestamp") or b.get("t")
        if ts_raw is None:
            return None

        # ts_raw is ISO string or unix ms
        if isinstance(ts_raw, str):
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            ts = int(dt.timestamp())
        else:
            ts = int(ts_raw) // 1000 if ts_raw > 1e10 else int(ts_raw)

        return {
            "time":   ts,
            "open":   float(b.get("open",  b.get("o", 0))),
            "high":   float(b.get("high",  b.get("h", 0))),
            "low":    float(b.get("low",   b.get("l", 0))),
            "close":  float(b.get("close", b.get("c", 0))),
            "volume": int(b.get("upVolume", 0) or 0) + int(b.get("downVolume", 0) or 0),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Module-level singleton — used by web_dashboard.py
# ---------------------------------------------------------------------------
_feed: Optional[TradovateFeed] = None


def get_feed() -> Optional[TradovateFeed]:
    return _feed


def init_feed(token: str = "", use_live: bool = False,
              symbols: Optional[list] = None) -> TradovateFeed:
    """
    Create and start the global feed singleton.
    Call from web_dashboard.py startup after loading token from temp file.

    symbols: list of (name, symbol_id, element_size) tuples to pre-subscribe.
             Defaults to MESM6 on 1/3/5m if not specified.
             Pass symbol_id=0 to trigger a REST lookup at subscription time.
    """
    global _feed
    _feed = TradovateFeed(token=token, use_live=use_live)

    # Default subscriptions (MESM6 known ID; others discovered lazily via REST)
    default_subs = symbols or [
        ("MESM6",   3961353, 5),   # MES 5m  (known ID — always works)
        ("MESM6_1", 3961353, 1),   # MES 1m
        ("MESM6_3", 3961353, 3),   # MES 3m
        ("MNQM6",   "MNQM6", 5),   # MNQ 5m  (string name fallback — ID unknown with limited ACL)
        ("MNQM6_1", "MNQM6", 1),   # MNQ 1m
        ("MNQM6_3", "MNQM6", 3),   # MNQ 3m
    ]
    for name, sym_id, element_size in default_subs:
        if sym_id:
            _feed.subscribe(name, symbol_id=sym_id, element_size=element_size)
        # 0 / None IDs are looked up lazily on first chart request via lookup_symbol_id()

    _feed.start()
    return _feed
