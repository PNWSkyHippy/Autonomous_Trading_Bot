"""
scalp_master.py — Strategy 5: Scalp Master
Trading Bot v2

High-frequency scalping with tight stops and quick profit targets.
Uses a short RSI period (7) and requires volume confirmation to avoid noise.
Position size is slightly smaller (1.5% vs default 2%) due to higher frequency.

Thresholds configurable in config.SIGNAL_TUNING.

Symbol whitelist: backtest data (30d 5m, Apr 2026) confirmed positive Sharpe
only on META, PLTR, ALAB, AMZN, GOOGL, MSFT. IREN and INTV added after
Anthropic backtest review Apr 2026.

Per-symbol adaptive SL/TP: After every 20 closed trades per symbol,
SL and TP are recalculated from actual trade history and saved to
symbol_params.json. Minimum R:R of 2.5 is always enforced.

Signal filters (Apr 2026 update):
  - VWAP filter: only long above VWAP, only short below VWAP
  - ADX filter: skip when ADX < 20 (choppy market, scalping unreliable)
  - EMA 9/21 filter: fast EMA must agree with signal direction
"""

import json
import logging
import os
import threading
from typing import Dict, Optional

import pandas as pd
import pandas_ta as ta
import numpy as np

import config
from strategies.base_strategy import BaseStrategy, TradeSignal


# ── Symbol whitelist ────────────────────────────────────────────────────────
SCALP_WHITELIST = {"META", "PLTR", "ALAB", "AMZN", "GOOGL", "MSFT", "IREN", "INTV"}

# ── Adaptive params file path ───────────────────────────────────────────────
PARAMS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "symbol_params.json"
)

# ── Per-symbol starting defaults (seed values before any trade history) ─────
SYMBOL_DEFAULTS: Dict[str, Dict] = {
    "META":  {"sl": 0.60, "tp": 1.75},
    "PLTR":  {"sl": 0.50, "tp": 1.25},
    "MSFT":  {"sl": 0.50, "tp": 1.25},
    "GOOGL": {"sl": 0.40, "tp": 1.50},
    "AMZN":  {"sl": 0.50, "tp": 1.25},
    "ALAB":  {"sl": 0.40, "tp": 1.00, "min_confidence": 0.58},
    "IREN":  {"sl": 0.35, "tp": 1.00, "min_confidence": 0.60},
    # INTV: added to whitelist after Apr 2026 review; no individual backtest yet.
    # Uses GLOBAL_FALLBACK (0.50/1.25) until enough live trades accumulate.
    # Intentionally omitted from explicit seed — monitor closely.
}

# ── Global fallback for symbols not in SYMBOL_DEFAULTS ─────────────────────
GLOBAL_FALLBACK = {"sl": 0.50, "tp": 1.25}

# ── Constants ───────────────────────────────────────────────────────────────
MIN_RR       = 2.5   # Minimum reward:risk ratio enforced at all times
RECALC_EVERY = 20    # Recalculate after this many closed trades per symbol

# ── File write lock ─────────────────────────────────────────────────────────
_params_lock = threading.Lock()

# ── In-memory cache (avoids 600+ disk reads/day on 5m 8-symbol scans) ──────
import time as _time
_params_cache: Dict = {}
_params_cache_time: float = 0.0
_CACHE_TTL_SEC: float = 30.0


# ===========================================================================
#  Adaptive params helpers
# ===========================================================================

def _load_params_file() -> Dict:
    """
    Load params from disk with in-memory cache (30-second TTL).
    Thread-safe: entire load is inside _params_lock to prevent partial reads
    during concurrent saves.
    """
    global _params_cache, _params_cache_time
    now = _time.time()
    # Return cached copy if still fresh — avoids disk read on every analyze()
    with _params_lock:
        if _params_cache and (now - _params_cache_time) < _CACHE_TTL_SEC:
            return dict(_params_cache)
        try:
            if os.path.exists(PARAMS_FILE):
                with open(PARAMS_FILE, "r") as f:
                    data = json.load(f)
                _params_cache      = data
                _params_cache_time = now
                return dict(data)
        except Exception as e:
            logging.getLogger("ScalpMaster").warning(
                f"Could not load {PARAMS_FILE}: {e} — using defaults"
            )
    return {}


def _save_params_file(data: Dict) -> None:
    global _params_cache, _params_cache_time
    with _params_lock:
        try:
            tmp = PARAMS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, PARAMS_FILE)
            # Update cache immediately so the next read sees the fresh data
            _params_cache      = data
            _params_cache_time = _time.time()
        except Exception as e:
            logging.getLogger("ScalpMaster").error(
                f"Failed to save {PARAMS_FILE}: {e}"
            )


def get_symbol_params(symbol: str) -> Dict:
    """
    Return current SL/TP params for a symbol.
    Priority: learned params on disk → per-symbol seed → global fallback.
    """
    sym = symbol.upper()
    stored = _load_params_file()
    if sym in stored and "sl" in stored[sym] and "tp" in stored[sym]:
        return stored[sym]
    if sym in SYMBOL_DEFAULTS:
        return dict(SYMBOL_DEFAULTS[sym])
    return dict(GLOBAL_FALLBACK)


def log_trade_result(symbol: str, pnl_pct: float, won: bool) -> None:
    """
    Record a closed scalp trade result and trigger adaptive recalculation
    every RECALC_EVERY trades. Called from trade_executor.close_trade().
    """
    sym = symbol.upper()
    stored = _load_params_file()

    if sym not in stored:
        stored[sym] = {}
    if "history" not in stored[sym]:
        stored[sym]["history"] = []

    stored[sym]["history"].append({"pnl_pct": round(pnl_pct, 4), "won": won})
    history = stored[sym]["history"]

    if len(history) % RECALC_EVERY == 0:
        stored[sym] = _recalculate_params(sym, history, stored[sym])
        logging.getLogger("ScalpMaster").info(
            f"[Adaptive] {sym}: recalculated after {len(history)} trades — "
            f"SL={stored[sym]['sl']:.3f}%  TP={stored[sym]['tp']:.3f}%"
        )

    _save_params_file(stored)


def _recalculate_params(symbol: str, history: list, current: Dict) -> Dict:
    """Recalculate SL/TP from actual closed trade history."""
    logger = logging.getLogger("ScalpMaster")
    wins   = [abs(t["pnl_pct"]) for t in history if     t["won"]]
    losses = [abs(t["pnl_pct"]) for t in history if not t["won"]]

    if len(losses) < 5:
        logger.info(f"[Adaptive] {symbol}: {len(losses)} losses < 5 — keeping SL")
        new_sl = current.get("sl", GLOBAL_FALLBACK["sl"])
    else:
        avg_loss = sum(losses) / len(losses)
        new_sl = current.get("sl", GLOBAL_FALLBACK["sl"]) if avg_loss == 0 else avg_loss * 0.85

    if len(wins) < 5:
        logger.info(f"[Adaptive] {symbol}: {len(wins)} wins < 5 — keeping TP")
        new_tp = current.get("tp", GLOBAL_FALLBACK["tp"])
    else:
        new_tp = (sum(wins) / len(wins)) * 0.90

    new_tp = max(new_tp, new_sl * MIN_RR)
    new_sl = max(0.10, min(new_sl, 3.0))
    new_tp = max(0.25, min(new_tp, 10.0))

    updated = dict(current)
    updated["sl"] = round(new_sl, 4)
    updated["tp"] = round(new_tp, 4)
    return updated


# ===========================================================================
#  Indicator helpers
# ===========================================================================

def _calc_vwap(candles: pd.DataFrame) -> Optional[float]:
    """
    Calculate intraday VWAP from available bars.
    VWAP = cumulative(typical_price * volume) / cumulative(volume)
    Typical price = (high + low + close) / 3
    Returns the current bar's VWAP value, or None on failure.
    """
    try:
        typical = (candles["high"] + candles["low"] + candles["close"]) / 3
        cum_vol = candles["volume"].cumsum()
        cum_tpv = (typical * candles["volume"]).cumsum()
        vwap = cum_tpv / cum_vol.replace(0, float("nan"))
        val = vwap.iloc[-1]
        return float(val) if not pd.isna(val) else None
    except Exception:
        return None


def _calc_adx(candles: pd.DataFrame, period: int = 14) -> Optional[float]:
    """
    Calculate ADX using pandas_ta. Returns current ADX value or None.
    ADX < 20 = choppy/ranging. ADX > 25 = trending.
    """
    try:
        adx_df = ta.adx(candles["high"], candles["low"], candles["close"], length=period)
        if adx_df is None or adx_df.empty:
            return None
        col = [c for c in adx_df.columns if c.startswith("ADX_")]
        if not col:
            return None
        val = adx_df[col[0]].iloc[-1]
        return float(val) if not pd.isna(val) else None
    except Exception:
        return None


# ===========================================================================
#  Strategy class
# ===========================================================================

class ScalpMaster(BaseStrategy):

    def __init__(self):
        super().__init__()
        self.strategy_name   = "scalp_master"
        self.stop_loss_pct   = 0.5    # framework default; actual stops are per-symbol adaptive
        self.take_profit_pct = 1.0    # framework default; actual TPs are per-symbol adaptive
        # NOTE: self.logger is already set by BaseStrategy.__init__ to "ScalpMaster"
        # (getLogger(self.__class__.__name__)). Do not override — it would shadow
        # framework-level logging hooks that rely on the base class field.

        self.stock_enabled          = True
        self.crypto_enabled         = False   # scalp whitelist is stocks only
        self.stock_candle_timeframe = "5Min"  # Alpaca 5-minute bars

        # ML model is dominated by grid_bot (1507 trades); scalp_master has
        # minimal training representation so ML blending collapses valid scores.
        self.ml_exempt           = True
        self.reviewer_exempt     = True   # reviewer lacks 5m scalp metadata context; exempt
        self.auto_disable_exempt = True   # whitelist-only; losing on one symbol ≠ strategy broken
        self.time_stop_profile   = "strategy_defined"  # SL/TP exits; generic 30m/2h stops interfere

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_condition: str = "unknown"
    ) -> Optional[TradeSignal]:

        # ── Whitelist check ──────────────────────────────────────────────
        if not self._passes_symbol_whitelist(
            symbol, SCALP_WHITELIST, "Scalp whitelist"
        ):
            self.verbose_log_skip(
                symbol,
                f"Not in scalp whitelist — skipping "
                f"(allowed: {', '.join(sorted(SCALP_WHITELIST))})"
            )
            return None

        # ── Load per-symbol adaptive params ─────────────────────────────
        sym_params   = get_symbol_params(symbol)
        sym_sl       = sym_params.get("sl",             self.stop_loss_pct)
        sym_tp       = sym_params.get("tp",             self.take_profit_pct)
        global_min   = config.SIGNAL_TUNING.get("scalp_min_score", 0.60)
        sym_min_conf = sym_params.get("min_confidence", global_min)

        tuning           = config.SIGNAL_TUNING
        rsi_oversold     = tuning["scalp_rsi_oversold"]
        rsi_overbought   = tuning["scalp_rsi_overbought"]
        rsi_period       = tuning["scalp_rsi_period"]
        min_volume_ratio = tuning["scalp_min_volume_ratio"]
        adx_min          = tuning.get("scalp_adx_min", 20)

        required = max(rsi_period + 10, 30)
        if not self._check_enough_candles(symbol, candles, required):
            return None

        if market_condition == "ranging":
            self.verbose_log_skip(symbol, "Ranging market — scalp signals unreliable")
            return None

        close  = candles["close"]
        volume = candles["volume"]

        # ── ADX filter — skip in choppy/non-trending conditions ──────────
        # Scalping RSI reversals in a directionless market produces whipsaws.
        # ADX < adx_min means there is no meaningful trend to ride.
        adx_val = _calc_adx(candles)
        if adx_val is not None:
            adx_ok = adx_val >= adx_min
            self.verbose_log(
                symbol, f"ADX trend strength (need >={adx_min} to scalp)",
                adx_ok, round(adx_val, 1), f">={adx_min}",
                extra=f"adx={adx_val:.1f}"
            )
            if not adx_ok:
                self.logger.info(
                    f"[ScalpMaster] {symbol}: SKIP — ADX={adx_val:.1f} < {adx_min} "
                    f"(market too choppy to scalp)"
                )
                return None
        else:
            self.verbose_log_skip(symbol, "ADX unavailable — proceeding without filter")

        # ── VWAP calculation ─────────────────────────────────────────────
        # Used directionally: only long above VWAP, only short below VWAP.
        # Entering a long scalp while price is below VWAP means fighting
        # the intraday sell pressure — historically high-loss scenario.
        vwap_val = _calc_vwap(candles)
        current_close = float(close.iloc[-1])

        if vwap_val is not None:
            price_above_vwap = current_close > vwap_val
            self.logger.info(
                f"[ScalpMaster] {symbol}: VWAP={vwap_val:.4f} "
                f"price={current_close:.4f} above={price_above_vwap}"
            )
        else:
            price_above_vwap = None
            self.verbose_log_skip(symbol, "VWAP unavailable — proceeding without VWAP filter")

        # ── EMA 9/21 trend filter ────────────────────────────────────────
        # Fast EMA must be above slow EMA for longs (uptrend), below for shorts.
        # Second trend confirmation independent of VWAP.
        try:
            ema9  = ta.ema(close, length=9)
            ema21 = ta.ema(close, length=21)
            ema9_val  = float(ema9.iloc[-1])  if ema9  is not None else None
            ema21_val = float(ema21.iloc[-1]) if ema21 is not None else None
            ema_bullish = (
                (ema9_val > ema21_val)
                if (ema9_val is not None and ema21_val is not None)
                else None
            )
            if ema9_val is not None and ema21_val is not None:
                self.logger.info(
                    f"[ScalpMaster] {symbol}: EMA9={ema9_val:.4f} "
                    f"EMA21={ema21_val:.4f} bullish={ema_bullish}"
                )
        except Exception:
            ema_bullish = None

        # ── RSI calculation ──────────────────────────────────────────────
        try:
            rsi_series = ta.rsi(close, length=rsi_period)
            if rsi_series is None or rsi_series.isna().all():
                self.verbose_log_skip(symbol, "RSI returned no data")
                return None
        except Exception as e:
            self.verbose_log_skip(symbol, f"RSI error: {e}")
            return None

        current_rsi  = rsi_series.iloc[-1]
        previous_rsi = rsi_series.iloc[-2]

        if pd.isna(current_rsi) or pd.isna(previous_rsi):
            self.verbose_log_skip(symbol, "RSI is NaN")
            return None

        # ── Volume confirmation ──────────────────────────────────────────
        avg_volume     = volume.iloc[-20:].mean()
        current_volume = volume.iloc[-1]
        volume_ratio   = current_volume / avg_volume if avg_volume > 0 else 0

        volume_ok = volume_ratio >= min_volume_ratio
        self.verbose_log(
            symbol, "Volume spike confirmation",
            volume_ok, volume_ratio, f">={min_volume_ratio}",
            extra=f"current_vol={current_volume:.0f} avg_vol={avg_volume:.0f}"
        )
        if not volume_ok:
            return None

        # ── LONG SIGNAL ──────────────────────────────────────────────────
        was_oversold   = previous_rsi < rsi_oversold
        now_recovering = current_rsi  >= rsi_oversold

        self.verbose_log(
            symbol, "Scalp: RSI was oversold (long setup)",
            was_oversold, previous_rsi, f"<{rsi_oversold}", "long"
        )

        if was_oversold and now_recovering:
            self.verbose_log(
                symbol, "Scalp: RSI recovering from oversold (long entry)",
                now_recovering, current_rsi, f">={rsi_oversold}", "long"
            )

            # VWAP check — reject long if price is below VWAP
            if price_above_vwap is not None and not price_above_vwap:
                self.logger.info(
                    f"[ScalpMaster] {symbol}: LONG rejected — "
                    f"price ${current_close:.4f} below VWAP ${vwap_val:.4f} "
                    f"(counter-trend scalp)"
                )
                return None

            # EMA check — reject long if fast EMA is below slow EMA
            if ema_bullish is not None and not ema_bullish:
                self.logger.info(
                    f"[ScalpMaster] {symbol}: LONG rejected — "
                    f"EMA9 below EMA21 (downtrend)"
                )
                return None

            score = min(1.0, sym_min_conf + volume_ratio * 0.05)
            self.verbose_log_score(symbol, score, sym_min_conf)
            if score >= sym_min_conf:
                adx_str  = f"{adx_val:.1f}" if adx_val is not None else "N/A"
                vwap_str = "above" if price_above_vwap else "N/A"
                return self._make_signal(
                    symbol          = symbol,
                    direction       = "long",
                    score           = score,
                    stop_loss_pct   = sym_sl,
                    take_profit_pct = sym_tp,
                    reason          = (
                        f"Scalp long: RSI {previous_rsi:.1f}→{current_rsi:.1f} "
                        f"vol={volume_ratio:.2f}x "
                        f"ADX={adx_str} "
                        f"VWAP={vwap_str} "
                        f"SL={sym_sl}% TP={sym_tp}%"
                    ),
                    metadata        = {
                        "strategy_name":    "scalp_master",
                        "entry_timeframe":  "5m",
                        "rsi":              round(float(current_rsi),  2),
                        "rsi_prev":         round(float(previous_rsi), 2),
                        "volume_ratio":     round(float(volume_ratio), 3),
                        "adx":              round(adx_val,    2) if adx_val    is not None else None,
                        "vwap":             round(vwap_val,   4) if vwap_val   is not None else None,
                        "price_above_vwap": price_above_vwap,
                        "ema9":             round(ema9_val,   4) if ema9_val   is not None else None,
                        "ema21":            round(ema21_val,  4) if ema21_val  is not None else None,
                        "ema_bullish":      ema_bullish,
                        "sym_sl":           sym_sl,
                        "sym_tp":           sym_tp,
                        "adaptive_params":  "learned" if sym_params.get("learned") else "seed",
                    },
                )

        # ── SHORT SIGNAL ─────────────────────────────────────────────────
        was_overbought = previous_rsi > rsi_overbought
        now_retreating = current_rsi  <= rsi_overbought

        self.verbose_log(
            symbol, "Scalp: RSI was overbought (short setup)",
            was_overbought, previous_rsi, f">{rsi_overbought}", "short"
        )

        if was_overbought and now_retreating:
            self.verbose_log(
                symbol, "Scalp: RSI retreating from overbought (short entry)",
                now_retreating, current_rsi, f"<={rsi_overbought}", "short"
            )

            # VWAP check — reject short if price is above VWAP
            if price_above_vwap is not None and price_above_vwap:
                self.logger.info(
                    f"[ScalpMaster] {symbol}: SHORT rejected — "
                    f"price ${current_close:.4f} above VWAP ${vwap_val:.4f} "
                    f"(counter-trend scalp)"
                )
                return None

            # EMA check — reject short if fast EMA is above slow EMA
            if ema_bullish is not None and ema_bullish:
                self.logger.info(
                    f"[ScalpMaster] {symbol}: SHORT rejected — "
                    f"EMA9 above EMA21 (uptrend)"
                )
                return None

            score = min(1.0, sym_min_conf + volume_ratio * 0.05)
            self.verbose_log_score(symbol, score, sym_min_conf)
            if score >= sym_min_conf:
                adx_str  = f"{adx_val:.1f}" if adx_val is not None else "N/A"
                vwap_str = "below" if price_above_vwap is False else "N/A"
                return self._make_signal(
                    symbol          = symbol,
                    direction       = "short",
                    score           = score,
                    stop_loss_pct   = sym_sl,
                    take_profit_pct = sym_tp,
                    reason          = (
                        f"Scalp short: RSI {previous_rsi:.1f}→{current_rsi:.1f} "
                        f"vol={volume_ratio:.2f}x "
                        f"ADX={adx_str} "
                        f"VWAP={vwap_str} "
                        f"SL={sym_sl}% TP={sym_tp}%"
                    ),
                    metadata        = {
                        "strategy_name":    "scalp_master",
                        "entry_timeframe":  "5m",
                        "rsi":              round(float(current_rsi),  2),
                        "rsi_prev":         round(float(previous_rsi), 2),
                        "volume_ratio":     round(float(volume_ratio), 3),
                        "adx":              round(adx_val,    2) if adx_val    is not None else None,
                        "vwap":             round(vwap_val,   4) if vwap_val   is not None else None,
                        "price_above_vwap": price_above_vwap,
                        "ema9":             round(ema9_val,   4) if ema9_val   is not None else None,
                        "ema21":            round(ema21_val,  4) if ema21_val  is not None else None,
                        "ema_bullish":      ema_bullish,
                        "sym_sl":           sym_sl,
                        "sym_tp":           sym_tp,
                        "adaptive_params":  "learned" if sym_params.get("learned") else "seed",
                    },
                )

        self.verbose_log(
            symbol, "Scalp: RSI in neutral zone",
            False, current_rsi, f"need <{rsi_oversold} or >{rsi_overbought}"
        )
        return None
