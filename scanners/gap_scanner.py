"""
scanners/gap_scanner.py
=======================
Overnight Gap Scanner -- two-stage: register at open, confirm throughout day.

Stage 1 -- Gap Register (fires once at market open)
-----------------------------------------------------
At 9:30 ET (stocks) or 00:00 UTC (crypto), scan symbols for overnight gaps.
Qualifying gaps are added to a watchlist queue -- NO signal fired yet.

Stage 2 -- Watchdog Monitor (runs every 5 min throughout session)
------------------------------------------------------------------
For each symbol in the watchlist, fetch recent 5m candles and check whether
the gap setup has confirmed:

  Gap & Go (momentum):
    - Price still above gap open (gap hasn't reversed)
    - Recent 5m candle is green (continuation)
    - Volume still elevated vs average
    - Must confirm within 2 hours of open or expires

  Gap Fill (mean reversion / fade):
    - Price still in gap territory (not already filled)
    - Reversal candle visible (bearish engulf, doji + wick, or early turn)
    - Volume fading on gap extension
    - Must confirm within 3 hours or expires

Expiry / removal conditions:
    - Gap & Go: price fills back below gap open (setup failed)
    - Gap Fill: price already >75% filled back to prev close (too late)
    - Stocks: 3:45 ET end of day clears all
    - Crypto: 20h after register clears entry
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import pandas as pd

from scanners.base_scanner import BaseEventScanner

try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning parameters
# ---------------------------------------------------------------------------

# Gap thresholds
GAP_MIN_PCT_STOCKS      = 2.0
GAP_MIN_PCT_CRYPTO      = 1.5
GAP_GO_MIN_PCT          = 3.0
GAP_GO_VOLUME_MIN       = 1.4
GAP_FILL_MAX_PCT        = 8.0
GAP_FILL_VOLUME_MAX     = 1.8

# Stop / target geometry
GAP_GO_SL_PCT           = 1.5
GAP_GO_TP_MULT          = 2.0
GAP_FILL_TP_FILL_RATIO  = 0.65
GAP_FILL_SL_BUFFER_PCT  = 0.5

# Confidence scores
GAP_GO_SCORE_BASE       = 0.72
GAP_FILL_SCORE_BASE     = 0.65

# Firing windows
STOCK_OPEN_WINDOW_START  = (9, 30)
STOCK_OPEN_WINDOW_END    = (10, 15)  # widened: 45 min window covers late bot starts
CRYPTO_OPEN_WINDOW_START = (0, 0)
CRYPTO_OPEN_WINDOW_END   = (0, 15)

# Watchdog monitor interval
MONITOR_INTERVAL_SEC    = 300   # check watchlist every 5 minutes

# Expiry: how long a setup can sit in queue before being discarded
GAP_GO_MAX_WAIT_HOURS   = 2.0   # momentum fades fast
GAP_FILL_MAX_WAIT_HOURS = 3.0   # fills can take longer

# Gap fill: if price has already retraced this fraction back to prev_close, too late
GAP_FILL_TOO_LATE_RATIO = 0.75  # 75% already filled = missed it

# Cooldown: 20h prevents re-register if bot restarts during the day
COOLDOWN_HOURS = 20.0

# Stock end-of-day cutoff (ET)
STOCK_EOD_HOUR   = 15
STOCK_EOD_MINUTE = 45


# ---------------------------------------------------------------------------
# GapSetup -- one entry in the watchlist queue
# ---------------------------------------------------------------------------

@dataclass
class GapSetup:
    symbol:           str
    asset_class:      str           # "stock" or "crypto"
    gap_type:         str           # "gap_and_go" or "gap_fill"
    direction:        str           # "long" or "short"
    gap_pct:          float         # signed, e.g. +4.2 or -2.8
    prev_close:       float
    gap_open:         float         # today's open price
    vol_spike:        float         # volume spike at open
    registered_at:    datetime      # UTC
    attempts:         int = 0       # how many monitor cycles checked
    fired:            bool = False  # True once signal pushed

    @property
    def abs_gap(self) -> float:
        return abs(self.gap_pct)

    def age_hours(self, now_utc: datetime) -> float:
        return (now_utc - self.registered_at).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# GapScanner
# ---------------------------------------------------------------------------

class GapScanner(BaseEventScanner):
    """
    Two-stage gap scanner.

    Stage 1: detect overnight gaps at market open, add to watchlist.
    Stage 2: monitor watchlist every 5 min, fire signal on confirmation.
    """

    def __init__(
        self,
        stock_symbols:  Optional[List[str]] = None,
        crypto_symbols: Optional[List[str]] = None,
        enable_stocks:  bool = True,
        enable_crypto:  bool = True,
    ):
        super().__init__(
            name               = "GapScanner",
            cooldown_hours     = COOLDOWN_HOURS,
            check_interval_sec = 30,
        )
        self.stock_symbols  = stock_symbols  or []
        self.crypto_symbols = crypto_symbols or []
        self.enable_stocks  = enable_stocks
        self.enable_crypto  = enable_crypto

        # Watchlist queue: symbol -> GapSetup
        self._watchlist: Dict[str, GapSetup] = {}

        # Session tracking
        self._stock_session_scanned:  Optional[str] = None
        self._crypto_session_scanned: Optional[str] = None

        # Throttle monitor checks
        self._last_monitor_utc: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Main loop (called every 30s by base class thread)
    # ------------------------------------------------------------------

    def run_once(self) -> None:
        now_et  = self.et_now()
        now_utc = self.utc_now()

        # --- Stage 1: register gaps at market open ---
        if self.enable_stocks and self.stock_symbols:
            in_window = (
                (now_et.hour, now_et.minute) >= STOCK_OPEN_WINDOW_START
                and (now_et.hour, now_et.minute) < STOCK_OPEN_WINDOW_END
            )
            today_str = now_et.strftime("%Y-%m-%d")
            if in_window and self._stock_session_scanned != today_str:
                self._stock_session_scanned = today_str
                logger.info("[GapScanner] Stock open window %s -- registering gaps for %d symbols",
                            now_et.strftime("%H:%M ET"), len(self.stock_symbols))
                self._register_stock_gaps()
            elif not in_window and self._stock_session_scanned != today_str:
                logger.debug("[GapScanner] Stock gap window not yet/already passed: %s ET",
                             now_et.strftime("%H:%M"))

        if self.enable_crypto and self.crypto_symbols:
            in_window = (
                (now_utc.hour, now_utc.minute) >= CRYPTO_OPEN_WINDOW_START
                and (now_utc.hour, now_utc.minute) < CRYPTO_OPEN_WINDOW_END
            )
            today_str = now_utc.strftime("%Y-%m-%d")
            if in_window and self._crypto_session_scanned != today_str:
                self._crypto_session_scanned = today_str
                logger.info("[GapScanner] Crypto open window -- registering gaps for %d symbols",
                            len(self.crypto_symbols))
                self._register_crypto_gaps()

        # --- Stage 2: watchdog monitor (throttled to every 5 min) ---
        if self._watchlist:
            due = (
                self._last_monitor_utc is None
                or (now_utc - self._last_monitor_utc).total_seconds() >= MONITOR_INTERVAL_SEC
            )
            if due:
                self._last_monitor_utc = now_utc
                self._monitor_watchlist(now_utc, now_et)

        # --- EOD: clear stock watchlist ---
        if (now_et.hour, now_et.minute) >= (STOCK_EOD_HOUR, STOCK_EOD_MINUTE):
            stock_keys = [k for k, v in self._watchlist.items() if v.asset_class == "stock"]
            for k in stock_keys:
                logger.info("[GapScanner] EOD -- removing %s from watchlist", k)
                del self._watchlist[k]

    # ------------------------------------------------------------------
    # Stage 1: gap registration
    # ------------------------------------------------------------------

    def _register_stock_gaps(self) -> None:
        if not _YF_OK:
            logger.warning("[GapScanner] yfinance not available -- stock gap register skipped")
            return

        # Dynamic: ask Alpaca who is actually moving today.
        # Fall back to static watchlist only if Alpaca screener is unavailable.
        symbols = self._get_alpaca_gap_symbols() or self.stock_symbols

        if not symbols:
            logger.warning("[GapScanner] No symbols to scan for gaps")
            return

        logger.info("[GapScanner] Scanning %d symbols for gaps", len(symbols))
        for symbol in symbols:
            if self.is_on_cooldown(symbol) or symbol in self._watchlist:
                continue
            try:
                self._try_register_gap(symbol, "stock")
                time.sleep(0.15)
            except Exception as e:
                logger.warning("[GapScanner] Error registering stock gap %s: %s", symbol, e)

    # Minimum filters to exclude penny stocks and illiquid micro-caps
    SCREENER_MIN_PRICE  = 10.0      # skip anything under $10 — low price stocks often lack candle data
    SCREENER_MIN_VOLUME = 500_000   # skip anything under 500K shares at open

    def _get_alpaca_gap_symbols(self, top: int = 25) -> Optional[List[str]]:
        """
        Query Alpaca screener for today's top market movers (gainers + losers).

        At 9:30 ET the percent_change on these is essentially the overnight gap.
        Penny stocks and low-volume names are filtered out — they gap huge on
        tiny volume and are untradeable.

        Returns a flat symbol list, or None if the screener is unavailable.
        """
        try:
            from alpaca.data import ScreenerClient, MarketMoversRequest, MarketType
            from config import ALPACA_API_KEY, ALPACA_SECRET_KEY

            if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
                logger.debug("[GapScanner] Alpaca keys not configured -- skipping screener")
                return None

            client = ScreenerClient(
                api_key    = ALPACA_API_KEY,
                secret_key = ALPACA_SECRET_KEY,
            )
            req    = MarketMoversRequest(market_type=MarketType.STOCKS, top=top)
            result = client.get_market_movers(req)

            raw_gainers = result.gainers if hasattr(result, "gainers") and result.gainers else []
            raw_losers  = result.losers  if hasattr(result, "losers")  and result.losers  else []
            all_movers  = raw_gainers + raw_losers

            # Filter: price >= $5, volume >= 500K
            tradeable = [
                m for m in all_movers
                if getattr(m, "price",  0) >= self.SCREENER_MIN_PRICE
                and getattr(m, "volume", 0) >= self.SCREENER_MIN_VOLUME
            ]

            skipped = len(all_movers) - len(tradeable)
            logger.info(
                "[GapScanner] Alpaca screener: %d movers, %d tradeable (filtered %d penny/illiquid)",
                len(all_movers), len(tradeable), skipped,
            )

            if tradeable:
                return [m.symbol for m in tradeable]

            logger.warning("[GapScanner] Alpaca screener returned no tradeable movers -- falling back to watchlist")
            return None

        except Exception as e:
            logger.warning("[GapScanner] Alpaca screener unavailable (%s) -- falling back to watchlist", e)
            return None

    def _register_crypto_gaps(self) -> None:
        if not _YF_OK:
            logger.warning("[GapScanner] yfinance not available -- crypto gap register skipped")
            return
        for symbol in self.crypto_symbols:
            if self.is_on_cooldown(symbol) or symbol in self._watchlist:
                continue
            try:
                self._try_register_gap(symbol, "crypto")
                time.sleep(0.3)
            except Exception as e:
                logger.warning("[GapScanner] Error registering crypto gap %s: %s", symbol, e)

    def _try_register_gap(self, symbol: str, asset_class: str) -> None:
        """Fetch daily candles, compute gap, add to watchlist if qualifies."""
        yf_symbol = symbol.replace("/", "-")
        ticker = yf.Ticker(yf_symbol)
        hist   = ticker.history(period="5d", interval="1d", auto_adjust=True)

        if hist is None or len(hist) < 2:
            return

        prev_close = float(hist["Close"].iloc[-2])
        today_open = float(hist["Open"].iloc[-1])

        if prev_close <= 0 or today_open <= 0:
            logger.info("[GapScanner] %s -- bad price data (prev_close=%.4f open=%.4f) -- skip",
                        symbol, prev_close, today_open)
            return

        gap_pct   = (today_open - prev_close) / prev_close * 100.0
        min_pct   = GAP_MIN_PCT_STOCKS if asset_class == "stock" else GAP_MIN_PCT_CRYPTO

        if abs(gap_pct) < min_pct:
            logger.info("[GapScanner] %s -- gap %.2f%% below min %.1f%% -- skip",
                        symbol, gap_pct, min_pct)
            return

        # Volume spike: use 5-min intraday bars for today vs rolling avg.
        # Daily yfinance bars have near-zero volume at 9:30 ET (bar still forming)
        # which was causing vol_spike ≈ 0 and blocking gap_and_go classification.
        try:
            intraday = self._get_intraday_candles(symbol, asset_class)
            if intraday is not None and len(intraday) >= 3:
                # First few bars of the day vs rolling 20-bar avg
                today_vol  = float(intraday["Volume"].iloc[:3].sum())   # first 15 min
                rolling_avg = float(intraday["Volume"].rolling(20).mean().iloc[-1])
                # Scale: 3 bars vs 1-bar avg → divide by 3
                vol_spike  = (today_vol / 3) / rolling_avg if rolling_avg > 0 else 1.0
            else:
                avg_vol    = float(hist["Volume"].iloc[:-1].mean())
                today_vol  = float(hist["Volume"].iloc[-1])
                vol_spike  = today_vol / avg_vol if avg_vol > 0 else 1.0
        except Exception:
            vol_spike = 1.0

        gap_type = self._classify_gap(abs(gap_pct), vol_spike, min_pct)
        if gap_type is None:
            logger.info("[GapScanner] %s -- gap %.2f%% vol=%.2fx -- no classification "
                        "(need >=%.1f%%+%.1fx for G&G or <%.1fx for fill) -- skip",
                        symbol, gap_pct, vol_spike,
                        GAP_GO_MIN_PCT, GAP_GO_VOLUME_MIN, GAP_FILL_VOLUME_MAX)
            return

        direction = (
            ("long"  if gap_pct > 0 else "short") if gap_type == "gap_and_go"
            else ("short" if gap_pct > 0 else "long")   # gap fill fades the gap
        )

        setup = GapSetup(
            symbol        = symbol,
            asset_class   = asset_class,
            gap_type      = gap_type,
            direction     = direction,
            gap_pct       = gap_pct,
            prev_close    = prev_close,
            gap_open      = today_open,
            vol_spike     = vol_spike,
            registered_at = self.utc_now(),
        )

        self._watchlist[symbol] = setup
        logger.info(
            "[GapScanner] REGISTERED %s  %s %s  gap=%.2f%%  vol=%.2fx  -- watching for confirmation",
            symbol, gap_type.upper(), direction.upper(), gap_pct, vol_spike,
        )

    # ------------------------------------------------------------------
    # Stage 2: watchdog monitor
    # ------------------------------------------------------------------

    def _monitor_watchlist(self, now_utc: datetime, now_et: datetime) -> None:
        if not self._watchlist:
            return

        logger.info("[GapScanner] Monitoring %d queued gap setup(s)", len(self._watchlist))
        to_remove = []

        for symbol, setup in list(self._watchlist.items()):

            if setup.fired:
                to_remove.append(symbol)
                continue

            age_h = setup.age_hours(now_utc)

            # --- Expiry check ---
            max_wait = GAP_GO_MAX_WAIT_HOURS if setup.gap_type == "gap_and_go" else GAP_FILL_MAX_WAIT_HOURS
            if age_h > max_wait:
                logger.info("[GapScanner] %s expired after %.1fh without confirming -- removed", symbol, age_h)
                to_remove.append(symbol)
                continue

            setup.attempts += 1

            try:
                candles = self._get_intraday_candles(symbol, setup.asset_class)
                if candles is None or len(candles) < 2:
                    continue

                live_price = float(candles["Close"].iloc[-1])

                # --- Failure / too-late checks ---
                if setup.gap_type == "gap_and_go":
                    # If price has filled back below gap open, setup failed
                    if setup.gap_pct > 0 and live_price < setup.gap_open * 0.995:
                        logger.info("[GapScanner] %s GAP-GO failed -- price filled back below open", symbol)
                        to_remove.append(symbol)
                        continue
                    if setup.gap_pct < 0 and live_price > setup.gap_open * 1.005:
                        logger.info("[GapScanner] %s GAP-GO failed -- price filled back above open", symbol)
                        to_remove.append(symbol)
                        continue

                elif setup.gap_type == "gap_fill":
                    # If price already retraced 75%+ back to prev_close, too late to enter
                    gap_size   = abs(setup.gap_open - setup.prev_close)
                    retraced   = abs(live_price - setup.gap_open)
                    fill_ratio = retraced / gap_size if gap_size > 0 else 0
                    if fill_ratio >= GAP_FILL_TOO_LATE_RATIO:
                        logger.info("[GapScanner] %s GAP-FILL too late -- %.0f%% already filled", symbol, fill_ratio * 100)
                        to_remove.append(symbol)
                        continue

                # --- Confirmation check ---
                confirmed, payload = self._check_confirmation(setup, candles, live_price)

                if confirmed and payload:
                    logger.info(
                        "[GapScanner] %s CONFIRMED after %.1fh (%d attempts) -- firing signal",
                        symbol, age_h, setup.attempts,
                    )
                    result = self.push_signal(payload)
                    if result.get("accepted"):
                        setup.fired = True
                        self.mark_fired(symbol)
                    to_remove.append(symbol)

                time.sleep(0.3)

            except Exception as e:
                logger.warning("[GapScanner] Monitor error for %s: %s", symbol, e)

        for symbol in set(to_remove):
            self._watchlist.pop(symbol, None)

    # ------------------------------------------------------------------
    # Confirmation logic
    # ------------------------------------------------------------------

    def _check_confirmation(
        self,
        setup: GapSetup,
        candles: pd.DataFrame,
        live_price: float,
    ):
        """
        Returns (confirmed: bool, payload: dict or None).
        """
        if setup.gap_type == "gap_and_go":
            return self._confirm_gap_and_go(setup, candles, live_price)
        else:
            return self._confirm_gap_fill(setup, candles, live_price)

    def _confirm_gap_and_go(self, setup: GapSetup, candles: pd.DataFrame, live_price: float):
        """
        Gap & Go confirmation:
          1. Price still above gap open (gap holding)
          2. Last closed 5m candle is green (direction continues)
          3. Volume on last candle elevated vs candle average
        """
        last = candles.iloc[-2]   # last CLOSED candle (-1 is still forming)
        o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])

        # 1. Price holding above gap open
        if setup.gap_pct > 0 and live_price < setup.gap_open:
            return False, None
        if setup.gap_pct < 0 and live_price > setup.gap_open:
            return False, None

        # 2. Last candle confirms direction
        if setup.direction == "long" and c <= o:
            return False, None
        if setup.direction == "short" and c >= o:
            return False, None

        # 3. Volume elevated on recent candle
        try:
            avg_vol_5m = float(candles["Volume"].iloc[:-1].mean())
            last_vol   = float(last["Volume"])
            vol_spike  = last_vol / avg_vol_5m if avg_vol_5m > 0 else 1.0
        except Exception:
            vol_spike = setup.vol_spike

        if vol_spike < 1.2:
            return False, None

        payload = self._build_payload(setup, live_price, vol_spike)
        return True, payload

    def _confirm_gap_fill(self, setup: GapSetup, candles: pd.DataFrame, live_price: float):
        """
        Gap Fill confirmation:
          1. Price still in gap zone (hasn't already snapped back)
          2. Reversal signal: bearish candle after gap up, or bullish after gap down
             OR: price starting to move back toward prev_close
          3. Volume fading (not surging further in gap direction)
        """
        last = candles.iloc[-2]
        o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])

        # 1. Price still in gap zone (between gap_open and prev_close extended)
        if setup.gap_pct > 0:
            # Gap up: price should still be above prev_close
            if live_price <= setup.prev_close:
                return False, None
        else:
            # Gap down: price should still be below prev_close
            if live_price >= setup.prev_close:
                return False, None

        # 2. Reversal signal
        reversal = False

        if setup.gap_pct > 0:
            # Gap up -- need bearish reversal signal
            # Bearish engulf or red candle with upper wick
            is_red = c < o
            wick_ratio = (h - max(o, c)) / (h - l) if (h - l) > 0 else 0
            has_wick = wick_ratio > 0.35   # upper wick > 35% of range
            if is_red or has_wick:
                reversal = True
            # OR: price has already started moving back (early turn)
            if live_price < setup.gap_open * 0.998:
                reversal = True

        else:
            # Gap down -- need bullish reversal signal
            is_green = c > o
            wick_ratio = (min(o, c) - l) / (h - l) if (h - l) > 0 else 0
            has_wick = wick_ratio > 0.35
            if is_green or has_wick:
                reversal = True
            if live_price > setup.gap_open * 1.002:
                reversal = True

        if not reversal:
            return False, None

        # 3. Volume not surging in gap direction (don't fade a momentum breakout)
        try:
            avg_vol_5m = float(candles["Volume"].iloc[:-1].mean())
            last_vol   = float(last["Volume"])
            vol_spike  = last_vol / avg_vol_5m if avg_vol_5m > 0 else 1.0
        except Exception:
            vol_spike = setup.vol_spike

        if vol_spike > 2.5:
            # Volume surging too hard -- gap may be legitimate, don't fade
            logger.info("[GapScanner] %s GAP-FILL blocked -- volume spike %.2fx too strong to fade", setup.symbol, vol_spike)
            return False, None

        payload = self._build_payload(setup, live_price, vol_spike)
        return True, payload

    # ------------------------------------------------------------------
    # Payload builder
    # ------------------------------------------------------------------

    def _build_payload(self, setup: GapSetup, live_price: float, vol_spike: float) -> Dict[str, Any]:
        abs_gap = setup.abs_gap

        if setup.gap_type == "gap_and_go":
            sl_pct  = GAP_GO_SL_PCT
            tp_pct  = max(round(abs_gap * GAP_GO_TP_MULT, 2), 1.5)
            score   = min(0.92, GAP_GO_SCORE_BASE
                          + min(0.15, (abs_gap - GAP_GO_MIN_PCT) * 0.03)
                          + min(0.07, (vol_spike - 1.0) * 0.05))
            reason  = "GAP-GO: %.1f%% gap %s vol=%.1fx -- confirmed continuation" % (
                setup.gap_pct, "up" if setup.gap_pct > 0 else "down", vol_spike)
            trail   = "two_bar"

        else:
            fill_target = abs((setup.prev_close - live_price) * GAP_FILL_TP_FILL_RATIO)
            tp_pct      = max(round(fill_target / live_price * 100, 2), 1.0)
            if setup.gap_pct > 0:
                sl_price = setup.gap_open * (1 + GAP_FILL_SL_BUFFER_PCT / 100)
            else:
                sl_price = setup.gap_open * (1 - GAP_FILL_SL_BUFFER_PCT / 100)
            sl_pct  = max(round(abs(sl_price - live_price) / live_price * 100, 2), 0.5)
            score   = min(0.88, GAP_FILL_SCORE_BASE
                          + min(0.15, abs_gap * 0.015)
                          - min(0.10, (vol_spike - 1.0) * 0.08))
            reason  = "GAP-FILL: %.1f%% gap %s vol=%.1fx -- reversal confirmed" % (
                setup.gap_pct, "up" if setup.gap_pct > 0 else "down", vol_spike)
            trail   = "none"

        # Cap stop at 2.0% — breakout receiver hard-rejects > 2.5%, leave buffer
        sl_pct = min(sl_pct, 2.0)

        if setup.direction == "long":
            structural_stop = live_price * (1 - sl_pct / 100)
        else:
            structural_stop = live_price * (1 + sl_pct / 100)

        now_utc = self.utc_now()

        return {
            "symbol":                 setup.symbol,
            "asset_class":            setup.asset_class,
            "direction":              setup.direction,
            "entry_price":            live_price,
            "current_price":          live_price,
            "move_pct":               setup.gap_pct,
            "volume_spike":           round(vol_spike, 3),
            "confidence":             round(score, 3),
            "escalation":             1,
            "timestamp":              now_utc.isoformat(),
            "signal_source":          "gap_scanner",
            "strategy_name":          "gap_scanner",
            "broker":                 ("alpaca" if setup.asset_class == "stock" else "coinbase"),
            "stop_loss_pct":          sl_pct,
            "take_profit_pct":        tp_pct,
            "structural_stop_price":  round(structural_stop, 6),
            "gap_pct":                round(setup.gap_pct, 3),
            "gap_type":               setup.gap_type,
            "prev_close":             round(setup.prev_close, 6),
            "today_open":             round(setup.gap_open, 6),
            "preferred_trail_mode":   trail,
            "reason":                 reason,
            "bars_since_breakout":    0,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _classify_gap(self, abs_gap_pct: float, vol_spike: float,
                      min_pct: float = GAP_MIN_PCT_STOCKS) -> Optional[str]:
        # Gap & Go: strong gap + volume surge = momentum continuation
        if abs_gap_pct >= GAP_GO_MIN_PCT and vol_spike >= GAP_GO_VOLUME_MIN:
            return "gap_and_go"
        # Gap Fill: moderate gap + not surging = fade/mean reversion
        # Bug fix: was hardcoded to GAP_MIN_PCT_STOCKS (2.0), ignoring crypto (1.5)
        if abs_gap_pct >= min_pct and abs_gap_pct < GAP_FILL_MAX_PCT:
            if vol_spike < GAP_FILL_VOLUME_MAX:
                return "gap_fill"
        return None

    def _get_intraday_candles(self, symbol: str, asset_class: str) -> Optional[pd.DataFrame]:
        """Fetch last 30 5-minute candles for confirmation checks."""
        if not _YF_OK:
            return None
        try:
            yf_symbol = symbol.replace("/", "-")
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(period="1d", interval="5m", auto_adjust=True)
            if df is None or len(df) < 3:
                return None
            return df
        except Exception as e:
            logger.debug("[GapScanner] Could not fetch intraday candles for %s: %s", symbol, e)
            return None

    def watchlist_summary(self) -> List[Dict]:
        """Return current watchlist state -- useful for dashboard / logging."""
        now_utc = self.utc_now()
        return [
            {
                "symbol":       s.symbol,
                "gap_type":     s.gap_type,
                "direction":    s.direction,
                "gap_pct":      round(s.gap_pct, 2),
                "age_hours":    round(s.age_hours(now_utc), 2),
                "attempts":     s.attempts,
            }
            for s in self._watchlist.values()
        ]
