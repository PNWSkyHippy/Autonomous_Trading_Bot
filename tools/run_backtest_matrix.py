"""
Run a bounded backtest parameter matrix and write sortable CSV/JSONL summaries.

Examples:
  python tools/run_backtest_matrix.py --asset crypto --quick
  python tools/run_backtest_matrix.py --asset crypto --strategy grid_bot --max-symbols 12 --max-combos 30
  python tools/run_backtest_matrix.py --asset crypto --full --max-symbols 20 --max-combos 200

The defaults are intentionally capped so this can run on the desktop without
starving the dashboard. Increase --max-symbols and --max-combos when running
overnight or on a VPS.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from intelligence.backtester import Backtester, clear_cancel


DEFAULT_STRATEGIES = [
    # Original core (1-11)
    "grid_bot", "dca_accumulator", "rsi_momentum", "bollinger_breakout",
    "ema_crossover", "mean_reversion", "scalp_master", "swing_trader",
    "vwap_momentum", "hammer_reversal", "orb_breakout",
    # Mid-gen (12-21)
    "ecb_strategy", "vdmr_strategy", "rsi_dip_spike_v4",
    "vwap_confirmed_orb", "bollinger_squeeze", "mr_02_vef", "mr_03_fbs",
    "mr_04_fvg", "adaptive_regime",
    # Phase 2 new / overhauled (22-30)
    "btc_v6_chandelier", "rsi_dip_simple", "pll_cycle",
    "kds_mean_reversion", "ema_ribbon_breakout", "rcr_mean_reversion",
    "cbae_strategy", "rare_strategy", "fels_strategy", "map_strategy",
    # pll_cycle_martingale excluded — permanently disabled in DB
]

QUICK_TIMEFRAMES = ["5m", "15m", "1h"]
FULL_TIMEFRAMES = ["5m", "15m", "1h", "1d"]

QUICK_SIDES = ["long", "all"]
FULL_SIDES = ["long", "short", "all"]

# label, legacy stop_mode, initial_stop_mode, trail_mode, lookback
QUICK_STOP_PROFILES = [
    ("auto", "standard", "auto", "auto", 2),
    ("struct_auto", "standard", "signal_structural", "auto", 2),
    ("two_bar_2", "two_bar", "two_bar", "two_bar", 2),
]

FULL_STOP_PROFILES = [
    ("auto", "standard", "auto", "auto", 2),
    ("fixed_percent", "standard", "percent", "percent", 2),
    ("struct_auto", "standard", "signal_structural", "auto", 2),
    ("struct_no_trail", "standard", "signal_structural", "none", 2),
    ("two_bar_2", "two_bar", "two_bar", "two_bar", 2),
    ("two_bar_3", "two_bar", "two_bar", "two_bar", 3),
]

# ---------------------------------------------------------------------------
# Param sweep definitions — one entry per strategy.
# Each key maps to a list of values to try. The sweep generates every
# combination (cartesian product) of all listed params for that strategy.
# Add/remove values here to control sweep density vs runtime.
# ---------------------------------------------------------------------------
PARAM_SWEEP_DEFS: Dict[str, Dict[str, list]] = {

    # ------------------------------------------------------------------
    # SIGNAL_TUNING strategies — keys patch config.SIGNAL_TUNING (Tier 1)
    # ------------------------------------------------------------------

    "rsi_momentum": {
        "rsi_momentum_oversold":  [25, 28, 30, 33],
        "rsi_momentum_period":    [7, 10, 14, 20],
        "rsi_momentum_min_score": [0.55, 0.60, 0.65],
    },
    "bollinger_breakout": {
        "bb_breakout_period": [15, 20, 25],
        "bb_breakout_std":    [1.8, 2.0, 2.2, 2.5],
        "bb_adx_min":         [22, 25, 28, 30],
    },
    "ema_crossover": {
        "ema_fast_period": [5, 7, 9, 12],
        "ema_slow_period": [18, 21, 26, 34],
        "ema_trend_period":[40, 50, 60],
    },
    "mean_reversion": {
        "mean_rev_zscore_entry": [1.5, 1.8, 2.0, 2.2, 2.5],
        "mean_rev_period":       [10, 15, 20, 25, 30],
        "mean_rev_min_score":    [0.55, 0.60, 0.65],
    },
    "scalp_master": {
        "scalp_rsi_oversold": [28, 30, 32, 35],
        "scalp_adx_min":      [15, 18, 20, 25],
        "scalp_min_score":    [0.55, 0.60, 0.65],
    },
    "swing_trader": {
        "swing_adx_min":        [22, 25, 28, 30],
        "swing_rsi_oversold":   [28, 30, 32, 35],
        "swing_rsi_overbought": [65, 68, 70, 72],
    },
    "grid_bot": {
        "grid_adx_max":   [12, 15, 18, 20],
        "grid_bb_period": [15, 20, 25, 30],
    },
    "dca_accumulator": {
        "dca_dip_pct":    [0.01, 0.015, 0.02, 0.025],
        "dca_rsi_max":    [28, 32, 35, 40],
        "dca_ema_period": [30, 50, 70],
    },
    "vwap_momentum": {
        "vwap_mom_adx_min":  [20, 22, 25, 28],
        "vwap_mom_rsi_low":  [35, 38, 40, 42],
        "vwap_mom_rsi_high": [58, 60, 62, 65],
    },

    # ------------------------------------------------------------------
    # Params-dataclass strategies — keys patch strategy_obj.params (Tier 2)
    # ------------------------------------------------------------------

    # rsi_dip_spike_v4: RSIDipSpikeV4Params fields
    "rsi_dip_spike_v4": {
        "rsi_oversold": [25, 28, 30, 33, 36],
        "adx_min":      [20, 25, 30],
        "tp_atr_mult":  [1.5, 2.0, 2.5, 3.0],
    },
    # btc_v6_chandelier: BTCV6ChandelierParams fields
    "btc_v6_chandelier": {
        "rsi_thresh": [38.0, 42.0, 45.0, 48.0, 52.0],
        "atr_mult":   [3.0, 3.5, 4.0, 4.5, 5.0, 5.5],
        "adx_min":    [18.0, 20.0, 22.0, 25.0],
    },
    # map_strategy: MapParams fields
    "map_strategy": {
        "sl_mult":    [1.5, 2.0, 2.5, 3.0],
        "tp_mult":    [2.0, 3.0, 4.0, 5.0, 6.0],
        "adx_thresh": [16.0, 18.0, 20.0, 22.0, 25.0],
        "min_r1":     [0.002, 0.003, 0.004, 0.005],
        "min_r2":     [0.004, 0.005, 0.006, 0.007],
        "min_r3":     [0.006, 0.008, 0.010, 0.012],
    },




    # rsi_dip_simple: RSIDipSimpleParams fields
    "rsi_dip_simple": {
        "rsi_oversold": [20.0, 22.0, 25.0, 28.0, 30.0],
        "rsi_exit":     [68.0, 72.0, 75.0, 80.0, 85.0],
        "sl_atr_mult":  [2.0, 2.5, 3.0, 3.5],
    },

    # mr_03_fbs: MR03Params fields
    "mr_03_fbs": {
        "bb_len":    [15, 20, 25, 30],
        "bb_mult":   [1.5, 1.75, 2.0, 2.25],
        "tp_mult":   [1.2, 1.5, 1.8, 2.0],
        "sl_mult":   [1.5, 2.0, 2.5, 3.0],
        "max_bars":  [24, 36, 48],
    },
    # kds_mean_reversion: KDSMeanReversionParams fields
    "kds_mean_reversion": {
        "kds_thresh": [1.5, 2.0, 2.5, 3.0],
        "rsi_os":     [28.0, 32.0, 35.0, 38.0],
        "rsi_ob":     [62.0, 65.0, 68.0, 72.0],
        "sl_mult":    [1.0, 1.5, 2.0, 2.5],
        "adx_max":    [22.0, 25.0, 28.0, 32.0],
    },
    # ema_ribbon_breakout: EMARibbonBreakoutParams fields
    "ema_ribbon_breakout": {
        "adx_min":  [18.0, 20.0, 22.0, 25.0, 28.0],
        "atr_mult": [2.0, 2.5, 3.0, 3.5, 4.0],
        "sl_pct":   [4.0, 5.0, 6.0, 7.0, 8.0],
    },
    # rcr_mean_reversion: RCRMeanReversionParams fields
    "rcr_mean_reversion": {
        "z_thresh":    [1.2, 1.5, 1.8, 2.0, 2.2],
        "stop_atr_k":  [0.8, 1.0, 1.5, 2.0],
        "ac_thresh":   [-0.10, -0.05, 0.0, 0.05],
    },
    # ecb_strategy: ECBParams fields
    "ecb_strategy": {
        "entropy_thresh": [0.65, 0.70, 0.75, 0.80],
        "sl_atr_mult":    [1.0, 1.5, 2.0, 2.5],
        "tp_atr_mult":    [2.0, 2.5, 3.0, 3.5],
        "max_bars_hold":  [16, 20, 24, 32],
    },
    # cbae_strategy: CBAEParams fields
    "cbae_strategy": {
        "extreme_thresh": [4.0, 5.0, 6.0, 7.0],
        "sl_atr_mult":    [1.0, 1.5, 2.0, 2.5],
        "tp_atr_mult":    [1.5, 2.0, 2.5, 3.0],
        "adx_max":        [25.0, 28.0, 30.0, 35.0],
    },
    # rare_strategy: RAREParams fields
    "rare_strategy": {
        "z_entry":     [1.5, 1.8, 2.0, 2.2],
        "sl_atr_mult": [1.5, 2.0, 2.5, 3.0],
        "tp_atr_mult": [2.0, 2.5, 3.0, 3.5],
    },
    # fels_strategy: FELSParams fields
    "fels_strategy": {
        "atr_close_thresh": [0.6, 0.8, 1.0, 1.2],
        "tp_revert_pct":    [0.4, 0.5, 0.6, 0.7],
        "adx_max":          [22.0, 25.0, 28.0, 32.0],
        "sl_atr_buffer":    [0.2, 0.3, 0.4, 0.5],
    },
    # pll_cycle: PLLCycleParams fields
    "pll_cycle": {
        "lock_thresh": [0.8, 1.0, 1.2, 1.5],
        "sl_pct":      [2.0, 2.5, 3.0, 3.5, 4.0],
        "tp_pct":      [8.0, 10.0, 12.0, 15.0],
    },
}


def build_param_combos(strategy: str) -> List[Dict[str, float]]:
    """Return list of param override dicts for every combination in the sweep def."""
    defs = PARAM_SWEEP_DEFS.get(strategy)
    if not defs:
        return [{}]  # no sweep defined — single run with defaults
    keys   = list(defs.keys())
    values = list(defs.values())
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


CSV_FIELDS = [
    "rank_hint",
    "strategy",
    "timeframe",
    "side",
    "stop_profile",
    "stop_mode",
    "initial_stop",
    "trail_stop",
    "lookback",
    "asset_class",
    "days",
    "symbols_tested",
    "symbols_with_trades",
    "symbols_skipped",
    "total_trades",
    "wins",
    "losses",
    "win_rate",
    "profit_factor",
    "total_pnl",
    "total_return_pct",
    "avg_symbol_return_pct",
    "max_drawdown_pct",
    "avg_sharpe",
    "avg_sortino",
    "avg_bars_held",
    "fees_paid",
    "started_at",
    "finished_at",
    "elapsed_sec",
    "symbols",
    "param_overrides",
]


@dataclass(frozen=True)
class Combo:
    strategy: str
    timeframe: str
    side: str
    stop_profile: str
    stop_mode: str
    initial_stop: str
    trail_stop: str
    lookback: int
    param_overrides: tuple = ()   # tuple of (key, value) pairs — hashable, converts to dict for run


def split_csv(value: str | None, default: Sequence[str]) -> List[str]:
    if not value:
        return list(default)
    return [part.strip() for part in value.split(",") if part.strip()]


def read_watchlist_file(path: Path) -> List[str]:
    if not path.exists():
        return []
    out: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        out.append(text.split(",")[0].strip())
    return out


def unique_symbols(symbols: Iterable[str], asset: str) -> List[str]:
    seen = set()
    out = []
    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if not sym or sym == "ALL":
            continue
        if asset == "crypto":
            if "-" in sym and "/" not in sym:
                base, quote = sym.split("-", 1)
                sym = f"{base}/{quote}"
            if "/" not in sym and sym.endswith("USD"):
                sym = f"{sym[:-3]}/USD"
        else:
            sym = sym.replace("/", "").replace("-", "")
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def default_symbols(asset: str) -> List[str]:
    if asset == "crypto":
        raw = list(getattr(config, "CRYPTO_WATCHLIST", []) or [])
        raw += read_watchlist_file(ROOT / "watchlists" / "crypto.txt")
        raw += read_watchlist_file(ROOT / "watchlist" / "scanned_crypto.txt")
    else:
        raw = list(getattr(config, "STOCK_WATCHLIST", []) or [])
        raw += read_watchlist_file(ROOT / "watchlists" / "stocks.txt")
        raw += read_watchlist_file(ROOT / "watchlist" / "scanned_stocks.txt")
    return unique_symbols(raw, asset)


def build_combos(args) -> List[Combo]:
    default_tfs = FULL_TIMEFRAMES if args.full else QUICK_TIMEFRAMES
    default_sides = FULL_SIDES if args.full else QUICK_SIDES
    stop_profiles = FULL_STOP_PROFILES if args.full else QUICK_STOP_PROFILES

    strategies = split_csv(args.strategies or args.strategy, DEFAULT_STRATEGIES)
    timeframes = split_csv(args.timeframes, default_tfs)
    sides = split_csv(args.sides, default_sides)

    combos = []
    for strategy in strategies:
        param_combos = build_param_combos(strategy) if getattr(args, "param_sweep", False) else [{}]
        for tf in timeframes:
            for side in sides:
                for label, stop_mode, initial_stop, trail_stop, lookback in stop_profiles:
                    for param_dict in param_combos:
                        combos.append(Combo(
                            strategy=strategy,
                            timeframe=tf,
                            side=side,
                            stop_profile=label,
                            stop_mode=stop_mode,
                            initial_stop=initial_stop,
                            trail_stop=trail_stop,
                            lookback=lookback,
                            param_overrides=tuple(sorted(param_dict.items())),
                        ))
    return combos[: max(1, args.max_combos)]


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        num = float(value)
        if math.isnan(num) or math.isinf(num):
            return default
        return num
    except Exception:
        return default


def summarize_combo(results, combo: Combo, args, symbols: Sequence[str], started: float) -> dict:
    finished = time.time()
    trades = [trade for result in results for trade in (getattr(result, "trades", []) or [])]
    wins = [trade for trade in trades if safe_float(getattr(trade, "pnl", 0)) > 0]
    losses = [trade for trade in trades if safe_float(getattr(trade, "pnl", 0)) <= 0]
    gross_win = sum(safe_float(getattr(trade, "pnl", 0)) for trade in wins)
    gross_loss = abs(sum(safe_float(getattr(trade, "pnl", 0)) for trade in losses))
    profit_factor = gross_win / gross_loss if gross_loss else (999.0 if gross_win > 0 else 0.0)
    total_start = sum(safe_float(getattr(r, "starting_capital", args.capital)) for r in results)
    total_end = sum(safe_float(getattr(r, "ending_capital", args.capital)) for r in results)
    total_pnl = total_end - total_start
    symbol_returns = [safe_float(getattr(r, "total_return_pct", 0)) for r in results]
    drawdowns = [safe_float(getattr(r, "max_drawdown_pct", 0)) for r in results]
    sharpes = [safe_float(getattr(r, "sharpe_ratio", 0)) for r in results]
    sortinos = [safe_float(getattr(r, "sortino_ratio", 0)) for r in results]
    bars_held = [safe_float(getattr(r, "avg_bars_held", 0)) for r in results if getattr(r, "total_trades", 0)]
    fees_paid = sum(safe_float(getattr(r, "fees_paid", 0)) for r in results)
    total_trades = len(trades)
    win_rate = len(wins) / total_trades * 100 if total_trades else 0.0
    total_return_pct = total_pnl / total_start * 100 if total_start else 0.0
    max_dd = min(drawdowns) if drawdowns else 0.0
    rank_hint = (
        profit_factor * 10
        + total_return_pct
        + win_rate / 10
        + max_dd / 5
        + min(total_trades, 200) / 20
    )

    return {
        "rank_hint": round(rank_hint, 4),
        "strategy": combo.strategy,
        "timeframe": combo.timeframe,
        "side": combo.side,
        "stop_profile": combo.stop_profile,
        "stop_mode": combo.stop_mode,
        "initial_stop": combo.initial_stop,
        "trail_stop": combo.trail_stop,
        "lookback": combo.lookback,
        "asset_class": args.asset,
        "days": args.days,
        "symbols_tested": len(symbols),
        "symbols_with_trades": len(results),
        "symbols_skipped": max(0, len(symbols) - len(results)),
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "avg_symbol_return_pct": round(sum(symbol_returns) / len(symbol_returns), 2) if symbol_returns else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "avg_sharpe": round(sum(sharpes) / len(sharpes), 3) if sharpes else 0.0,
        "avg_sortino": round(sum(sortinos) / len(sortinos), 3) if sortinos else 0.0,
        "avg_bars_held": round(sum(bars_held) / len(bars_held), 2) if bars_held else 0.0,
        "fees_paid": round(fees_paid, 2),
        "started_at": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "finished_at": datetime.fromtimestamp(finished).isoformat(timespec="seconds"),
        "elapsed_sec": round(finished - started, 2),
        "symbols": " ".join(symbols),
        "param_overrides": json.dumps(dict(combo.param_overrides)) if combo.param_overrides else "",
    }


def append_csv(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def write_top_csv(path: Path, rows: Sequence[dict]) -> None:
    top_path = path.with_name(path.stem + "_top.csv")
    ranked = sorted(rows, key=lambda r: safe_float(r.get("rank_hint")), reverse=True)
    with top_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ranked)


def run_combo(bt: Backtester, combo: Combo, args, symbols: Sequence[str]):
    results = []
    delay = max(0.0, float(getattr(args, "symbol_delay_sec", 0.0) or 0.0))
    param_overrides = dict(combo.param_overrides) if combo.param_overrides else None
    for idx, symbol in enumerate(symbols):
        # Only throttle when we're about to hit the network — skip if cached
        if idx and delay and not bt.is_cached(symbol, args.days, combo.timeframe):
            time.sleep(delay)
        if combo.strategy == "original_scorer":
            result = bt.run(
                symbol,
                days=args.days,
                starting_capital=args.capital,
                timeframe=combo.timeframe,
                asset_class=args.asset,
                stop_mode=combo.stop_mode,
                two_bar_lookback=combo.lookback,
                initial_stop_mode=combo.initial_stop,
                trail_mode=combo.trail_stop,
                entry_side_filter=combo.side,
            )
        else:
            result = bt.run_strategy(
                symbol,
                combo.strategy,
                days=args.days,
                starting_capital=args.capital,
                timeframe=combo.timeframe,
                asset_class=args.asset,
                stop_mode=combo.stop_mode,
                two_bar_lookback=combo.lookback,
                initial_stop_mode=combo.initial_stop,
                trail_mode=combo.trail_stop,
                entry_side_filter=combo.side,
                param_overrides=param_overrides,
            )
        if result and getattr(result, "total_trades", 0) > 0:
            results.append(result)
    return results


def parse_args():
    parser = argparse.ArgumentParser(description="Run a bounded backtest matrix and export summary rows.")
    parser.add_argument("--asset", choices=["crypto", "stock"], default="crypto")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Default uses config/watchlist.")
    parser.add_argument("--max-symbols", type=int, default=8, help="Cap symbols per combo. Default: 8")
    parser.add_argument("--strategy", default="", help="Single strategy name. Alias for --strategies.")
    parser.add_argument("--strategies", default="", help="Comma-separated strategies. Default: core active set.")
    parser.add_argument("--timeframes", default="", help="Comma-separated timeframes. Default: quick/full preset.")
    parser.add_argument("--sides", default="", help="Comma-separated sides: long,short,all. Default: quick/full preset.")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--capital", type=float, default=float(getattr(config, "STARTING_CAPITAL", 100000)))
    parser.add_argument("--max-combos", type=int, default=40, help="Hard cap on matrix combos. Default: 40")
    parser.add_argument(
        "--symbol-delay-sec",
        type=float,
        default=0.35,
        help="Sleep between symbols inside each combo to avoid data-provider bursts. Default: 0.35",
    )
    parser.add_argument("--full", action="store_true", help="Use the broader preset lists before max-combos cap.")
    parser.add_argument("--param-sweep", action="store_true",
                        help="Expand param combinations from PARAM_SWEEP_DEFS for the selected strategy.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned combos and exit.")
    parser.add_argument("--out", default="", help="CSV output path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.chdir(ROOT)
    clear_cancel()

    if args.symbols:
        symbols = unique_symbols(split_csv(args.symbols, []), args.asset)
    else:
        symbols = default_symbols(args.asset)
    symbols = symbols[: max(1, args.max_symbols)]
    combos = build_combos(args)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else ROOT / "reports" / "backtest_matrix" / f"matrix_{args.asset}_{stamp}.csv"
    if not out.is_absolute():
        out = ROOT / out
    jsonl = out.with_suffix(".jsonl")

    print(f"[MATRIX] asset={args.asset} symbols={len(symbols)} combos={len(combos)} days={args.days}")
    print(f"[MATRIX] csv={out}")
    print(f"[MATRIX] jsonl={jsonl}")
    if symbols:
        print(f"[MATRIX] symbols: {' '.join(symbols)}")
    if args.dry_run:
        for idx, combo in enumerate(combos, 1):
            print(f"{idx:03d}: {combo}")
        return 0

    bt = Backtester(starting_capital=args.capital)
    rows = []
    for idx, combo in enumerate(combos, 1):
        started = time.time()
        param_str = f" params={dict(combo.param_overrides)}" if combo.param_overrides else ""
        print(
            f"[{idx}/{len(combos)}] {combo.strategy} {combo.timeframe} "
            f"side={combo.side} stop={combo.stop_profile}{param_str}",
            flush=True,
        )
        try:
            results = run_combo(bt, combo, args, symbols)
            row = summarize_combo(results, combo, args, symbols, started)
        except KeyboardInterrupt:
            print("\n[MATRIX] cancelled by keyboard")
            return 130
        except Exception as exc:
            row = {
                **{field: "" for field in CSV_FIELDS},
                **asdict(combo),
                "asset_class": args.asset,
                "days": args.days,
                "symbols_tested": len(symbols),
                "symbols": " ".join(symbols),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "elapsed_sec": round(time.time() - started, 2),
                "rank_hint": -9999,
                "total_trades": 0,
                "error": repr(exc),
            }
            print(f"  ERROR: {exc}")
        rows.append(row)
        append_csv(out, row)
        append_jsonl(jsonl, row)
        print(
            f"  trades={row.get('total_trades', 0)} win={row.get('win_rate', 0)}% "
            f"pf={row.get('profit_factor', 0)} ret={row.get('total_return_pct', 0)}% "
            f"dd={row.get('max_drawdown_pct', 0)}%",
            flush=True,
        )

    write_top_csv(out, rows)
    print(f"[MATRIX] done. Sorted copy: {out.with_name(out.stem + '_top.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
