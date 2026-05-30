"""
ECB — Entropy Collapse Breakout
================================
Classification : CANDIDATE
Best markets   : DOGEUSDT (+5.16%), BNBUSDT (+2.76%), ETHUSDT (+2.24%),
                 BTCUSDT (+1.43%), XRPUSDT (+0.77%)  — 5/6 symbols positive
Avoid          : SOLUSDT (−0.44%, borderline)
Timeframe      : 1h  (primary)

Mathematical hypothesis
-----------------------
When the Shannon entropy of candle direction over a rolling window collapses
below a threshold, price is moving with abnormal directional consistency —
the market has stopped being random. This signals a regime of active
directional commitment. Entering in the direction of that commitment, aligned
with the macro trend (200-bar SMA), captures the continuation of that move
before the regime normalises.

Entropy = −p_up·log₂(p_up) − p_dn·log₂(p_dn)
  1.0 = fully random (50/50 up/down)
  0.0 = all bars same direction

Entry when entropy < 0.75 AND macro regime aligned.
Exit: 24-bar time hold. ATR-based SL/TP as catastrophic stops.

BTC 1h backtest results (2024-01-01 → 2025-05-14)
--------------------------------------------------
  Net profit     : +1.43 %
  Profit factor  : 2.59
  Win rate       : 63.6 %
  Avg win / loss : 1.48 R
  Max drawdown   : 0.66 %
  Sharpe ratio   : 0.87
  Trades         : 22

Cross-symbol 1h (2024) summary
--------------------------------
  DOGEUSDT  +5.16%  PF 2.86  Sharpe 1.17
  BNBUSDT   +2.76%  PF 2.26  Sharpe 0.96
  ETHUSDT   +2.24%  PF 2.40  Sharpe 0.75
  BTCUSDT   +1.43%  PF 2.59  Sharpe 0.87
  XRPUSDT   +0.77%  PF 1.26  Sharpe 0.20
  SOLUSDT   −0.44%  PF 0.95  Sharpe −0.10

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AUDIT STATUS (2026-05-20) — quant-strategy-auditor-refiner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STATUS: WATCHLIST — structural exit bug fixed, monitoring

LIVE PERFORMANCE (27 strategy exits, excl. manual_close):
  Long:  3 trades,  0.0% WR, -$25.94 (too few to judge)
  Short: 24 trades, 33.3% WR, -$142.51
  Net:   -$168.45 total

ROOT CAUSE — EXIT MECHANISM BUG:
  ECB's own exits (ecb_max_hold, ecb_entropy_reset, ecb_regime_flip)
  NEVER FIRED in live trading. All exits were position monitor mechanisms:
    pivot_break_r1:    13 exits  -$56.94  ← #1 cause
    perf_time_stop_1hr: 8 exits  -$39.79  ← ECB's 24-bar thesis murdered at 2hr
    stop_loss:          2 exits  -$93.12  (BCH alone = -$79.36)
    take_profit:        1 exit   +$26.75

  CRITICAL BUG: time_stop_profile = "strategy_defined" was declared but
  position_monitor never read it. The 2hr perf stop fired on ECB trades,
  cutting the 24-bar thesis at bar 2 every time.

APPLIED FIX (Iter1, 2026-05-20):
  ✅ position_monitor._check_performance_time_stop(): added ecb_strategy
     to TIME_STOP_EXEMPT set — 2hr/5hr/8hr stops now bypass ECB trades.
     ecb_max_hold (24-bar) will now be the primary time exit as designed.

NEXT STEPS:
  1. Monitor live: ECB exits should now show ecb_max_hold / ecb_entropy_reset
     / ecb_regime_flip. If perf_time_stop_1hr still fires → investigate further.
  2. After 50+ post-fix trades: check if WR improves toward backtest 63.6%
  3. BCH was the worst offender (-$79.36 stop_loss). If BCH repeatedly generates
     ECB shorts that blow up → add it to blacklist.
  4. Long side (3 trades, 0% WR) is too thin to judge. Monitor — if longs remain
     near 0% after 20+ trades, check the bull_regime filter for false positives.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Dependencies
------------
  pip install pandas numpy ccxt
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Dict, Optional


# ─── Parameter dataclass ──────────────────────────────────────────────────────

@dataclass
class ECBParams:
    # Entropy
    entropy_window:  int   = 20    # Rolling window for Shannon entropy
    entropy_thresh:  float = 0.75  # Enter when entropy collapses below this
    entropy_reset:   float = 0.90  # Entropy must rise above this before re-entry

    # Directional bias
    bias_threshold:  float = 0.55  # Rolling up-ratio must exceed this for long bias

    # ATR
    atr_length:      int   = 14
    sl_atr_mult:     float = 1.5   # Catastrophic stop = entry ± sl_atr_mult * ATR
    tp_atr_mult:     float = 2.5   # Profit target (rarely hit; main exit is time)
    vol_filter_len:  int   = 50
    vol_filter_pct:  float = 0.75  # ATR must be > vol_filter_pct * MA(ATR)

    # Macro trend regime
    trend_ma_length: int   = 200   # Long only above SMA, short only below

    # Trade duration
    max_bars_hold:   int   = 24    # Primary exit: time-based hold


# ─── Core entropy calculation ─────────────────────────────────────────────────

def rolling_entropy(df: pd.DataFrame, window: int) -> pd.Series:
    """
    Compute 2-class Shannon entropy of candle direction over a rolling window.
    Returns a Series in [0.0, 1.0]:  1.0 = random,  0.0 = fully directional.
    """
    is_up  = (df["close"] > df["open"]).astype(float)
    p_up   = is_up.rolling(window).mean().clip(1e-9, 1 - 1e-9)
    p_dn   = 1.0 - p_up
    entropy = -(p_up * np.log2(p_up) + p_dn * np.log2(p_dn))
    return entropy.rename("entropy")


# ─── Signal generation ────────────────────────────────────────────────────────

def generate_signals(df: pd.DataFrame, params: ECBParams = ECBParams()) -> pd.DataFrame:
    """
    Compute ECB signals on a standard OHLCV DataFrame.

    Parameters
    ----------
    df : DataFrame with columns [open, high, low, close, volume].
         Index should be a DatetimeIndex.
    params : ECBParams instance.

    Returns
    -------
    DataFrame with additional columns:
        entropy, bias_up, low_entropy, macro_ma, bull_regime, bear_regime,
        vol_ok, atr, long_signal, short_signal, sl_long, sl_short, tp_long, tp_short
    """
    p   = params
    out = df.copy()
    c   = out["close"]

    # ── Shannon entropy of candle direction ───────────────────────────────────
    out["entropy"]     = rolling_entropy(out, p.entropy_window)
    is_up              = (c > out["open"]).astype(float)

    # Rolling up-ratio (raw proportion, used for bias and metadata reporting)
    rolling_up_ratio        = is_up.rolling(p.entropy_window).mean()
    out["rolling_up_ratio"] = rolling_up_ratio

    # Directional bias using explicit threshold (not hardcoded 0.5)
    out["bias_up"]   = rolling_up_ratio >= p.bias_threshold
    out["bias_down"] = rolling_up_ratio <= (1.0 - p.bias_threshold)

    out["low_entropy"] = out["entropy"] < p.entropy_thresh

    # Entropy collapse event: was >= threshold, now drops below it.
    # This is an EVENT (crossover), not a persistent STATE — prevents
    # repeated entries inside the same low-entropy regime.
    entropy_prev            = out["entropy"].shift(1)
    out["entropy_collapse"] = (
        (entropy_prev >= p.entropy_thresh) & (out["entropy"] < p.entropy_thresh)
    )

    # ── ATR ───────────────────────────────────────────────────────────────────
    hl   = out["high"] - out["low"]
    hpc  = (out["high"] - c.shift()).abs()
    lpc  = (out["low"]  - c.shift()).abs()
    tr   = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    out["atr"]    = tr.ewm(span=p.atr_length, adjust=False).mean()
    atr_ma        = out["atr"].rolling(p.vol_filter_len).mean()
    out["vol_ok"] = out["atr"] > atr_ma * p.vol_filter_pct

    # ── Real volume participation (Patch 3) ───────────────────────────────────
    # Separate from vol_ok (ATR-based regime). This measures actual traded
    # volume vs rolling average — pure participation, no ATR coupling.
    vol_ma             = out["volume"].rolling(p.vol_filter_len).mean()
    out["volume_ratio"] = out["volume"] / vol_ma.replace(0, np.nan)

    # ── Macro trend regime ────────────────────────────────────────────────────
    out["macro_ma"]    = c.rolling(p.trend_ma_length).mean()
    out["bull_regime"] = c > out["macro_ma"]
    out["bear_regime"] = c < out["macro_ma"]

    # ── Entry signals ─────────────────────────────────────────────────────────
    # LONG: entropy COLLAPSED (event) with upward bias, price above 200 SMA, vol active
    out["long_signal"] = (
        out["entropy_collapse"] &
        out["bias_up"]          &
        out["bull_regime"]      &
        out["vol_ok"]
    )

    # SHORT: entropy COLLAPSED (event) with downward bias, price below 200 SMA, vol active
    out["short_signal"] = (
        out["entropy_collapse"] &
        out["bias_down"]        &
        out["bear_regime"]      &
        out["vol_ok"]
    )

    # ── Stop / target prices ──────────────────────────────────────────────────
    out["sl_long"]  = c - out["atr"] * p.sl_atr_mult
    out["sl_short"] = c + out["atr"] * p.sl_atr_mult
    out["tp_long"]  = c + out["atr"] * p.tp_atr_mult
    out["tp_short"] = c - out["atr"] * p.tp_atr_mult

    return out


# ─── Simple event-driven backtest ────────────────────────────────────────────

@dataclass
class Trade:
    direction:   str
    entry_bar:   int
    entry_price: float
    sl_price:    float
    tp_price:    float
    exit_bar:    Optional[int]   = None
    exit_price:  Optional[float] = None
    exit_reason: str             = ""
    pnl_pct:     float           = 0.0


def backtest(df: pd.DataFrame, params: ECBParams = ECBParams(),
             commission_pct: float = 0.05,
             position_size_pct: float = 10.0) -> tuple[list[Trade], pd.Series]:
    """
    RESEARCH-ONLY event-driven backtest. Returns (trades, equity_curve).

    WARNING: SL/TP evaluated at bar close — intrabar stop touches not simulated.
    This overstates profit on winning trades and understates stop-out frequency.
    Use intelligence/backtester.py (bot backtester) for realistic evaluation.

    position_size_pct : % of equity allocated per trade (default 10%).
    commission_pct    : round-trip commission as % of trade value.
    """
    sig    = generate_signals(df, params)
    size   = position_size_pct / 100.0
    equity = 1.0
    curve  = []
    trades: list[Trade] = []

    position: Optional[Trade] = None
    bars_held  = 0
    # Re-arm state: after an entry, wait for entropy to normalize above
    # entropy_reset before allowing another entry in the same direction.
    long_armed  = True
    short_armed = True

    for i, (idx, row) in enumerate(sig.iterrows()):
        c           = row["close"]
        entropy_now = float(row.get("entropy", 1.0))

        # Re-arm: once entropy has risen above reset threshold, allow new entries
        if entropy_now > params.entropy_reset:
            long_armed  = True
            short_armed = True

        if position is not None:
            bars_held += 1
            exit_triggered = False
            pnl = 0.0

            if position.direction == "long":
                if c <= position.sl_price:
                    pnl = (c - position.entry_price) / position.entry_price
                    position.exit_reason = "SL"
                    exit_triggered = True
                elif c >= position.tp_price:
                    pnl = (c - position.entry_price) / position.entry_price
                    position.exit_reason = "TP"
                    exit_triggered = True
                elif bars_held >= params.max_bars_hold:
                    pnl = (c - position.entry_price) / position.entry_price
                    position.exit_reason = "MaxDur"
                    exit_triggered = True
            else:
                if c >= position.sl_price:
                    pnl = (position.entry_price - c) / position.entry_price
                    position.exit_reason = "SL"
                    exit_triggered = True
                elif c <= position.tp_price:
                    pnl = (position.entry_price - c) / position.entry_price
                    position.exit_reason = "TP"
                    exit_triggered = True
                elif bars_held >= params.max_bars_hold:
                    pnl = (position.entry_price - c) / position.entry_price
                    position.exit_reason = "MaxDur"
                    exit_triggered = True

            if exit_triggered:
                net = pnl - commission_pct / 100 * 2
                position.exit_bar   = i
                position.exit_price = c
                position.pnl_pct    = net
                trades.append(position)
                equity *= (1 + net * size)
                position   = None
                bars_held  = 0

        if position is None:
            if row["long_signal"] and long_armed:
                position = Trade(
                    direction="long",
                    entry_bar=i,
                    entry_price=c,
                    sl_price=row["sl_long"],
                    tp_price=row["tp_long"],
                )
                bars_held   = 0
                long_armed  = False  # disarm — wait for entropy reset before re-entry
            elif row["short_signal"] and short_armed:
                position = Trade(
                    direction="short",
                    entry_bar=i,
                    entry_price=c,
                    sl_price=row["sl_short"],
                    tp_price=row["tp_short"],
                )
                bars_held   = 0
                short_armed = False  # disarm — wait for entropy reset before re-entry

        curve.append(equity)

    return trades, pd.Series(curve, index=sig.index, name="equity")


# ─── Stats ────────────────────────────────────────────────────────────────────

def print_stats(trades: list[Trade], label: str = "") -> None:
    if not trades:
        print("No trades.")
        return

    pnls   = [t.pnl_pct for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pf     = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr     = len(wins) / len(pnls) * 100
    aw     = np.mean(wins)   if wins   else 0
    al     = np.mean(losses) if losses else 0
    reasons = pd.Series([t.exit_reason for t in trades]).value_counts().to_dict()

    hdr = f"  [{label}]" if label else ""
    print(f"{'─'*45}{hdr}")
    print(f"  Trades        : {len(trades)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  Win rate      : {wr:.1f}%")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Avg win       : {aw*100:+.3f}%")
    print(f"  Avg loss      : {al*100:+.3f}%")
    print(f"  Win/Loss R    : {abs(aw/al):.2f}" if al else "  Win/Loss R    : ∞")
    print(f"  Net P&L       : {sum(pnls)*100:+.2f}%  (10% sizing, compounding)")
    print(f"  Exit reasons  : {reasons}")
    longs  = [t for t in trades if t.direction == "long"]
    shorts = [t for t in trades if t.direction == "short"]
    print(f"  Longs / Shorts: {len(longs)} / {len(shorts)}")
    print(f"{'─'*45}")


# ─── Live signal scanner ─────────────────────────────────────────────────────

def fetch_ohlcv_ccxt(symbol: str = "BTC/USDT", timeframe: str = "1h",
                     limit: int = 500, exchange_id: str = "bybit") -> pd.DataFrame:
    try:
        import ccxt
    except ImportError:
        raise ImportError("pip install ccxt")
    ex  = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp")


def scan_current_signal(symbol: str = "BTC/USDT", timeframe: str = "1h",
                        params: ECBParams = ECBParams()) -> dict:
    """Return current bar's signal state for live monitoring."""
    df   = fetch_ohlcv_ccxt(symbol, timeframe)
    sig  = generate_signals(df, params)
    last = sig.iloc[-1]
    return {
        "symbol":            symbol,
        "timeframe":         timeframe,
        "bar_time":          str(last.name),
        "close":             round(last["close"], 6),
        "entropy":           round(last["entropy"],  4),
        "entropy_collapse":  bool(last["entropy_collapse"]),
        "bias_up":           bool(last["bias_up"]),
        "bias_down":         bool(last["bias_down"]),
        "rolling_up_ratio":  round(float(last["rolling_up_ratio"]), 4),
        "low_entropy":       bool(last["low_entropy"]),
        "bull_regime":       bool(last["bull_regime"]),
        "bear_regime":       bool(last["bear_regime"]),
        "macro_ma":          round(float(last["macro_ma"]), 6) if not pd.isna(last["macro_ma"]) else None,
        "vol_ok":            bool(last["vol_ok"]),
        "atr":               round(float(last["atr"]), 6),
        "long_signal":       bool(last["long_signal"]),
        "short_signal":      bool(last["short_signal"]),
        "sl_long":           round(last["sl_long"],  6),
        "tp_long":           round(last["tp_long"],  6),
        "sl_short":          round(last["sl_short"], 6),
        "tp_short":          round(last["tp_short"], 6),
    }


def scan_watchlist(symbols: list[str], timeframe: str = "1h",
                   params: ECBParams = ECBParams()) -> list[dict]:
    """Scan multiple symbols and return only those with active signals."""
    active = []
    for sym in symbols:
        try:
            result = scan_current_signal(sym, timeframe, params)
            if result["long_signal"] or result["short_signal"]:
                active.append(result)
        except Exception as e:
            print(f"  {sym}: error — {e}")
    return active


# ─── Entry point ─────────────────────────────────────────────────────────────

WATCHLIST = [
    "BTC/USD",  "ETH/USD",  "SOL/USD",  "ADA/USD",
    "MATIC/USD","DOT/USD",  "AVAX/USD", "LINK/USD",
    "LTC/USD",  "XRP/USD",
]

if __name__ == "__main__":
    print("ECB Strategy — live signal scan")
    print(f"Scanning {len(WATCHLIST)} symbols on 1h…\n")
    try:
        signals = scan_watchlist(WATCHLIST, "1h")
        if signals:
            print(f"  {len(signals)} active signal(s):\n")
            for s in signals:
                direction = "LONG" if s["long_signal"] else "SHORT"
                print(f"  *** {direction}  {s['symbol']}  @{s['close']}"
                      f"  entropy={s['entropy']:.3f}  bar={s['bar_time']}")
        else:
            print("  No active signals on current bar.")
    except Exception as e:
        print(f"  (Live fetch unavailable: {e})")
        print("  Supply a DataFrame to generate_signals() or backtest() directly.")


# ─── BaseStrategy wrapper — Trading Bot v2 integration ────────────────────────

# Bot integration imports (available when running inside the bot)
try:
    from strategies.base_strategy import BaseStrategy, TradeSignal
except ImportError:
    BaseStrategy = object  # fallback for standalone use
    TradeSignal  = None


class ECBStrategy(BaseStrategy):
    """Entropy Collapse Breakout — low-entropy directional commitment signals (CANDIDATE)."""

    def __init__(self):
        super().__init__()
        self.strategy_name           = "ecb_strategy"    # snake_case, framework-safe
        self.params                  = ECBParams()
        self.stop_loss_pct           = 1.5      # fallback default (ATR-based preferred)
        self.take_profit_pct         = 2.5
        self.crypto_enabled          = True
        self.stock_enabled           = False
        self.crypto_candle_timeframe = "1h"
        self.reviewer_exempt         = True     # ATR-based dynamic stops
        self.ml_exempt               = True     # ML model dominated by grid_bot (1507 trades) — ECB has 1 historical trade, blending produces noise not signal

        # Strategy-defined hold: tell backtester to skip generic intraday time stops.
        # ECB's thesis uses a 24-bar max hold — the 8h hard stop and 30m early-loss
        # stop would prematurely kill trades and invalidate results.
        self.time_stop_profile       = "strategy_defined"

        # Warmup: need max(trend_ma, vol_filter, entropy_window) + 20 buffer
        p = self.params
        self.candle_limit = max(p.trend_ma_length, p.vol_filter_len, p.entropy_window) + 30

        # Re-arm state: per-symbol flag tracking whether entropy has normalized
        # since the last entry. Prevents repeated entries in the same low-entropy regime.
        self._entropy_rearm: Dict[str, bool] = {}

    def analyze(self, symbol: str, candles: pd.DataFrame,
                market_condition: str = "unknown") -> Optional[TradeSignal]:
        p = self.params
        MIN_BARS = max(p.trend_ma_length, p.vol_filter_len, p.entropy_window) + 20
        if not self._check_enough_candles(symbol, candles, MIN_BARS):
            return None

        # ── Timeframe guard: ECB requires 1h bars ────────────────────────────
        # On 5m/15m bars, Shannon entropy collapses every few hours of noise,
        # generating frequent low-quality entries (~8% WR observed in Apr 2026
        # backtest).  The 200-bar macro trend is also only ~16h of 5m data —
        # completely unstable as a regime filter.
        # Detect bar interval from last two candles and skip if < 50 minutes.
        if len(candles) >= 2:
            try:
                bar_secs = (candles.index[-1] - candles.index[-2]).total_seconds()
                if bar_secs < 3000:   # 3000s = 50 min; 1h bars ≥ 3600s
                    self.verbose_log_skip(
                        symbol,
                        f"ECB requires ≥1h bars; detected ~{int(bar_secs/60)}m bars — skip"
                    )
                    return None
            except Exception:
                pass  # non-datetime index — proceed without check

        try:
            sig  = generate_signals(candles, p)
            last = sig.iloc[-1]

            long_sig  = bool(last["long_signal"])
            short_sig = bool(last["short_signal"])

            if not (long_sig or short_sig):
                # Even when no signal, update re-arm state:
                # if entropy has normalized above reset, mark as re-armed
                entropy_now = float(last.get("entropy", 1.0))
                if entropy_now > p.entropy_reset:
                    self._entropy_rearm[symbol] = True
                return None

            direction = "long" if long_sig else "short"

            # Re-arm check: don't re-enter until entropy has risen above entropy_reset
            if not self._entropy_rearm.get(symbol, True):
                self.verbose_log_skip(
                    symbol,
                    f"ECB: waiting for entropy reset (must rise above {p.entropy_reset})"
                )
                return None

            close   = float(last["close"])
            atr     = float(last["atr"])
            entropy = float(last["entropy"])

            sl_pct = round(atr * p.sl_atr_mult / close * 100, 3)
            tp_pct = round(atr * p.tp_atr_mult / close * 100, 3)

            # Structural stop as absolute price (preferred by receiver and executor)
            structural_stop = (
                close - atr * p.sl_atr_mult if direction == "long"
                else close + atr * p.sl_atr_mult
            )

            # Score: lower entropy = stronger commitment; stronger bias = stronger signal
            # Patch 5: improved spread — old formula bunched at 0.50 floor because
            # entropy component was too small near the threshold.
            bias_ratio    = float(last.get("rolling_up_ratio", 0.5))
            bias_score    = abs(bias_ratio - 0.5) * 2          # 0.0 neutral → 1.0 pure directional
            entropy_depth = max(0.0, 1.0 - entropy / p.entropy_thresh)  # 0 at threshold → 1 at 0
            volume_ratio  = float(last.get("volume_ratio", 1.0))
            vol_part      = min(1.0, volume_ratio / 2.0)        # normalise: 2× avg volume = full bonus
            score = round(
                max(0.50, min(0.95,
                    0.55 +
                    0.30 * entropy_depth +                        # deeper collapse → higher score
                    0.20 * bias_score +                           # clearer direction → higher score
                    0.05 * float(last.get("vol_ok", False))       # ATR regime: small bonus only
                    # volume_ratio logged in metadata but not in score to keep it clean
                )),
                3
            )

            self.verbose_log(symbol, "entropy_collapse", True,
                             entropy, p.entropy_thresh, direction)
            self.verbose_log_score(symbol, score, 0.50)

            # Disarm: require entropy normalization before re-entry
            self._entropy_rearm[symbol] = False

            macro_ma_val = float(last["macro_ma"]) if not pd.isna(last["macro_ma"]) else None

            # Record the entry bar timestamp so check_custom_exit can derive
            # bars_held in the live path (Patch 1 — live/backtest parity).
            entry_bar_time = str(candles.index[-1]) if len(candles) > 0 else None

            return self._make_signal(
                symbol          = symbol,
                direction       = direction,
                score           = score,
                reason          = (
                    f"ECB: entropy_collapse={entropy:.3f} < {p.entropy_thresh} "
                    f"| bias_ratio={bias_ratio:.3f} "
                    f"| {'bull' if last['bull_regime'] else 'bear'} regime"
                ),
                stop_loss_pct   = sl_pct,
                take_profit_pct = tp_pct,
                metadata={
                    # Core ECB signal data — all real computed values, no placeholders
                    "strategy_name":          "ecb_strategy",
                    "entry_timeframe":        self.crypto_candle_timeframe,
                    # Patch 1: entry bar time for live max-hold parity
                    "_entry_bar_time":        entry_bar_time,
                    # Entropy
                    "entropy":                round(entropy, 4),
                    "entropy_thresh":         p.entropy_thresh,
                    "entropy_reset":          p.entropy_reset,
                    # Bias
                    "bias_up_ratio":          round(bias_ratio, 4),
                    "bias_up":                bool(last["bias_up"]),
                    "bias_down":              bool(last["bias_down"]),
                    # Patch 3/4: volatility (ATR-based) + real volume participation
                    "vol_ok":                 bool(last["vol_ok"]),           # ATR regime (kept for compat)
                    "volatility_ok":          bool(last["vol_ok"]),           # clearer alias (Patch 4)
                    "volume_ratio":           round(float(last.get("volume_ratio", float("nan"))), 3)
                                              if not pd.isna(last.get("volume_ratio", float("nan"))) else None,
                    # Regime
                    "bull_regime":            bool(last["bull_regime"]),
                    "bear_regime":            bool(last["bear_regime"]),
                    # ATR / stops
                    "atr":                    round(atr, 6),
                    "macro_ma":               round(macro_ma_val, 6) if macro_ma_val else None,
                    "structural_stop_price":  round(structural_stop, 6),
                },
            )
        except Exception as e:
            self.logger.error(f"ECBStrategy.analyze error on {symbol}: {e}", exc_info=True)
            return None

    def check_custom_exit(
        self,
        symbol: str,
        bars: pd.DataFrame,
        direction: str,
        entry_metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        ECB-specific exit logic.  Called by the backtester on every bar after entry.

        Priority:
          A. Regime flip  — price crosses macro MA against position
          B. Entropy normalization — entropy rises above entropy_reset
          C. Max bars hold — 24-bar time stop (strategy thesis)

        bars_held is injected into entry_metadata as "_bars_held" by the backtester.
        """
        if bars is None or len(bars) < 2:
            return None

        meta = entry_metadata or {}
        p    = self.params
        last = bars.iloc[-1]

        close = float(last["close"])

        # A. Regime flip: price crosses macro MA against position
        if len(bars) >= p.trend_ma_length:
            macro_ma = bars["close"].rolling(p.trend_ma_length).mean().iloc[-1]
            if not pd.isna(macro_ma):
                if direction == "long" and close < float(macro_ma):
                    return "ecb_regime_flip"
                if direction == "short" and close > float(macro_ma):
                    return "ecb_regime_flip"

        # B. Entropy normalization — Patch 2: require 2 consecutive bars above
        # entropy_reset before exiting. One-bar spike = noise. Two bars = regime ended.
        try:
            entropy_series = rolling_entropy(bars, p.entropy_window)
            if len(entropy_series) >= 2:
                e_now  = float(entropy_series.iloc[-1])
                e_prev = float(entropy_series.iloc[-2])
                if (not pd.isna(e_now) and not pd.isna(e_prev)
                        and e_now > p.entropy_reset and e_prev > p.entropy_reset):
                    return "ecb_entropy_reset"
        except Exception:
            pass

        # C. Max bars hold — Patch 1: live/backtest parity.
        # Primary path: backtester injects _bars_held directly.
        # Fallback path: derive from _entry_bar_time stored in metadata at entry.
        #   The live position monitor passes entry_metadata through, so _entry_bar_time
        #   written by analyze() is available here in the live path.
        bars_held = int(meta.get("_bars_held", 0))
        if bars_held == 0 and "_entry_bar_time" in meta:
            try:
                entry_ts  = pd.Timestamp(meta["_entry_bar_time"])
                # Count bars whose index is strictly after the entry bar
                bars_held = int((bars.index > entry_ts).sum())
            except Exception:
                pass
        if bars_held >= p.max_bars_hold:
            return "ecb_max_hold"

        return None
