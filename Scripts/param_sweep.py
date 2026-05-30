"""
=================================================================
  PARAM SWEEP  -- standalone CLI parameter optimizer
  Runs entirely independently of the web dashboard.
  Safe to run for hours; reports are written to disk so a server
  restart or browser close never loses your results.

  DISCOVERY
  ---------
    # List all strategy names
    python Scripts/param_sweep.py --list-strategies

    # Show params + default ranges for a strategy, prints a
    # ready-to-paste sweep command you can copy and tweak
    python Scripts/param_sweep.py --list-params mr_04_fvg
    python Scripts/param_sweep.py --list-params grid_bot

  SWEEP (optimizer search)
  ------------------------
    python Scripts/param_sweep.py \
        --strategy mr_04_fvg \
        --param lookback=25:150:5:int \
        --param min_gap_atr=0.12:0.75:0.05 \
        --symbols BTC/USD,ETH/USD,SOL/USD \
        --timeframe 1h --days 365 --asset-class crypto \
        --direction both \
        --method annealing --iterations 80 \
        --metric profit_factor

    # Auto-detect all params (use --list-params to see them first)
    python Scripts/param_sweep.py \
        --strategy grid_bot --all-params \
        --symbols BTC/USD,ETH/USD --timeframe 1h \
        --direction long

  FULL MATRIX (every combination -- can be very slow)
  ---------------------------------------------------
    python Scripts/param_sweep.py \
        --strategy mr_04_fvg --all-params \
        --symbols BTC/USD,ETH/USD --method matrix

  PARAM FORMAT
  ------------
    --param name=min:max:step          (float)
    --param name=min:max:step:int      (integer, step must be whole number)

    Examples:
      --param rsi_period=5:30:1:int    rsi_period from 5 to 30 step 1
      --param stop_loss_pct=0.5:3.0:0.25
      --param atr_len=7:42:1:int

  DIRECTION FILTER
  ----------------
    --direction long    only count long-side entries
    --direction short   only count short-side entries
    --direction both    count all entries (default)

  METHODS
  -------
    annealing    simulated annealing (smart exploration, good for many params)
    random       random sampling with restarts
    sequential   one param at a time, holds others fixed
    all_random   pure random, no restarts
    matrix       exhaustive grid -- every combination (slow, confirms before >5000)
    zoom         two-stage coarse-then-fine grid (recommended for 3+ params)
                   Stage 1: coarse grid (--zoom-coarse-buckets N, default 6)
                             finds the best region fast
                   Stage 2: fine grid (+/- --zoom-fine-window steps around winner)
                             drills into that region with original step size
                 Example: range [1,75] step=1 with buckets=6 window=3
                   Coarse: 1,13,26,39,52,65,75  (7 values, winner=39)
                   Fine:   36,37,38,39,40,41,42  (7 values, step=1)

  METRICS
  -------
    profit_factor   gross profit / gross loss  [maximize]
    win_rate        % of trades profitable      [maximize]
    expectancy      avg $ per trade             [maximize]
    net_pnl         total P&L                   [maximize]
    sharpe          Sharpe ratio                [maximize]
    consistency     % of symbols profitable     [maximize]

  OUTPUT
  ------
    Reports written to  reports/param_sweeps/
    as  <strategy>_<timestamp>.json  and  <strategy>_<timestamp>.csv
    Progress printed to stdout every iteration.
    Ctrl+C saves partial results before exiting.
=================================================================
"""

import argparse
import csv
import dataclasses
import importlib
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Path setup — same pattern as Scripts/backtester.py
# ---------------------------------------------------------------------------
_script_dir  = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.normpath(os.path.join(_script_dir, ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
# intelligence/ lives under logs/ in this tree
_logs_dir = os.path.join(_project_root, "logs")
if _logs_dir not in sys.path:
    sys.path.insert(0, _logs_dir)
os.chdir(_project_root)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_project_root, ".env"))
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [param_sweep] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy registry  (mirrors web_dashboard.py — keep in sync when adding
# new strategies)
# ---------------------------------------------------------------------------
STRATEGY_MAP = {
    "rsi_momentum":       ("strategies.rsi_momentum",       "RSIMomentum"),
    "bollinger_breakout": ("strategies.bollinger_breakout",  "BollingerBreakout"),
    "bollinger_squeeze":  ("strategies.bollinger_squeeze",   "BollingerSqueeze"),
    "ema_crossover":      ("strategies.ema_crossover",       "EMACrossover"),
    "mean_reversion":     ("strategies.mean_reversion",      "MeanReversion"),
    "scalp_master":       ("strategies.scalp_master",        "ScalpMaster"),
    "swing_trader":       ("strategies.swing_trader",        "SwingTrader"),
    "grid_bot":           ("strategies.grid_bot",            "GridBot"),
    "dca_accumulator":    ("strategies.dca_accumulator",     "DCAAccumulator"),
    "vwap_momentum":      ("strategies.vwap_momentum",       "VWAPMomentum"),
    "vwap_confirmed_orb": ("strategies.vwap_confirmed_orb",  "VwapConfirmedOrb"),
    "hammer_reversal":    ("strategies.hammer_reversal",     "HammerReversal"),
    "orb_breakout":       ("strategies.orb_breakout",        "ORBBreakout"),
    "adaptive_regime":    ("strategies.adaptive_regime",     "AdaptiveRegime"),
    "ecb_strategy":       ("strategies.ecb_strategy",        "ECBStrategy"),
    "vdmr_strategy":      ("strategies.vdmr_strategy",       "VDMRStrategy"),
    "rsi_dip_spike_v4":   ("strategies.rsi_dip_spike_v4",   "RSIDipSpikeV4Strategy"),
    "mr_02_vef":              ("strategies.mr_02_vef_strategy",  "MR02VEFStrategy"),
    "mr_03_fbs":              ("strategies.mr_03_fbs_strategy",  "MR03FBSStrategy"),
    "mr_04_fvg":              ("strategies.mr_04_fvg_strategy",  "MR04FVGStrategy"),
    "btc_v6_chandelier":      ("strategies.btc_v6_chandelier",   "BTCV6ChandelierStrategy"),
    "rsi_dip_simple":         ("strategies.rsi_dip_simple",      "RSIDipSimpleStrategy"),
    "pll_cycle":              ("strategies.pll_cycle",           "PLLCycleStrategy"),
    "pll_cycle_martingale":   ("strategies.pll_cycle",           "PLLCycleMartingaleStrategy"),
    "kds_mean_reversion":     ("strategies.kds_mean_reversion",  "KDSMeanReversionStrategy"),
    "ema_ribbon_breakout":    ("strategies.ema_ribbon_breakout", "EMARibbonBreakoutStrategy"),
    "rcr_mean_reversion":     ("strategies.rcr_mean_reversion",  "RCRMeanReversionStrategy"),
}


# ---------------------------------------------------------------------------
# Param introspection  (same logic as web_dashboard.get_strategy_optimizer_params)
# ---------------------------------------------------------------------------

# Operational/infrastructure attrs that are not strategy-tuning params.
# These appear on the flat-attr strategies but should never be swept.
_FLAT_ATTR_BLOCKLIST = frozenset({
    "candle_limit", "enabled", "ml_exempt", "reviewer_exempt",
    "auto_disable_exempt", "crypto_enabled", "stock_enabled",
})


def _param_range(val, is_int: bool) -> tuple:
    """Return (lo, hi, step) for a single numeric default value."""
    if is_int:
        if val <= 0:
            return None
        step = max(1, val // 10)
        lo   = max(1, val // 2)
        hi   = val * 3
        return lo, hi, step
    else:
        if val <= 0:
            return 0.5, 5.0, 0.5
        magnitude = 10 ** math.floor(math.log10(val))
        step = round(magnitude * 0.5, 4)
        lo   = round(max(0.1, val * 0.4), 4)
        hi   = round(val * 2.5, 4)
        return lo, hi, step


def get_strategy_params(strategy_name: str) -> list:
    """
    Returns list of dicts: {name, default, min, max, step, is_int}
    Skips bool/string fields.

    Handles two strategy patterns:
      1. obj.params is a dataclass  (newer mr_* / ecb / vdmr strategies)
      2. Flat numeric attributes on the object itself  (most older strategies)
    """
    if strategy_name not in STRATEGY_MAP:
        return []
    try:
        module_path, class_name = STRATEGY_MAP[strategy_name]
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        obj = cls()

        # ── Pattern 1: params dataclass ──────────────────────────────────────
        params_obj = getattr(obj, "params", None)
        if params_obj is not None and dataclasses.is_dataclass(params_obj):
            result = []
            for f in dataclasses.fields(params_obj):
                val = getattr(params_obj, f.name)
                if isinstance(val, (bool, str)):
                    continue
                is_int   = isinstance(val, int)
                is_float = isinstance(val, float)
                if not (is_int or is_float):
                    continue
                rng = _param_range(val, is_int)
                if rng is None:
                    continue
                lo, hi, step = rng
                result.append({"name": f.name, "default": val,
                                "min": lo, "max": hi, "step": step,
                                "is_int": is_int})
            return result

        # ── Pattern 2: flat numeric attributes on the object ─────────────────
        result = []
        for attr in sorted(dir(obj)):
            if attr.startswith("_"):
                continue
            if attr in _FLAT_ATTR_BLOCKLIST:
                continue
            try:
                val = getattr(obj, attr)
            except Exception:
                continue
            if isinstance(val, bool) or callable(val):
                continue
            is_int   = isinstance(val, int)
            is_float = isinstance(val, float)
            if not (is_int or is_float):
                continue
            rng = _param_range(val, is_int)
            if rng is None:
                continue
            lo, hi, step = rng
            result.append({"name": attr, "default": val,
                            "min": lo, "max": hi, "step": step,
                            "is_int": is_int})
        return result

    except Exception as e:
        logger.error(f"get_strategy_params({strategy_name}): {e}")
        return []


# ---------------------------------------------------------------------------
# --list-strategies
# ---------------------------------------------------------------------------

def cmd_list_strategies():
    print("\nAvailable strategies:")
    print("-" * 36)
    for name in sorted(STRATEGY_MAP):
        print(f"  {name}")
    print()
    print("Use --list-params <strategy> to see that strategy's parameters.")
    print()


# ---------------------------------------------------------------------------
# --list-params <strategy>
# ---------------------------------------------------------------------------

def cmd_list_params(strategy_name: str):
    if strategy_name not in STRATEGY_MAP:
        print(f"\nUnknown strategy '{strategy_name}'.")
        print("Run --list-strategies to see valid names.")
        sys.exit(1)

    params = get_strategy_params(strategy_name)
    if not params:
        print(f"\nNo optimizable numeric params found for '{strategy_name}'.")
        sys.exit(0)

    print(f"\nStrategy: {strategy_name}")
    print(f"Params ({len(params)} optimizable):")
    print("-" * 72)
    fmt = "  {:<28} default={:<10} range=[{}, {}]  step={}  {}"
    for p in params:
        tag = "[int]" if p["is_int"] else "[float]"
        print(fmt.format(
            p["name"], p["default"],
            p["min"], p["max"], p["step"], tag
        ))

    # Print a ready-to-paste sweep command using the new = syntax
    print()
    print("Ready-to-paste sweep command (edit symbols / direction / days as needed):")
    print("-" * 72)
    lines = ["python Scripts/param_sweep.py \\"]
    lines.append(f"    --strategy {strategy_name} \\")
    for p in params:
        suffix = ":int" if p["is_int"] else ""
        lines.append(
            f"    --param {p['name']}={p['min']}:{p['max']}:{p['step']}{suffix} \\"
        )
    lines.append(f"    --symbols BTC/USD,ETH/USD,SOL/USD \\")
    lines.append(f"    --timeframe 1h --days 365 --asset-class crypto \\")
    lines.append(f"    --direction both \\")
    lines.append(f"    --method annealing --iterations 60 --metric profit_factor")
    print("\n".join(lines))
    print()
    print("Direction options: long  short  both")
    print("Method options:    annealing  random  sequential  matrix")
    print("Metric options:    profit_factor  win_rate  expectancy  net_pnl  sharpe  consistency")
    print()


# ---------------------------------------------------------------------------
# JSON / CSV serialization helpers
# ---------------------------------------------------------------------------

class _Encoder(json.JSONEncoder):
    def default(self, obj):
        try:
            import numpy as np
            if isinstance(obj, np.integer): return int(obj)
            if isinstance(obj, np.floating): return float(obj)
            if isinstance(obj, np.ndarray):  return obj.tolist()
        except ImportError:
            pass
        return super().default(obj)


def result_to_dict(r) -> dict:
    return {
        "params":             r.params,
        "score":              round(r.score, 6),
        "avg_profit_factor":  round(r.avg_profit_factor, 4),
        "avg_win_rate":       round(r.avg_win_rate, 2),
        "avg_net_pnl":        round(r.avg_net_pnl, 2),
        "consistency":        round(r.consistency, 4),
        "symbols_tested":     r.symbols_tested,
        "symbols_profitable": r.symbols_profitable,
        "per_symbol":         r.per_symbol,
    }


def save_results(results, strategy_name: str, run_meta: dict, out_dir: str, label: str = ""):
    os.makedirs(out_dir, exist_ok=True)
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem  = f"{strategy_name}{'_' + label if label else ''}_{ts}"

    # JSON
    json_path = os.path.join(out_dir, stem + ".json")
    payload = {
        "meta":    run_meta,
        "results": [result_to_dict(r) for r in results],
    }
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2, cls=_Encoder)

    # CSV
    csv_path = os.path.join(out_dir, stem + ".csv")
    if results:
        flat_rows = []
        for r in results:
            row = dict(r.params)
            row["score"]             = round(r.score, 6)
            row["avg_profit_factor"] = round(r.avg_profit_factor, 4)
            row["avg_win_rate"]      = round(r.avg_win_rate, 2)
            row["avg_net_pnl"]       = round(r.avg_net_pnl, 2)
            row["consistency"]       = round(r.consistency, 4)
            row["symbols_tested"]    = r.symbols_tested
            row["symbols_profitable"]= r.symbols_profitable
            flat_rows.append(row)
        cols = list(flat_rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(flat_rows)

    return json_path, csv_path


# ---------------------------------------------------------------------------
# Print summary table
# ---------------------------------------------------------------------------

def print_summary(results, metric: str, top_n: int = 20):
    if not results:
        print("\nNo results to display.")
        return
    top = results[:top_n]
    print(f"\n{'='*72}")
    print(f"  TOP {min(top_n, len(results))} RESULTS  (ranked by score = {metric} × consistency)")
    print(f"{'='*72}")
    # Header
    param_names = list(top[0].params.keys())
    hdr_params  = "  ".join(f"{n[:14]:<14}" for n in param_names)
    print(f"  Rank  Score   PF     WR%    PnL    Consist  {hdr_params}")
    print(f"  {'-'*68}")
    for i, r in enumerate(top, 1):
        pvals = "  ".join(f"{str(v):<14}" for v in r.params.values())
        print(
            f"  {i:<5} {r.score:<7.4f} {r.avg_profit_factor:<6.2f} "
            f"{r.avg_win_rate:<6.1f} {r.avg_net_pnl:<6.2f} "
            f"{r.consistency:<8.0%}  {pvals}"
        )
    print(f"{'='*72}\n")


# ---------------------------------------------------------------------------
# Matrix (full grid) sweep
# ---------------------------------------------------------------------------

def run_matrix(opt, strategy_name, param_ranges, symbols, metric, run_kwargs,
               out_dir, label, strategy_name_label, skip: int = 0):
    """Exhaustive grid: every combination of param step values."""
    import itertools
    from intelligence.optimizer import ParamRange

    # Build all combinations
    grids = [pr.values() for pr in param_ranges]
    combos = list(itertools.product(*grids))
    total  = len(combos)

    if skip:
        print(f"\nResuming: skipping first {skip} combos (already done).")
        combos = combos[skip:]

    print(f"\nMatrix mode: {total:,} combinations across {len(symbols)} symbol(s) "
          f"(running {len(combos):,} remaining)")
    if total > 5000:
        print(f"WARNING: {total:,} combos × {len(symbols)} symbols = "
              f"{total * len(symbols):,} backtests — this may take a very long time.")
        ans = input("Continue? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            sys.exit(0)

    results_collected = []
    cancelled = False

    def _sigint(sig, frame):
        nonlocal cancelled
        cancelled = True
        print("\n\nCtrl+C received — saving partial results...")

    signal.signal(signal.SIGINT, _sigint)

    t0 = time.time()
    for idx, combo in enumerate(combos, 1):
        if cancelled:
            break
        params = {pr.name: v for pr, v in zip(param_ranges, combo)}

        try:
            sym_results = opt._run_combo(strategy_name, symbols, params, run_kwargs)
            r = opt._build_opt_result(params, sym_results, symbols, metric, minimize=False)
            results_collected.append(r)
        except Exception as e:
            logger.warning(f"Combo {idx} failed: {e}")

        elapsed  = time.time() - t0
        per_iter = elapsed / idx
        eta      = (total - idx) * per_iter
        best     = max((r.score for r in results_collected), default=0.0)
        print(
            f"\r  {idx}/{total} ({idx/total:.0%})  "
            f"best={best:.4f}  "
            f"elapsed={elapsed/60:.1f}m  ETA={eta/60:.1f}m   ",
            end="", flush=True,
        )

    print()
    results_collected.sort(key=lambda r: r.score, reverse=True)
    return results_collected


# ---------------------------------------------------------------------------
# Zoom sweep  (coarse matrix -> narrow around winner -> fine matrix)
# ---------------------------------------------------------------------------

def run_zoom(opt, strategy_name, param_ranges, symbols, metric, run_kwargs,
             coarse_buckets: int, fine_window: int):
    """
    Two-stage coarse-to-fine grid sweep.

    Stage 1 -- coarse:
        Divide each param's full range into `coarse_buckets` equal slices.
        Run a full matrix across those coarse values.
        Find the winning combo.

    Stage 2 -- fine:
        For each param, centre a tight window of +/- `fine_window` original
        steps around the coarse winner.  Run a full matrix on those values.

    Returns (all_results, coarse_winner_params)
    """
    import itertools
    from intelligence.optimizer import ParamRange

    # ── Stage 1: build coarse ranges ────────────────────────────────────────
    coarse_ranges = []
    for pr in param_ranges:
        span  = pr.max_val - pr.min_val
        # coarse step = span / coarse_buckets, rounded to original step granularity
        raw_step = span / max(coarse_buckets, 1)
        if pr.is_int:
            c_step = max(1, int(round(raw_step)))
        else:
            c_step = round(raw_step, 6)
        coarse_ranges.append(ParamRange(pr.name, pr.min_val, pr.max_val, c_step, pr.is_int))

    coarse_combos = list(itertools.product(*[pr.values() for pr in coarse_ranges]))
    c_total = len(coarse_combos)

    # ── Gap check: fine window must cover at least half the coarse step gap ──
    # If coarse_step=12 and fine_window=3 (original step=1), fine only covers
    # ±3 around winner but neighbouring coarse points are 12 apart — 6-unit
    # dead zones exist on each side where the true optimum could hide.
    gap_warnings = []
    min_window_needed = 0
    for pr, cr in zip(param_ranges, coarse_ranges):
        half_gap = cr.step / pr.step / 2   # coarse gap in original-step units
        if fine_window < half_gap:
            gap_warnings.append(
                f"    {pr.name}: coarse_step={cr.step:.4g}  orig_step={pr.step:.4g}  "
                f"need window>={math.ceil(half_gap)}  got {fine_window}"
            )
            min_window_needed = max(min_window_needed, math.ceil(half_gap))

    if gap_warnings:
        print(f"\n  WARNING: fine window ({fine_window}) is too small -- blind spots exist!")
        for w in gap_warnings:
            print(w)
        print(f"  Recommended: --zoom-fine-window {min_window_needed}  (covers all gaps)")
        print(f"  Continuing anyway -- results may miss the true optimum.\n")

    print(f"\n  STAGE 1 (coarse) -- {c_total:,} combos  "
          f"({coarse_buckets} buckets per param)")

    cancelled = False
    def _sigint(sig, frame):
        nonlocal cancelled
        cancelled = True
        print("\n\nCtrl+C -- saving partial results...")
    signal.signal(signal.SIGINT, _sigint)

    coarse_results = []
    t0 = time.time()
    for idx, combo in enumerate(coarse_combos, 1):
        if cancelled:
            break
        params = {pr.name: v for pr, v in zip(coarse_ranges, combo)}
        try:
            sym_results = opt._run_combo(strategy_name, symbols, params, run_kwargs)
            r = opt._build_opt_result(params, sym_results, symbols, metric, minimize=False)
            coarse_results.append(r)
        except Exception as e:
            logger.warning(f"Coarse combo {idx} failed: {e}")

        elapsed  = time.time() - t0
        eta      = (c_total - idx) * (elapsed / idx)
        best     = max((r.score for r in coarse_results), default=0.0)
        print(f"\r  [{idx}/{c_total}] best={best:.4f}  ETA={eta/60:.1f}m   ",
              end="", flush=True)

    print()
    if not coarse_results or cancelled:
        return coarse_results, {}

    coarse_results.sort(key=lambda r: r.score, reverse=True)
    winner = coarse_results[0].params
    print(f"\n  Coarse winner: score={coarse_results[0].score:.4f}")
    for k, v in winner.items():
        print(f"    {k} = {v}")

    # ── Stage 2: build fine ranges centred on winner ─────────────────────────
    fine_ranges = []
    for pr in param_ranges:
        best_val = winner.get(pr.name, (pr.min_val + pr.max_val) / 2)
        half     = fine_window * pr.step
        lo = max(pr.min_val, best_val - half)
        hi = min(pr.max_val, best_val + half)
        # ensure lo != hi (edge of range case)
        if lo >= hi:
            lo = max(pr.min_val, best_val - pr.step)
            hi = min(pr.max_val, best_val + pr.step)
        fine_ranges.append(ParamRange(pr.name, lo, hi, pr.step, pr.is_int))

    fine_combos = list(itertools.product(*[pr.values() for pr in fine_ranges]))
    f_total = len(fine_combos)
    print(f"\n  STAGE 2 (fine)   -- {f_total:,} combos  "
          f"(+/-{fine_window} steps around winner per param)")
    for pr in fine_ranges:
        vals = list(pr.values())
        print(f"    {pr.name:<28} [{pr.min_val} -> {pr.max_val}  step {pr.step}]  "
              f"({len(vals)} values)")

    fine_results = []
    t0 = time.time()
    for idx, combo in enumerate(fine_combos, 1):
        if cancelled:
            break
        params = {pr.name: v for pr, v in zip(fine_ranges, combo)}
        try:
            sym_results = opt._run_combo(strategy_name, symbols, params, run_kwargs)
            r = opt._build_opt_result(params, sym_results, symbols, metric, minimize=False)
            fine_results.append(r)
        except Exception as e:
            logger.warning(f"Fine combo {idx} failed: {e}")

        elapsed  = time.time() - t0
        eta      = (f_total - idx) * (elapsed / idx)
        best     = max((r.score for r in fine_results), default=0.0)
        print(f"\r  [{idx}/{f_total}] best={best:.4f}  ETA={eta/60:.1f}m   ",
              end="", flush=True)

    print()
    fine_results.sort(key=lambda r: r.score, reverse=True)

    # Merge: fine results first (higher fidelity), then any coarse-only combos
    all_results = fine_results + coarse_results
    # de-dupe by params key
    seen = set()
    deduped = []
    for r in all_results:
        key = tuple(sorted(r.params.items()))
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    deduped.sort(key=lambda r: r.score, reverse=True)
    return deduped, winner


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def cmd_sweep(args):
    strategy_name = args.strategy
    if strategy_name not in STRATEGY_MAP:
        print(f"\nUnknown strategy '{strategy_name}'. Run --list-strategies.")
        sys.exit(1)

    # ── Resolve param ranges ─────────────────────────────────────────────────
    from intelligence.optimizer import ParamRange

    if args.all_params:
        raw = get_strategy_params(strategy_name)
        if not raw:
            print(f"No optimizable params found for '{strategy_name}'.")
            sys.exit(1)
        param_ranges = [
            ParamRange(p["name"], p["min"], p["max"], p["step"], p["is_int"])
            for p in raw
        ]
        print(f"Auto-detected {len(param_ranges)} params for '{strategy_name}'.")
    else:
        if not args.params:
            print("Provide --param or use --all-params. Run --list-params <strategy> for names.")
            sys.exit(1)
        param_ranges = []
        for spec in args.params:
            # Accept both formats:
            #   new:  rsi_period=5:30:1:int
            #   old:  rsi_period:5:30:1:int
            if "=" in spec:
                name, rest = spec.split("=", 1)
            else:
                # legacy colon-only format -- first token is the name
                parts_all = spec.split(":")
                if len(parts_all) < 4:
                    print(f"Bad --param format '{spec}'.")
                    print("  Expected: name=min:max:step  or  name=min:max:step:int")
                    print("  Example:  rsi_period=5:30:1:int")
                    sys.exit(1)
                name = parts_all[0]
                rest = ":".join(parts_all[1:])

            name = name.strip()
            parts = rest.split(":")
            if len(parts) < 3:
                print(f"Bad --param range '{spec}'.")
                print("  Expected: name=min:max:step[:int]")
                print("  Example:  stop_loss_pct=0.5:3.0:0.25")
                sys.exit(1)
            mn      = float(parts[0])
            mx      = float(parts[1])
            step    = float(parts[2])
            is_int  = len(parts) >= 4 and parts[3].lower() in ("int", "true", "1")
            param_ranges.append(ParamRange(name, mn, mx, step, is_int))

    # ── Symbols ──────────────────────────────────────────────────────────────
    if args.symbols_file:
        with open(args.symbols_file) as f:
            symbols = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    else:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    if not symbols:
        print("No symbols specified. Use --symbols or --symbols-file.")
        sys.exit(1)

    # direction: "both" means no filter (pass "all" to backtester)
    direction = args.direction if args.direction != "both" else "all"

    # ── Run meta ─────────────────────────────────────────────────────────────
    run_meta = {
        "strategy":    strategy_name,
        "symbols":     symbols,
        "timeframe":   args.timeframe,
        "days":        args.days,
        "asset_class": args.asset_class,
        "direction":   args.direction,
        "method":      args.method,
        "iterations":  args.iterations,
        "metric":      args.metric,
        "params":      [{"name": pr.name, "min": pr.min_val, "max": pr.max_val,
                         "step": pr.step, "is_int": pr.is_int}
                        for pr in param_ranges],
        "started_at":  datetime.now().isoformat(),
    }

    run_kwargs = {
        "days":               args.days,
        "timeframe":          args.timeframe,
        "asset_class":        args.asset_class,
        "entry_side_filter":  direction,
    }

    out_dir = args.output_dir
    label   = args.label

    # ── Load heavy modules ───────────────────────────────────────────────────
    print(f"\nLoading backtester and optimizer...")
    try:
        from intelligence.backtester import Backtester
        from intelligence.optimizer  import StrategyOptimizer as Optimizer
    except Exception as e:
        print(f"Import error: {e}")
        sys.exit(1)

    bt  = Backtester()
    opt = Optimizer(bt)

    # ── Pre-fetch symbols into candle cache ──────────────────────────────────
    print(f"Pre-fetching {len(symbols)} symbol(s) [{args.timeframe} / {args.days}d]...")
    for sym in symbols:
        try:
            if args.asset_class == "crypto":
                df = bt.fetch_history_crypto(sym, days=args.days, timeframe=args.timeframe)
            else:
                df = bt.fetch_history(sym, days=args.days, timeframe=args.timeframe)
            if df is None or df.empty:
                raise ValueError("No data returned")
            print(f"  {sym}: {len(df):,} bars cached")
        except Exception as e:
            print(f"  ERROR fetching {sym}: {e}")
            sys.exit(1)

    # ── Print what we're about to do ─────────────────────────────────────────
    method_label = args.method
    if args.method == "zoom":
        method_label = (f"zoom  (coarse={args.zoom_coarse_buckets} buckets, "
                        f"fine=+/-{args.zoom_fine_window} steps)")
    print(f"\n{'='*64}")
    print(f"  Strategy  : {strategy_name}")
    print(f"  Symbols   : {', '.join(symbols)}")
    print(f"  Timeframe : {args.timeframe}  /  {args.days} days")
    print(f"  Direction : {args.direction}")
    print(f"  Method    : {method_label}")
    if args.method not in ("matrix", "zoom"):
        print(f"  Iterations: {args.iterations}")
    print(f"  Metric    : {args.metric}")
    print(f"  Params    : {len(param_ranges)}")
    for pr in param_ranges:
        tag = "[int]" if pr.is_int else "[float]"
        n_vals = len(list(pr.values()))
        print(f"    {pr.name:<28} [{pr.min_val} -> {pr.max_val}  step {pr.step}]  "
              f"({n_vals} values) {tag}")
    print(f"  Output    : {out_dir}/")
    print(f"{'='*64}\n")

    # ── Matrix / zoom / optimizer sweep ──────────────────────────────────────
    results = []
    cancelled = False

    if args.method == "matrix":
        results = run_matrix(
            opt, strategy_name, param_ranges, symbols,
            args.metric, run_kwargs, out_dir, label, strategy_name,
            skip=args.skip,
        )

    elif args.method == "zoom":
        results, coarse_winner = run_zoom(
            opt, strategy_name, param_ranges, symbols,
            args.metric, run_kwargs,
            coarse_buckets = args.zoom_coarse_buckets,
            fine_window    = args.zoom_fine_window,
        )
        if coarse_winner:
            run_meta["zoom_coarse_winner"] = coarse_winner

    else:
        # Optimizer search (annealing / random / all_random / sequential)
        def _progress(current: int, total: int, best: float):
            pct = current / total * 100 if total else 0
            bar_len = 30
            filled  = int(bar_len * current / total) if total else 0
            bar     = "#" * filled + "." * (bar_len - filled)
            print(
                f"\r  [{bar}] {current}/{total} ({pct:.0f}%)  "
                f"best={best:.4f}   ",
                end="", flush=True,
            )

        def _sigint(sig, frame):
            nonlocal cancelled
            cancelled = True
            print("\n\nCtrl+C — saving partial results...")

        signal.signal(signal.SIGINT, _sigint)

        try:
            results = opt.optimize(
                strategy_name = strategy_name,
                param_ranges  = param_ranges,
                symbols       = symbols,
                metric        = args.metric,
                method        = args.method,
                iterations    = args.iterations,
                progress_cb   = _progress,
                run_kwargs    = run_kwargs,
            )
            print()  # newline after progress bar
        except KeyboardInterrupt:
            cancelled = True
            print("\n\nInterrupted — saving partial results...")
            try:
                results = opt._partial_results or []
            except AttributeError:
                results = []

    # ── Save & summarise ──────────────────────────────────────────────────────
    run_meta["finished_at"] = datetime.now().isoformat()
    run_meta["cancelled"]   = cancelled
    run_meta["total_tested"] = len(results)

    if results:
        json_path, csv_path = save_results(results, strategy_name, run_meta, out_dir, label)
        print_summary(results, args.metric)
        print(f"Reports saved:")
        print(f"  JSON: {json_path}")
        print(f"  CSV:  {csv_path}")
    else:
        print("No results to save.")

    if cancelled:
        print("\nRun cancelled — partial results saved above.")
    else:
        print("\nSweep complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Standalone parameter sweep optimizer — runs independently of the web server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Discovery
    parser.add_argument("--list-strategies", action="store_true",
                        help="Print all available strategy names and exit.")
    parser.add_argument("--list-params", metavar="STRATEGY",
                        help="Print param names, defaults, ranges for STRATEGY and exit.")

    # Sweep target
    parser.add_argument("--strategy", metavar="NAME",
                        help="Strategy to optimize.")
    # --param  accepts both  name=min:max:step[:int]  and legacy  name:min:max:step[:int]
    parser.add_argument("--param", metavar="name=min:max:step[:int]",
                        action="append", default=[], dest="params",
                        help="Param range (repeatable). Format: rsi_period=5:30:1:int")
    parser.add_argument("--all-params", action="store_true",
                        help="Auto-detect all numeric params with default ranges.")

    # Data
    parser.add_argument("--symbols", default="BTC/USD,ETH/USD,SOL/USD",
                        metavar="SYM1,SYM2,...",
                        help="Comma-separated symbol list (default: BTC/USD,ETH/USD,SOL/USD)")
    parser.add_argument("--symbols-file", metavar="FILE",
                        help="Text file with one symbol per line (overrides --symbols).")
    parser.add_argument("--timeframe", default="1h",
                        choices=["5m", "15m", "1h", "1d"],
                        help="Candle timeframe (default: 1h)")
    parser.add_argument("--days", type=int, default=365,
                        help="Days of history to use (default: 365)")
    parser.add_argument("--asset-class", default="crypto",
                        choices=["crypto", "stock"],
                        help="Asset class for data fetch (default: crypto)")
    parser.add_argument("--direction", default="both",
                        choices=["long", "short", "both"],
                        help="Trade direction filter: long, short, or both (default: both)")

    # Optimizer
    parser.add_argument("--method", default="annealing",
                        choices=["annealing", "random", "all_random",
                                 "sequential", "matrix", "zoom"],
                        help="Search method (default: annealing). "
                             "'matrix'=exhaustive grid. "
                             "'zoom'=coarse grid then fine grid around winner.")
    parser.add_argument("--iterations", type=int, default=60,
                        help="Iterations for annealing/random/sequential (default: 60).")
    parser.add_argument("--zoom-coarse-buckets", type=int, default=6, metavar="N",
                        help="zoom: divide each param range into N coarse slices "
                             "(default: 6).  More = slower but better coarse winner.")
    parser.add_argument("--zoom-fine-window", type=int, default=3, metavar="N",
                        help="zoom: search +/-N original steps around coarse winner "
                             "(default: 3).  E.g. winner=45 step=5 window=3 -> [30,60].")
    parser.add_argument("--metric", default="profit_factor",
                        choices=["profit_factor", "win_rate", "net_pnl",
                                 "sharpe", "expectancy", "consistency"],
                        help="Metric to maximise (default: profit_factor)")

    # Output
    parser.add_argument("--output-dir", default="reports/param_sweeps",
                        metavar="DIR",
                        help="Directory for JSON+CSV reports (default: reports/param_sweeps)")
    parser.add_argument("--label", default="",
                        metavar="TEXT",
                        help="Optional label appended to report filename.")
    parser.add_argument("--skip", type=int, default=0, metavar="N",
                        help="Skip first N combos (matrix/zoom resume after interruption).")

    args = parser.parse_args()

    # Route to sub-commands
    if args.list_strategies:
        cmd_list_strategies()
        sys.exit(0)

    if args.list_params:
        cmd_list_params(args.list_params)
        sys.exit(0)

    if not args.strategy:
        parser.print_help()
        print("\nERROR: --strategy is required for a sweep. "
              "Use --list-strategies to see options.")
        sys.exit(1)

    cmd_sweep(args)
