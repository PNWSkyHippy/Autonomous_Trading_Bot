"""
core/breakout_receiver.py  (hardened v2.2)
=========================================
Receives inbound breakout signals from BreakoutScanner and injects them
into the trade executor after a fast validation checklist.

v2.2 additions:
  - Understands sender's source_broker / source_price / source_timestamp
  - No longer relies solely on scanner.get_current_price(); falls back to
    scanner fetchers directly
  - Normalizes broker names and preserves both preferred_broker + source_broker
  - Blacklist rejections now return the same structured response shape
"""

import logging
import threading
import time
from datetime import datetime, timezone, date
from typing import Optional, Dict, Any, Tuple, List

import config
from data.database import db
from core.risk_manager import risk_manager
from core.trade_executor import TradeExecutor

logger = logging.getLogger(__name__)

# ── Tuning constants ────────────────────────────────────────────────────────
SIGNAL_TTL_SEC          = 60
VOLUME_SPIKE_MIN        = 1.5   # Opus retune: 2.0 was filtering too many valid breakouts; 1.5 recovers edge
WICK_BODY_MAX_RATIO     = 2.0
GAP_TRAP_MAX_PCT        = 30.0
PRICE_DRIFT_MAX_PCT     = 0.5
STOP_MAX_DISTANCE_STOCK = 2.5
STOP_MAX_DISTANCE_CRYPTO= 3.5
RSI_LONG_MIN            = 50.0
RSI_LONG_EXHAUST        = 82.0
RSI_SHORT_MAX           = 50.0
RSI_SHORT_EXHAUST       = 18.0
MARKET_CRASH_THRESHOLD  = -2.0
MARKET_RALLY_THRESHOLD  = +2.0
MIN_SOFT_PASSES         = 0
MIN_PRICE_CRYPTO        = 0.01   # reject sub-penny crypto — micro-caps with no liquidity
MAX_CONCURRENT_BREAKOUTS= 8      # Opus retune: was 2 — single winner held all day blocked all new entries; 8 lets bot trade
PUMP_VOLUME_SPIKE_MAX   = 15.0   # volume spike above this = coordinated pump (organic breaks rarely > 10x)
PUMP_MOVE_MAX_CRYPTO    = 15.0   # crypto move above this % = likely already at pump peak, not start

# Quiet hours — crypto breakouts during thin market hours (00:00–05:00 UTC) are
# mostly noise: no institutional flow, spreads widen, micro-moves look like breakouts.
# US session (13:00–21:00 UTC) and Asia open (00:00 excluded) are the liquid windows.
QUIET_HOURS_START_UTC   = 0      # midnight UTC
QUIET_HOURS_END_UTC     = 5      # 5am UTC (covers US dead zone + thin Asia pre-market)

BREAKOUT_SYMBOL_BLACKLIST = {"MLN/USD", "MLNUSD", "MLN/USDT", "MLNUSDT"}

# Reject any symbol whose quote currency is not USD.
# USDC, USDT, BUSD pairs can't be priced by Kraken's USD feed and get stuck
# in the position monitor indefinitely, jamming the scan loop.
_REJECTED_QUOTE_CURRENCIES = {"/USDC", "/USDT", "/BUSD", "/DAI", "/TUSD", "USDC", "USDT"}

def _is_bad_quote_currency(symbol: str) -> bool:
    s = symbol.upper()
    return any(s.endswith(q) for q in _REJECTED_QUOTE_CURRENCIES)

def _is_breakout_scanner_payload(p: Dict[str, Any]) -> bool:
    return (
        p.get("signal_source") == "breakout_scanner"
        or p.get("strategy_name") == "breakout_scanner"
        or p.get("source") == "breakout_scanner"
    )

REQUIRED_FIELDS = [
    "symbol", "asset_class", "direction",
    "entry_price", "current_price",
    "move_pct", "volume_spike",
    "confidence", "escalation", "timestamp",
]

EXECUTION_BROKERS = {"kraken", "coinbase", "alpaca", "ibkr"}
BROKER_ALIASES = {
    "krakenpro": "kraken",
    "coinbasepro": "coinbase",
    "coinbase_advanced": "coinbase",
    "interactivebrokers": "ibkr",
    "interactive_brokers": "ibkr",
    "ib": "ibkr",
    "ibkr": "ibkr",
    "alpaca_markets": "alpaca",
}


class ChecklistResult:
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"

    def __init__(self, status: str, reason: str):
        self.status = status
        self.passed = (status == self.PASS)
        self.reason = reason

    @classmethod
    def ok(cls, reason: str) -> "ChecklistResult":
        return cls(cls.PASS, reason)

    @classmethod
    def fail(cls, reason: str) -> "ChecklistResult":
        return cls(cls.FAIL, reason)

    @classmethod
    def skip(cls, reason: str) -> "ChecklistResult":
        return cls(cls.SKIP, reason)

    def __repr__(self):
        return f"[{self.status}] {self.reason}"


class BreakoutReceiver:
    # Class-level slot counter — shared across all threads, prevents burst race.
    # When 5 FundingScanner signals arrive simultaneously, each grabs a slot
    # BEFORE any is committed to the DB, so _check_concurrent_breakouts using
    # DB-only counts would allow all 5 through.  This counter reserves in-flight
    # slots atomically; it is decremented after the trade completes or fails.
    _slot_lock        = threading.Lock()
    _slots_used       = 0    # breakout positions currently open OR being processed
    _slots_reserved   = set()  # symbols whose slot is currently held (thread-safe via _slot_lock)

    def __init__(self):
        self._executor: Optional[TradeExecutor] = None
        self._scanner_ref = None
        self._drift_state: Dict[str, Any] = {}
        self._pending_lock  = threading.Lock()
        self._pending_symbols: set = set()  # symbols mid-processing (race guard)

    def set_executor(self, executor: TradeExecutor):
        self._executor = executor

    def set_scanner(self, scanner):
        self._scanner_ref = scanner

    # ──────────────────────────────────────────────────────────────────────
    # Public entry
    # ──────────────────────────────────────────────────────────────────────

    def receive_signal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._drift_state = {}   # wipe state from any previous call
        sym            = payload.get("symbol", "?")
        direction      = payload.get("direction", "?")
        esc            = payload.get("escalation", 0)
        ts_in          = payload.get("timestamp", "")
        signal_age_sec = self._signal_age(ts_in)

        # ── Duplicate-signal race guard ──────────────────────────────────────
        # Prevents two signals for the same symbol arriving within the same
        # second from both passing _check_no_open_position before the first
        # trade is committed to the DB.
        with self._pending_lock:
            if sym in self._pending_symbols:
                reason = f"Duplicate signal rejected — {sym} already being processed"
                logger.warning(f"[BREAKOUT RECV] {reason}")
                return {
                    "accepted": False,
                    "execution_eligible": False,
                    "failure_class": "duplicate_signal",
                    "reason": reason,
                    "trade_id": None,
                    "checklist": {},
                }
            self._pending_symbols.add(sym)
        try:
            return self._receive_signal_inner(payload, sym, direction, esc,
                                              ts_in, signal_age_sec)
        finally:
            with self._pending_lock:
                self._pending_symbols.discard(sym)
            # Release the slot only if this signal reserved one (i.e., passed the
            # concurrent check).  Signals that failed before reaching that check
            # never took a slot and must NOT decrement.
            with BreakoutReceiver._slot_lock:
                if sym in BreakoutReceiver._slots_reserved:
                    BreakoutReceiver._slots_reserved.discard(sym)
                    BreakoutReceiver._slots_used = max(0, BreakoutReceiver._slots_used - 1)

    def _receive_signal_inner(
        self,
        payload: Dict[str, Any],
        sym: str,
        direction: str,
        esc: int,
        ts_in: str,
        signal_age_sec: float,
    ) -> Dict[str, Any]:

        broker_hint   = self._normalize_broker_name(payload.get("broker", ""))
        source_broker = self._normalize_broker_name(
            payload.get("source_broker", broker_hint or "")
        )

        if sym.upper() in BREAKOUT_SYMBOL_BLACKLIST:
            reason = f"{sym} in breakout symbol blacklist"
            logger.info(f"[BREAKOUT RECV] {sym} rejected — {reason}")
            return {
                "accepted": False,
                "execution_eligible": False,
                "failure_class": "checklist_rejected",
                "reason": reason,
                "trade_id": None,
                "checklist": {},
            }

        if _is_bad_quote_currency(sym):
            reason = f"{sym} has non-USD quote currency — unpriceable by Kraken feed"
            logger.warning(f"[BREAKOUT RECV] {sym} rejected — {reason}")
            return {
                "accepted": False,
                "execution_eligible": False,
                "failure_class": "bad_quote_currency",
                "reason": reason,
                "trade_id": None,
                "checklist": {},
            }

        logger.info(
            f"[BREAKOUT RECV] {sym} {str(direction).upper()} "
            f"esc={esc} move={payload.get('move_pct', 0):+.2f}% "
            f"age={signal_age_sec:.0f}s vol={payload.get('volume_spike', 0):.2f}x "
            f"broker={broker_hint or 'none'} source_broker={source_broker or 'none'} "
            f"stop={payload.get('structural_stop_price', 'none')} "
            f"bars_since={payload.get('bars_since_breakout', '?')}"
        )

        missing = [f for f in REQUIRED_FIELDS if payload.get(f) is None]
        if missing:
            reason = f"Missing required fields: {', '.join(missing)}"
            logger.warning(f"[BREAKOUT RECV] INVALID PAYLOAD {sym} — {reason}")
            return {
                "accepted": False,
                "execution_eligible": False,
                "failure_class": "invalid_payload",
                "reason": reason,
                "trade_id": None,
                "checklist": {},
            }

        hard_results, soft_results = self._run_checklist(payload, signal_age_sec)

        hard_passed = all(
            r.status in (ChecklistResult.PASS, ChecklistResult.SKIP)
            for r in hard_results
        )
        hard_skips  = sum(1 for r in hard_results if r.status == ChecklistResult.SKIP)
        soft_passes = sum(1 for r in soft_results if r.status == ChecklistResult.PASS)
        soft_skips  = sum(1 for r in soft_results if r.status == ChecklistResult.SKIP)

        for r in hard_results:
            lvl = logging.WARNING if r.status == ChecklistResult.FAIL else logging.INFO
            logger.log(lvl, f"  [HARD] {r}")
        for r in soft_results:
            logger.info(f"  [SOFT] {r}")

        checklist_summary = {
            "hard_passed":       hard_passed,
            "hard_skipped":      hard_skips,
            "soft_passed":       soft_passes,
            "soft_skipped":      soft_skips,
            "failed_hard_check": next(
                (r.reason for r in hard_results if r.status == ChecklistResult.FAIL), None
            ),
        }

        if not hard_passed:
            failed = next(r for r in hard_results if r.status == ChecklistResult.FAIL)

            drift = self._drift_state
            self._drift_state = {}   # consume regardless of outcome
            if drift:
                flip_result = self._try_direction_flip(
                    payload,
                    drift["live_price"],
                    drift["drift_pct"],
                    signal_age_sec,
                )
                if flip_result is not None:
                    return flip_result

            reason = f"Hard check failed: {failed.reason}"
            logger.info(f"[BREAKOUT RECV] CHECKLIST REJECTED {sym} — {reason}")
            return {
                "accepted": False,
                "execution_eligible": True,
                "failure_class": "checklist_rejected",
                "reason": reason,
                "trade_id": None,
                "checklist": checklist_summary,
            }

        logger.info(
            f"[BREAKOUT RECV] CHECKLIST PASSED {sym} — "
            f"soft: {soft_passes} pass / {soft_skips} skip (advisory only)"
        )
        return self._inject_signal(payload, checklist_summary)

    # ──────────────────────────────────────────────────────────────────────
    # Checklist
    # ──────────────────────────────────────────────────────────────────────

    def _run_checklist(
        self, p: Dict, signal_age_sec: float
    ) -> Tuple[List[ChecklistResult], List[ChecklistResult]]:
        hard = [
            self._check_freshness(p, signal_age_sec),
            self._check_no_open_position(p),
            self._check_daily_symbol_limit(p),
            self._check_not_in_cooldown(p),
            self._check_trading_not_halted(),
            self._check_quiet_hours(p),
            self._check_concurrent_breakouts(p),
            self._check_min_price(p),
            self._check_price_still_moving(p),
            self._check_volume_elevated(p),
            self._check_no_wick_rejection(p),
            self._check_not_gap_trap(p),
            self._check_not_pump_and_dump(p),
            self._check_momentum_phase(p),
            self._check_structural_stop(p),
        ]
        soft = [
            self._check_rsi_confirms(p),
            self._check_trend_alignment(p),
            self._check_market_context(p),
        ]
        return hard, soft

    def _check_freshness(self, p: Dict, age_sec: float) -> ChecklistResult:
        if age_sec < 0:
            return ChecklistResult.fail("Signal timestamp missing or unparsable")
        if age_sec > SIGNAL_TTL_SEC:
            return ChecklistResult.fail(
                f"Signal stale — {age_sec:.0f}s old (TTL={SIGNAL_TTL_SEC}s)"
            )
        return ChecklistResult.ok(f"Signal fresh ({age_sec:.0f}s old)")

    def _check_no_open_position(self, p: Dict) -> ChecklistResult:
        sym = p["symbol"]
        if any(t["symbol"] == sym for t in (db.get_open_trades() or [])):
            return ChecklistResult.fail(f"Already have open position on {sym}")
        return ChecklistResult.ok(f"No existing position on {sym}")

    def _check_daily_symbol_limit(self, p: Dict) -> ChecklistResult:
        MAX_DAILY_PER_SYMBOL = 4  # Opus retune: was 2 — too restrictive on trending days
        sym = p["symbol"]
        try:
            today = date.today().isoformat()
            count = len([
                t for t in (db.get_trades_for_date(today) or [])
                if t.get("symbol") == sym
                and t.get("strategy_name") == "breakout_scanner"
            ])
            if count >= MAX_DAILY_PER_SYMBOL:
                return ChecklistResult.fail(
                    f"{sym} already traded {count}× today via breakout_scanner "
                    f"(daily limit={MAX_DAILY_PER_SYMBOL})"
                )
        except Exception as e:
            logger.debug(f"[DAILY LIMIT] Could not check daily trade count: {e}")
        return ChecklistResult.ok("Daily symbol limit OK")

    def _check_not_in_cooldown(self, p: Dict) -> ChecklistResult:
        sym  = p["symbol"]
        last = getattr(risk_manager, "_last_closed", {}).get(sym)
        if last:
            elapsed  = time.time() - last.get("time", 0)
            # FIXED (Opus audit 2026-05-29): was INVERTED — locked winning symbols
            # out for 30min, losers only 10min. A breakout winner is the fast-mover
            # we want to re-enter. Align to risk_manager policy (win=2min, loss=10min).
            cooldown = 120 if last.get("won") else 600   # win=2min, loss=10min
            if elapsed < cooldown:
                esc = int(p.get("escalation") or 0)
                if last.get("won") and p.get("bypass_win_cooldown"):
                    p["bypass_win_cooldown"] = True
                    return ChecklistResult.ok(
                        f"{sym} won {int(elapsed)}s ago — breakout re-entry bypass"
                    )
                return ChecklistResult.fail(
                    f"{sym} in cooldown — {int((cooldown-elapsed)/60)}min remaining"
                )
        return ChecklistResult.ok(f"{sym} not in cooldown")

    def _check_trading_not_halted(self) -> ChecklistResult:
        today   = datetime.now().date().isoformat()
        session = db.get_session(today) or {}
        if session.get("trading_halted"):
            return ChecklistResult.fail(
                f"Trading halted: {session.get('halt_reason', 'unknown')}"
            )
        consec = session.get("consecutive_losses", 0)
        if consec >= getattr(config, "MAX_CONSECUTIVE_LOSSES", 3):
            return ChecklistResult.fail(f"Consecutive loss limit hit ({consec})")
        return ChecklistResult.ok("Trading active")

    def _check_price_still_moving(self, p: Dict) -> ChecklistResult:
        sym       = p["symbol"]
        asset     = p.get("asset_class", "crypto")
        ref_price = float(p.get("current_price") or p.get("entry_price", 0))
        direction = p.get("direction", "long")

        if not self._scanner_ref:
            return ChecklistResult.fail(
                "Price re-verify impossible — scanner not wired to receiver"
            )
        if ref_price <= 0:
            return ChecklistResult.fail("current_price/entry_price is zero or missing")

        try:
            current = self._fetch_live_price_from_scanner(p)

            if not current:
                return ChecklistResult.skip(
                    f"Could not fetch live price for {sym} — skipping price drift check"
                )

            drift_pct = ((current - ref_price) / ref_price) * 100

            if direction == "long" and drift_pct < -PRICE_DRIFT_MAX_PCT:
                self._drift_state = {"live_price": current, "drift_pct": drift_pct}
                return ChecklistResult.fail(
                    f"{sym} reversed {drift_pct:.2f}% from signal price "
                    f"(limit -{PRICE_DRIFT_MAX_PCT}%)"
                )
            if direction == "short" and drift_pct > PRICE_DRIFT_MAX_PCT:
                self._drift_state = {"live_price": current, "drift_pct": drift_pct}
                return ChecklistResult.fail(
                    f"{sym} reversed +{drift_pct:.2f}% from signal price "
                    f"(limit +{PRICE_DRIFT_MAX_PCT}%)"
                )

            return ChecklistResult.ok(f"{sym} still moving (drift={drift_pct:+.2f}%)")
        except Exception as e:
            return ChecklistResult.fail(f"Price re-verify exception: {e}")

    def _check_quiet_hours(self, p: Dict) -> ChecklistResult:
        """Block crypto breakout entries during thin-volume hours (00:00–05:00 UTC).
        Stocks are unaffected — they only trade during exchange hours anyway.
        """
        asset = str(p.get("asset_class", "")).lower()
        if "crypto" not in asset:
            return ChecklistResult.ok("Stocks not subject to quiet hours")
        hour_utc = datetime.utcnow().hour
        if QUIET_HOURS_START_UTC <= hour_utc < QUIET_HOURS_END_UTC:
            return ChecklistResult.fail(
                f"Quiet hours ({QUIET_HOURS_START_UTC:02d}:00–{QUIET_HOURS_END_UTC:02d}:00 UTC) "
                f"— crypto volume too thin for reliable breakouts (now {hour_utc:02d}:xx UTC)"
            )
        return ChecklistResult.ok(f"Outside quiet hours ({hour_utc:02d}:xx UTC)")

    def _check_concurrent_breakouts(self, p: Dict) -> ChecklistResult:
        """Limit simultaneous open breakout_scanner positions to MAX_CONCURRENT_BREAKOUTS.

        Uses a class-level slot counter (not just DB count) to handle bursts where
        5 signals arrive simultaneously before any are committed to the DB.
        Slots are reserved here (by adding sym to _slots_reserved) and released in
        receive_signal's finally block.  Only signals that pass this check hold a slot.
        """
        sym = p.get("symbol", "?")
        with BreakoutReceiver._slot_lock:
            # Sync counter to reality: DB is the ground truth for already-open trades;
            # the in-memory counter adds slots that are being processed right now but
            # not yet in DB.  On restart the counter starts at 0 and syncs to DB.
            open_trades = db.get_open_trades() or []
            db_count = len([t for t in open_trades
                            if t.get("strategy_name") == "breakout_scanner"])
            # The counter may lag if a position closed since last update; clamp up to db_count.
            BreakoutReceiver._slots_used = max(BreakoutReceiver._slots_used, db_count)
            current = BreakoutReceiver._slots_used
            if current >= MAX_CONCURRENT_BREAKOUTS:
                return ChecklistResult.fail(
                    f"Already {current} breakout_scanner position(s) in-flight/open "
                    f"(max={MAX_CONCURRENT_BREAKOUTS}) — wait for one to close before adding more"
                )
            # Reserve a slot — this is the atomic claim that prevents burst races.
            BreakoutReceiver._slots_used += 1
            BreakoutReceiver._slots_reserved.add(sym)

        return ChecklistResult.ok(
            f"Concurrent breakouts: {current}/{MAX_CONCURRENT_BREAKOUTS} (slot reserved)"
        )

    def _check_min_price(self, p: Dict) -> ChecklistResult:
        asset = str(p.get("asset_class", "")).lower()
        price = float(p.get("current_price") or p.get("entry_price") or 0)
        if "crypto" in asset and price > 0 and price < MIN_PRICE_CRYPTO:
            return ChecklistResult.fail(
                f"Price ${price:.6f} below MIN_PRICE_CRYPTO ${MIN_PRICE_CRYPTO} — micro-cap rejected"
            )
        return ChecklistResult.ok(f"Price ${price:.6f} passes min price check")

    def _check_volume_elevated(self, p: Dict) -> ChecklistResult:
        vol = float(p.get("volume_spike", 0))
        if vol <= 0 or vol == 1.0:
            # FundingScanner: edge is funding rate extremes, not breakout momentum
            if p.get("signal_source") == "funding_scanner":
                return ChecklistResult.skip(
                    f"Volume spike is {vol:.2f}x (funding scanner — volume N/A, rate-based signal)"
                )
            # GapScanner gap_fill: deliberately fades low-volume moves — skip volume check
            if p.get("signal_source") == "gap_scanner" and p.get("gap_type") == "gap_fill":
                return ChecklistResult.skip(
                    f"Volume spike {vol:.2f}x (gap fill — fading low-volume reversal, volume check N/A)"
                )
            return ChecklistResult.fail(
                f"No volume baseline available ({vol:.2f}x) — cannot confirm breakout volume. "
                f"Rejecting to prevent thin-float / micro-cap entries with no liquidity confirmation."
            )
        if vol < VOLUME_SPIKE_MIN:
            # Gap fills trade on fading volume by design — don't require breakout-level vol
            if p.get("signal_source") == "gap_scanner" and p.get("gap_type") == "gap_fill":
                return ChecklistResult.skip(
                    f"Volume spike {vol:.2f}x (gap fill — low volume expected on reversal)"
                )
            esc = int(p.get("escalation") or 0)
            move = abs(float(p.get("move_pct") or 0))
            if _is_breakout_scanner_payload(p) and (esc >= 2 or move >= 5.0):
                return ChecklistResult.skip(
                    f"Volume spike {vol:.2f}x below {VOLUME_SPIKE_MIN}x, "
                    f"but breakout is already escalated/moving — advisory only"
                )
            return ChecklistResult.fail(
                f"Volume spike {vol:.2f}x below minimum {VOLUME_SPIKE_MIN}x"
            )
        return ChecklistResult.ok(f"Volume elevated {vol:.2f}x")

    def _check_no_wick_rejection(self, p: Dict) -> ChecklistResult:
        o, h, l, c = (
            p.get("candle_open"),
            p.get("candle_high"),
            p.get("candle_low"),
            p.get("candle_close"),
        )

        if not all(v is not None for v in [o, h, l, c]):
            return ChecklistResult.skip(
                "Wick check skipped — OHLC not in payload"
            )

        o, h, l, c = float(o), float(h), float(l), float(c)
        body = abs(c - o)
        if body == 0:
            return ChecklistResult.fail("Doji candle — no body (fakeout risk)")

        direction = p.get("direction", "long")
        if direction == "long":
            wick = h - max(o, c)
            if wick > body * WICK_BODY_MAX_RATIO:
                return ChecklistResult.fail(
                    f"Upper wick {wick:.4f} > {WICK_BODY_MAX_RATIO}x body {body:.4f}"
                )
        else:
            wick = min(o, c) - l
            if wick > body * WICK_BODY_MAX_RATIO:
                return ChecklistResult.fail(
                    f"Lower wick {wick:.4f} > {WICK_BODY_MAX_RATIO}x body {body:.4f}"
                )
        return ChecklistResult.ok("Candle body valid")

    def _check_not_gap_trap(self, p: Dict) -> ChecklistResult:
        move = abs(float(p.get("move_pct", 0)))
        if move > GAP_TRAP_MAX_PCT:
            return ChecklistResult.fail(
                f"Move {move:.1f}% > gap trap threshold {GAP_TRAP_MAX_PCT}%"
            )
        return ChecklistResult.ok(f"Move {move:.1f}% within range")

    def _check_not_pump_and_dump(self, p: Dict) -> ChecklistResult:
        """Reject signals that look like coordinated pump & dump activity.

        Two tells that organic breakouts on liquid tokens almost never show:
          1. Volume spike way above normal — legitimate breakouts on mid/large-cap
             crypto run 3–8x volume. 15x+ is consistent with wash-trading or a
             small group ramping a thin-float token.
          2. Crypto move already > 18% — by the time the scanner fires, the pump is
             likely at peak. Real breakout entries are 2–8% off base, not 18%+.
             (Stocks use the existing GAP_TRAP_MAX_PCT = 30% which is fine for
             pre-market gaps; this tighter crypto cap targets intraday pumps.)
        """
        asset  = str(p.get("asset_class", "")).lower()
        vol    = float(p.get("volume_spike", 0) or 0)
        move   = abs(float(p.get("move_pct", 0) or 0))

        if vol > 0 and vol != 1.0 and vol > PUMP_VOLUME_SPIKE_MAX:
            return ChecklistResult.fail(
                f"Volume spike {vol:.1f}x exceeds PUMP_VOLUME_SPIKE_MAX {PUMP_VOLUME_SPIKE_MAX}x "
                f"— possible wash-trade or thin-float pump"
            )

        if "crypto" in asset and move > PUMP_MOVE_MAX_CRYPTO:
            return ChecklistResult.fail(
                f"Crypto move {move:.1f}% exceeds PUMP_MOVE_MAX_CRYPTO {PUMP_MOVE_MAX_CRYPTO}% "
                f"— likely at pump peak, not breakout entry"
            )

        return ChecklistResult.ok(
            f"P&D check passed — vol={vol:.1f}x move={move:.1f}%"
        )

    def _check_momentum_phase(self, p: Dict) -> ChecklistResult:
        direction   = p.get("direction", "long")
        bars_since  = p.get("bars_since_breakout")
        dist_pct    = p.get("distance_from_breakout_pct")
        rsi         = p.get("rsi")

        MAX_BARS_SINCE    = 3    # reverted: 2 was too tight, scanner was silent — 3 bars = 15m on 5m
        MAX_DIST_FROM_LVL = 6.0  # reverted: 2.0% killed nearly all signals — 6.0% is the proven working cap
        RSI_EXHAUST_LONG  = 78
        RSI_EXHAUST_SHORT = 22

        if bars_since is not None:
            bars_since = int(bars_since)
            if bars_since > MAX_BARS_SINCE:
                return ChecklistResult.fail(
                    f"Move is {bars_since} bars old — too late to enter "
                    f"(max {MAX_BARS_SINCE} bars from breakout level)"
                )

        if dist_pct is not None:
            dist = abs(float(dist_pct))
            if dist > MAX_DIST_FROM_LVL:
                return ChecklistResult.fail(
                    f"Price already {dist:.1f}% from breakout level — chasing "
                    f"(max {MAX_DIST_FROM_LVL}% allowed)"
                )

        if rsi is not None:
            rsi_f = float(rsi)
            esc = int(p.get("escalation") or 0)
            move = abs(float(p.get("move_pct") or 0))
            breakout_runaway = _is_breakout_scanner_payload(p) and (esc >= 2 or move >= 5.0)
            if direction == "long" and rsi_f >= RSI_EXHAUST_LONG:
                if breakout_runaway:
                    return ChecklistResult.skip(
                        f"RSI {rsi_f:.1f} overbought, but escalated breakout can stay "
                        "parabolic — advisory only"
                    )
                return ChecklistResult.fail(
                    f"RSI {rsi_f:.1f} overbought — parabolic up likely exhausted "
                    f"(long rejected, threshold={RSI_EXHAUST_LONG})"
                )
            if direction == "short" and rsi_f <= RSI_EXHAUST_SHORT:
                if breakout_runaway:
                    return ChecklistResult.skip(
                        f"RSI {rsi_f:.1f} oversold, but escalated breakdown can continue "
                        "parabolic — advisory only"
                    )
                return ChecklistResult.fail(
                    f"RSI {rsi_f:.1f} oversold — parabolic down likely exhausted "
                    f"(short rejected, threshold={RSI_EXHAUST_SHORT})"
                )

        return ChecklistResult.ok(
            f"Momentum phase OK: bars_since={bars_since} dist={dist_pct}% rsi={rsi}"
        )

    def _check_structural_stop(self, p: Dict) -> ChecklistResult:
        stop      = p.get("structural_stop_price")
        price     = float(p.get("current_price") or p.get("entry_price", 0))
        direction = p.get("direction", "long")
        asset     = p.get("asset_class", "crypto")

        if stop is None:
            return ChecklistResult.ok("No structural stop — executor computes percent-based")

        stop = float(stop)
        if price <= 0:
            return ChecklistResult.ok("Stop sanity skipped — no price")

        if direction == "long" and stop >= price:
            if _is_breakout_scanner_payload(p):
                p["structural_stop_price"] = None
                return ChecklistResult.skip(
                    f"Structural stop {stop:.4f} >= price {price:.4f}; "
                    "falling back to breakout percent stop"
                )
            return ChecklistResult.fail(
                f"Stop {stop:.4f} >= price {price:.4f} — already broken for long"
            )
        if direction == "short" and stop <= price:
            if _is_breakout_scanner_payload(p):
                p["structural_stop_price"] = None
                return ChecklistResult.skip(
                    f"Structural stop {stop:.4f} <= price {price:.4f}; "
                    "falling back to breakout percent stop"
                )
            return ChecklistResult.fail(
                f"Stop {stop:.4f} <= price {price:.4f} — already broken for short"
            )

        max_dist = STOP_MAX_DISTANCE_CRYPTO if asset == "crypto" else STOP_MAX_DISTANCE_STOCK
        dist_pct = abs(price - stop) / price * 100
        if dist_pct > max_dist:
            if _is_breakout_scanner_payload(p):
                p["structural_stop_price"] = None
                return ChecklistResult.skip(
                    f"Structural stop too far ({dist_pct:.2f}% > max {max_dist}%); "
                    "falling back to breakout percent stop"
                )
            return ChecklistResult.fail(
                f"Stop too far: {dist_pct:.2f}% > max {max_dist}%"
            )
        return ChecklistResult.ok(f"Stop valid: {stop:.4f} ({dist_pct:.2f}% away)")

    def _check_rsi_confirms(self, p: Dict) -> ChecklistResult:
        rsi       = p.get("rsi")
        direction = p.get("direction", "long")
        if rsi is None:
            return ChecklistResult.skip("RSI not provided — neutral")
        rsi = float(rsi)
        if direction == "long":
            if rsi >= RSI_LONG_EXHAUST:
                return ChecklistResult.fail(f"RSI {rsi:.1f} overbought")
            if rsi < RSI_LONG_MIN:
                return ChecklistResult.fail(f"RSI {rsi:.1f} below {RSI_LONG_MIN}")
        else:
            if rsi <= RSI_SHORT_EXHAUST:
                return ChecklistResult.fail(f"RSI {rsi:.1f} oversold")
            if rsi > RSI_SHORT_MAX:
                return ChecklistResult.fail(f"RSI {rsi:.1f} above {RSI_SHORT_MAX}")
        return ChecklistResult.ok(f"RSI {rsi:.1f} confirms {direction}")

    def _check_trend_alignment(self, p: Dict) -> ChecklistResult:
        sma200    = p.get("sma200")
        price     = float(p.get("current_price") or p.get("entry_price", 0))
        direction = p.get("direction", "long")
        if sma200 is None or price <= 0:
            return ChecklistResult.skip("SMA200 not provided — neutral")
        sma200 = float(sma200)
        if direction == "long" and price < sma200:
            return ChecklistResult.fail(f"Price below SMA200 — counter-trend long")
        if direction == "short" and price > sma200:
            return ChecklistResult.fail(f"Price above SMA200 — counter-trend short")
        return ChecklistResult.ok("Trend aligned")

    def _check_market_context(self, p: Dict) -> ChecklistResult:
        mkt_chg   = p.get("market_pct_change")
        direction = p.get("direction", "long")
        if mkt_chg is None:
            return ChecklistResult.skip("Market context not provided — neutral")
        mkt_chg = float(mkt_chg)
        if direction == "long" and mkt_chg < MARKET_CRASH_THRESHOLD:
            return ChecklistResult.fail(f"Market down {mkt_chg:.1f}% — bad for longs")
        if direction == "short" and mkt_chg > MARKET_RALLY_THRESHOLD:
            return ChecklistResult.fail(f"Market up {mkt_chg:.1f}% — bad for shorts")
        return ChecklistResult.ok(f"Market context OK ({mkt_chg:+.1f}%)")

    # ──────────────────────────────────────────────────────────────────────
    # Direction flip
    # ──────────────────────────────────────────────────────────────────────

    def _try_direction_flip(
        self,
        payload: Dict[str, Any],
        live_price: float,
        drift_pct: float,
        signal_age_sec: float,
    ) -> Optional[Dict[str, Any]]:
        sym      = payload.get("symbol", "?")
        orig_dir = payload.get("direction", "long")
        flipped  = "short" if orig_dir == "long" else "long"

        # Evaluate 3 flip criteria (require 2/3)
        criteria_met = 0

        vol = float(payload.get("volume_spike", 0))
        c1  = vol >= VOLUME_SPIKE_MIN
        if c1:
            criteria_met += 1

        momentum = float(payload.get("momentum_score", 0))
        c2 = (momentum < 0) if orig_dir == "long" else (momentum > 0)
        if c2:
            criteria_met += 1

        candle_open = payload.get("candle_open")
        if candle_open is not None:
            co = float(candle_open)
            c3 = (live_price < co) if orig_dir == "long" else (live_price > co)
        else:
            c3 = False
        if c3:
            criteria_met += 1

        logger.info(
            f"[BREAKOUT RECV] FLIP EVAL {sym}: {orig_dir.upper()}->{flipped.upper()} "
            f"criteria={criteria_met}/3  "
            f"vol={vol:.2f}x>={VOLUME_SPIKE_MIN}:{c1}  "
            f"momentum={momentum:.3f} reversed:{c2}  "
            f"candle_open={f'{float(candle_open):.4f}' if candle_open is not None else 'N/A'}  "
            f"live={live_price:.4f} body_confirms:{c3}"
        )

        if criteria_met < 2:
            logger.info(
                f"[BREAKOUT RECV] FLIP DENIED {sym}: only {criteria_met}/3 criteria met — "
                f"rejecting as original {orig_dir.upper()}"
            )
            return None

        # Build the flipped payload
        p = dict(payload)
        p["direction"]     = flipped
        p["current_price"] = live_price
        p["entry_price"]   = live_price
        p["move_pct"]      = round(drift_pct, 4)

        dist = payload.get("distance_from_breakout_pct")
        if dist is not None:
            p["distance_from_breakout_pct"] = -float(dist)

        p["structural_stop_price"] = None  # old side's stop is now on the wrong side

        logger.info(
            f"[BREAKOUT RECV] DIRECTION FLIP {sym}: {orig_dir.upper()}->{flipped.upper()} "
            f"(drift={drift_pct:+.2f}%, criteria_met={criteria_met}/3) "
            f"entry={live_price:.4f} move_pct={p['move_pct']:+.4f}%"
        )

        hard_results, soft_results = self._run_checklist(p, signal_age_sec)

        hard_passed = all(
            r.status in (ChecklistResult.PASS, ChecklistResult.SKIP)
            for r in hard_results
        )
        hard_skips  = sum(1 for r in hard_results if r.status == ChecklistResult.SKIP)
        soft_passes = sum(1 for r in soft_results if r.status == ChecklistResult.PASS)
        soft_skips  = sum(1 for r in soft_results if r.status == ChecklistResult.SKIP)

        for r in hard_results:
            lvl = logging.WARNING if r.status == ChecklistResult.FAIL else logging.INFO
            logger.log(lvl, f"  [FLIP HARD] {r}")
        for r in soft_results:
            logger.info(f"  [FLIP SOFT] {r}")

        checklist_summary = {
            "hard_passed":       hard_passed,
            "hard_skipped":      hard_skips,
            "soft_passed":       soft_passes,
            "soft_skipped":      soft_skips,
            "failed_hard_check": next(
                (r.reason for r in hard_results if r.status == ChecklistResult.FAIL), None
            ),
            "direction_flip": f"{orig_dir}->{flipped}",
        }

        if not hard_passed:
            failed = next(r for r in hard_results if r.status == ChecklistResult.FAIL)
            logger.info(
                f"[BREAKOUT RECV] FLIP CHECKLIST REJECTED {sym} — {failed.reason}"
            )
            return {
                "accepted":           False,
                "execution_eligible": True,
                "failure_class":      "flip_checklist_rejected",
                "reason":             f"Direction flip failed checklist: {failed.reason}",
                "trade_id":           None,
                "checklist":          checklist_summary,
            }

        logger.info(
            f"[BREAKOUT RECV] FLIP CHECKLIST PASSED {sym} — "
            f"injecting {flipped.upper()} "
            f"soft: {soft_passes} pass / {soft_skips} skip (advisory only)"
        )
        return self._inject_signal(p, checklist_summary)

    # ──────────────────────────────────────────────────────────────────────
    # Injection
    # ──────────────────────────────────────────────────────────────────────

    def _inject_signal(self, p: Dict, checklist: dict) -> Dict:
        if not self._executor:
            logger.error("[BREAKOUT RECV] Executor not set")
            return {
                "accepted": False,
                "failure_class": "injection_exception",
                "reason": "Executor not configured",
                "trade_id": None,
                "checklist": checklist,
            }

        try:
            from scanners.market_scanner import Signal

            asset_class   = p.get("asset_class", "crypto")
            direction     = p.get("direction", "long")
            sym           = p["symbol"]
            price         = float(p.get("current_price") or p.get("entry_price"))
            esc           = p.get("escalation", 0)

            broker_hint   = self._normalize_broker_name(p.get("broker", "kraken"))
            source_broker = self._normalize_broker_name(
                p.get("source_broker", broker_hint)
            )

            if broker_hint not in EXECUTION_BROKERS:
                broker_hint = "kraken" if asset_class == "crypto" else "alpaca"

            move_pct_abs = abs(float(p.get("move_pct", 0)))
            has_structural_stop = p.get("structural_stop_price") is not None

            if asset_class == "crypto" and not has_structural_stop:
                custom_stop = round(min(max(move_pct_abs * 0.30, 2.0), 4.5), 2)
                logger.info(
                    f"[BREAKOUT RECV] {sym} custom_stop_loss_pct={custom_stop}% "
                    f"(move={move_pct_abs:.1f}%, no structural stop)"
                )
            else:
                custom_stop = None

            signal = Signal(
                symbol        = sym,
                asset_class   = asset_class,
                direction     = direction,
                score         = min(float(p.get("confidence", 0.7)) + 0.1, 1.0),
                current_price = price,
                reason        = (
                    f"breakout_scanner esc={esc} "
                    f"move={p.get('move_pct', 0):+.1f}% "
                    f"vol={p.get('volume_spike', 0):.1f}x"
                ),
                indicators    = {
                    "strategy_name":              "breakout_scanner",
                    "timeframe":                  "5m" if asset_class == "crypto" else "5Min",
                    "signal_source":              "breakout_scanner",
                    "preferred_broker":           broker_hint,
                    "source_broker":              source_broker,
                    "source_price":               p.get("source_price", p.get("current_price")),
                    "source_timestamp":           p.get("source_timestamp", p.get("timestamp")),
                    "sender_timestamp":           p.get("timestamp"),
                    "escalation":                 esc,
                    "move_pct":                   p.get("move_pct", 0),
                    "volume_spike":               p.get("volume_spike", 0),
                    "momentum_score":             p.get("momentum_score", 0),
                    "pattern":                    p.get("pattern", "breakout"),
                    "rsi":                        p.get("rsi"),
                    "sma200":                     p.get("sma200"),
                    "structural_stop_price":      p.get("structural_stop_price"),
                    "breakout_level":             p.get("breakout_level"),
                    "bars_since_breakout":        p.get("bars_since_breakout"),
                    "distance_from_breakout_pct": p.get("distance_from_breakout_pct"),
                    "custom_stop_loss_pct":       custom_stop,
                    "bypass_win_cooldown":        p.get("bypass_win_cooldown", False),
                },
            )

            logger.info(
                f"[BREAKOUT RECV] INJECTING {sym} {direction.upper()} "
                f"@ ${price:.4f} | esc={esc} move={p.get('move_pct',0):+.2f}% "
                f"preferred_broker={broker_hint} source_broker={source_broker}"
            )

            trade_id = self._executor.execute_signal(signal)

            if trade_id:
                logger.info(f"[BREAKOUT RECV] EXECUTED {sym} -> trade_id={trade_id}")
                return {
                    "accepted": True,
                    "execution_eligible": True,
                    "failure_class": None,
                    "reason": "Signal executed",
                    "trade_id": trade_id,
                    "checklist": checklist,
                }

            logger.warning(f"[BREAKOUT RECV] Executor rejected {sym}")
            return {
                "accepted": False,
                "execution_eligible": True,
                "failure_class": "checklist_rejected",
                "reason": "Executor rejected (risk manager or broker)",
                "trade_id": None,
                "checklist": checklist,
            }

        except Exception as e:
            logger.error(
                f"[BREAKOUT RECV] Injection exception for {p.get('symbol')}: {e}",
                exc_info=True,
            )
            return {
                "accepted": False,
                "execution_eligible": True,
                "failure_class": "injection_exception",
                "reason": f"Injection exception: {e}",
                "trade_id": None,
                "checklist": checklist,
            }

    # ──────────────────────────────────────────────────────────────────────
    # Scanner price fallback helpers
    # ──────────────────────────────────────────────────────────────────────

    def _normalize_broker_name(self, raw: str) -> str:
        name = str(raw or "").strip().lower()
        return BROKER_ALIASES.get(name, name)

    def _fetch_live_price_from_scanner(self, p: Dict) -> Optional[float]:
        """
        Re-verify live price using whatever scanner hooks are actually available.

        Order:
          1. scanner.get_current_price(...) if defined
          2. stock_fetcher.get_historical_data(...)[-1].price for stocks
          3. crypto_fetcher specific exchange(s).fetch_ticker(symbol) for crypto
        """
        scanner = self._scanner_ref
        if not scanner:
            return None

        sym          = p["symbol"]
        asset_class  = p.get("asset_class", "crypto")
        broker_hint  = self._normalize_broker_name(p.get("broker", ""))
        source_broker= self._normalize_broker_name(p.get("source_broker", ""))

        # 1) Direct scanner hook if present
        get_price = getattr(scanner, "get_current_price", None)
        if callable(get_price):
            try:
                px = get_price(sym, asset_class)
                if px:
                    return float(px)
            except Exception:
                pass

        # 2) Stock fallback
        if asset_class == "stock":
            try:
                sf = getattr(scanner, "stock_fetcher", None)
                if sf and hasattr(sf, "get_historical_data"):
                    bars = sf.get_historical_data(sym)
                    if bars:
                        last = getattr(bars[-1], "price", None)
                        if last:
                            return float(last)
            except Exception:
                pass
            return None

        # 3) Crypto fallback through scanner crypto fetcher / exchanges
        try:
            cf = getattr(scanner, "crypto_fetcher", None)
            if not cf:
                return None

            exchanges = getattr(cf, "exchanges", {}) or {}
            primary   = getattr(cf, "exchange", None)
            primary_name = self._normalize_broker_name(getattr(cf, "exchange_name", ""))

            candidate_names = []
            for name in (source_broker, broker_hint, primary_name):
                if name and name not in candidate_names:
                    candidate_names.append(name)

            # Named exchanges first
            for name in candidate_names:
                exc = exchanges.get(name.upper())
                if not exc and primary and name == primary_name:
                    exc = primary
                if not exc:
                    continue
                try:
                    symbols = getattr(exc, "symbols", []) or []
                    if symbols and sym not in symbols:
                        continue
                    ticker = exc.fetch_ticker(sym)
                    last = ticker.get("last") or ticker.get("close")
                    if last:
                        return float(last)
                except Exception:
                    continue

            # Primary exchange blind fallback
            if primary:
                try:
                    ticker = primary.fetch_ticker(sym)
                    last = ticker.get("last") or ticker.get("close")
                    if last:
                        return float(last)
                except Exception:
                    pass

        except Exception:
            pass

        return None

    # ──────────────────────────────────────────────────────────────────────
    # Timestamp helper
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _signal_age(timestamp_str: str) -> float:
        if not timestamp_str:
            return -1.0
        try:
            ts = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
            now = datetime.now(timezone.utc) if ts.tzinfo else datetime.now()
            return (now - ts).total_seconds()
        except Exception:
            return -1.0


# Module singleton
breakout_receiver = BreakoutReceiver()
