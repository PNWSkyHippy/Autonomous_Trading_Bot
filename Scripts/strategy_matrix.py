"""
=================================================================
  STRATEGY MATRIX  --  full condition-exhaustion test runner
  Runs entirely independently of the web dashboard.

  Fixes the strategy's params at defaults (or overrides you supply)
  and blasts every combination of:
      symbol  x  timeframe  x  direction
  producing a characterisation report showing WHERE the strategy
  has edge and WHERE it breaks.

  USAGE
  -----
    # Quick run -- built-in crypto universe, 1h, all directions
    python Scripts/strategy_matrix.py --strategy grid_bot

    # Custom universe
    python Scripts/strategy_matrix.py \
        --strategy mr_04_fvg \
        --symbols-crypto BTC/USD,ETH/USD,SOL/USD,ADA/USD,AVAX/USD,MATIC/USD \
        --symbols-stock  AAPL,MSFT,NVDA,AMD,TSLA \
        --timeframes 15m,1h,1d \
        --directions long,short,both \
        --days 365

    # Override specific params (use default-detected values for rest)
    python Scripts/strategy_matrix.py \
        --strategy grid_bot \
        --param stop_loss_pct=1.2 \
        --param take_profit_pct=2.5 \
        --days 180

    # List all available strategies
    python Scripts/strategy_matrix.py --list-strategies

  OUTPUT
  ------
    reports/strategy_matrix/
      <strategy>_<timestamp>.json    full per-cell data
      <strategy>_<timestamp>.csv     flat table (one row per cell)
      <strategy>_<timestamp>.txt     human-readable report

    Terminal shows a live progress line + final matrix table.
    Ctrl+C saves partial results.

  READING THE REPORT
  ------------------
    Each cell shows:  PF / WR% / trades
      PF  = profit factor (>1.0 is profitable)
      WR% = win rate
      trades = number of trades (low = unreliable)

    Rows = symbols, Columns = timeframe/direction combos.
    Summary section ranks conditions and symbols by average PF.
    "Edge map" highlights cells with PF >= threshold (default 1.2).
=================================================================
"""

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime
from itertools import product
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_script_dir   = os.path.dirname(os.path.abspath(__file__))
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
    level=logging.WARNING,          # suppress strategy-load chatter during the run
    format="%(asctime)s [matrix] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy registry  (mirrors param_sweep.py)
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
    "kds_mean_reversion":     ("strategies.kds_mean_reversion",   "KDSMeanReversionStrategy"),
    "ema_ribbon_breakout":    ("strategies.ema_ribbon_breakout",  "EMARibbonBreakoutStrategy"),
    "rcr_mean_reversion":     ("strategies.rcr_mean_reversion",   "RCRMeanReversionStrategy"),
    "map_strategy":           ("strategies.map_strategy",          "MAPStrategy"),
    "rare_strategy":          ("strategies.rare_strategy",         "RAREStrategy"),
    "fels_strategy":          ("strategies.fels_strategy",         "FELSStrategy"),
    "cbae_strategy":          ("strategies.cbae_strategy",         "CBAEStrategy"),
    "sfr_structural_fakeout": ("strategies.sfr_structural_fakeout", "SFRStrategy"),
}

# ---------------------------------------------------------------------------
# Default test universes
# ---------------------------------------------------------------------------
DEFAULT_CRYPTO = [
    "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD",
    "AVAX/USD", "DOT/USD", "LINK/USD", "DOGE/USD",
    "BNB/USD", "XRP/USD",
]
DEFAULT_STOCKS = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA",
    "META", "AMZN", "GOOGL", "JPM", "SPY",
]
DEFAULT_TIMEFRAMES  = ["1h", "1d"]
DEFAULT_DIRECTIONS  = ["long", "short", "both"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pf_display(pf: float) -> str:
    """Compact PF display with visual indicator."""
    if pf <= 0:
        return "  --- "
    if pf >= 2.0:
        return f"**{pf:4.2f}**"   # double-star = strong
    if pf >= 1.2:
        return f" +{pf:4.2f} "    # plus = edge
    if pf >= 0.9:
        return f"  {pf:4.2f}  "   # neutral
    return f"  {pf:4.2f}  "       # losing


def cell_str(cell: Optional[dict]) -> str:
    """One-line cell for the terminal matrix table."""
    if cell is None:
        return "    --   "
    if cell.get("error"):
        return "   ERR   "
    trades = cell.get("trades", 0)
    if trades == 0:
        return "  (0t)   "
    pf  = cell.get("profit_factor", 0.0)
    wr  = cell.get("win_rate",      0.0)
    return f"{pf:5.2f}/{wr:4.0f}%"


def _avg(values: List[float]) -> float:
    vals = [v for v in values if v is not None and v > 0]
    return sum(vals) / len(vals) if vals else 0.0


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

def run_cell(bt, strategy_name: str, symbol: str, timeframe: str,
             direction: str, asset_class: str, days: int,
             param_overrides: dict) -> dict:
    """Run one backtest cell. Returns a dict with stats."""
    side_filter = direction if direction != "both" else "all"
    try:
        r = bt.run_strategy(
            symbol             = symbol,
            strategy_name      = strategy_name,
            days               = days,
            timeframe          = timeframe,
            asset_class        = asset_class,
            entry_side_filter  = side_filter,
            param_overrides    = param_overrides if param_overrides else None,
            skip_equity_curve  = True,
        )
        if r is None:
            return {"trades": 0, "error": "no_result"}
        return {
            "trades":        r.total_trades,
            "profit_factor": round(r.profit_factor if r.profit_factor < 900 else 0.0, 3),
            "win_rate":      round(r.win_rate,      2),
            "net_pnl":       round(r.ending_capital - r.starting_capital, 2),
            "sharpe":        round(getattr(r, "sharpe_ratio", 0.0) or 0.0, 3),
            "expectancy":    round(getattr(r, "expectancy",   0.0) or 0.0, 4),
            "error":         None,
        }
    except Exception as e:
        return {"trades": 0, "error": str(e)[:120]}


def run_matrix(bt, strategy_name: str,
               crypto_symbols: List[str],
               stock_symbols:  List[str],
               timeframes:     List[str],
               directions:     List[str],
               days:           int,
               param_overrides: dict) -> Tuple[dict, int, int]:
    """
    Run the full condition matrix.
    Returns (results_dict, total_cells, completed_cells).

    results_dict layout:
      results[symbol][tf][direction] = cell_dict
    """
    # Build full job list
    jobs = []
    for sym in crypto_symbols:
        for tf in timeframes:
            for d in directions:
                jobs.append((sym, tf, d, "crypto"))
    for sym in stock_symbols:
        for tf in timeframes:
            for d in directions:
                jobs.append((sym, tf, d, "stock"))

    total     = len(jobs)
    done      = 0
    cancelled = False
    results: Dict[str, Dict[str, Dict[str, dict]]] = defaultdict(
        lambda: defaultdict(dict)
    )

    def _sigint(sig, frame):
        nonlocal cancelled
        cancelled = True
        print("\n\nCtrl+C -- saving partial results...")

    signal.signal(signal.SIGINT, _sigint)

    t0 = time.time()
    for sym, tf, d, ac in jobs:
        if cancelled:
            break

        cell = run_cell(bt, strategy_name, sym, tf, d, ac, days, param_overrides)
        results[sym][tf][d] = cell
        done += 1

        elapsed  = time.time() - t0
        per_cell = elapsed / done
        eta      = (total - done) * per_cell
        bar_len  = 28
        filled   = int(bar_len * done / total)
        bar      = "#" * filled + "." * (bar_len - filled)
        pf_val   = cell.get("profit_factor", 0.0)
        trades   = cell.get("trades", 0)
        print(
            f"\r  [{bar}] {done}/{total}  "
            f"{sym:<14} {tf:<4} {d:<6}  "
            f"PF={pf_val:.2f} ({trades}t)  "
            f"ETA={eta/60:.1f}m   ",
            end="", flush=True,
        )

    print()
    return dict(results), total, done


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

EDGE_THRESHOLD = 1.20   # PF >= this = "has edge"
MIN_TRADES     = 2      # cells with fewer trades are flagged as low-confidence


def build_report(strategy_name: str,
                 results: dict,
                 crypto_symbols: List[str],
                 stock_symbols:  List[str],
                 timeframes: List[str],
                 directions: List[str],
                 days: int,
                 param_overrides: dict,
                 total_cells: int,
                 done_cells: int,
                 cancelled: bool,
                 started_at: str,
                 finished_at: str,
                 edge_threshold: float = EDGE_THRESHOLD,
                 min_trades: int = MIN_TRADES) -> dict:
    """Build the full structured report dict."""

    all_symbols = crypto_symbols + stock_symbols
    conditions  = [f"{tf}/{d}" for tf, d in product(timeframes, directions)]

    # ── Per-cell flat list ────────────────────────────────────────────────────
    cells = []
    for sym in all_symbols:
        ac = "crypto" if sym in crypto_symbols else "stock"
        for tf in timeframes:
            for d in directions:
                cell = results.get(sym, {}).get(tf, {}).get(d)
                cells.append({
                    "symbol":        sym,
                    "asset_class":   ac,
                    "timeframe":     tf,
                    "direction":     d,
                    "condition":     f"{tf}/{d}",
                    **(cell or {"trades": 0, "error": "not_run"}),
                })

    # ── Condition summary (average PF across all symbols) ────────────────────
    cond_stats = {}
    for tf, d in product(timeframes, directions):
        key = f"{tf}/{d}"
        pfs = []
        wrs = []
        n_edge = 0
        n_traded = 0
        for sym in all_symbols:
            c = results.get(sym, {}).get(tf, {}).get(d)
            if c and c.get("trades", 0) >= min_trades and not c.get("error"):
                pf = c.get("profit_factor", 0.0)
                pfs.append(pf)
                wrs.append(c.get("win_rate", 0.0))
                if pf >= edge_threshold:
                    n_edge += 1
                n_traded += 1
        cond_stats[key] = {
            "avg_pf":       round(_avg(pfs), 3),
            "avg_wr":       round(_avg(wrs), 2),
            "n_symbols":    n_traded,
            "n_edge":       n_edge,
            "edge_rate":    round(n_edge / n_traded * 100, 1) if n_traded else 0.0,
        }

    # ── Symbol summary (average PF across all conditions) ────────────────────
    sym_stats = {}
    for sym in all_symbols:
        pfs = []
        wrs = []
        n_edge = 0
        n_traded = 0
        for tf in timeframes:
            for d in directions:
                c = results.get(sym, {}).get(tf, {}).get(d)
                if c and c.get("trades", 0) >= min_trades and not c.get("error"):
                    pf = c.get("profit_factor", 0.0)
                    pfs.append(pf)
                    wrs.append(c.get("win_rate", 0.0))
                    if pf >= edge_threshold:
                        n_edge += 1
                    n_traded += 1
        sym_stats[sym] = {
            "avg_pf":       round(_avg(pfs), 3),
            "avg_wr":       round(_avg(wrs), 2),
            "n_conditions": n_traded,
            "n_edge":       n_edge,
            "edge_rate":    round(n_edge / n_traded * 100, 1) if n_traded else 0.0,
            "asset_class":  "crypto" if sym in crypto_symbols else "stock",
        }

    # ── Top edge cells ────────────────────────────────────────────────────────
    edge_cells = [
        c for c in cells
        if c.get("profit_factor", 0) >= edge_threshold
        and c.get("trades", 0) >= min_trades
        and not c.get("error")
    ]
    edge_cells.sort(key=lambda x: x.get("profit_factor", 0), reverse=True)

    # ── Overall numbers ───────────────────────────────────────────────────────
    all_pfs = [c.get("profit_factor", 0) for c in cells
               if c.get("trades", 0) >= min_trades and not c.get("error")]
    overall_avg_pf    = round(_avg(all_pfs), 3)
    overall_n_edge    = sum(1 for p in all_pfs if p >= edge_threshold)
    overall_edge_rate = round(overall_n_edge / len(all_pfs) * 100, 1) if all_pfs else 0.0

    # ── Best / worst conditions ───────────────────────────────────────────────
    ranked_conds = sorted(cond_stats.items(), key=lambda x: x[1]["avg_pf"], reverse=True)
    ranked_syms  = sorted(sym_stats.items(),  key=lambda x: x[1]["avg_pf"], reverse=True)

    return {
        "meta": {
            "strategy":       strategy_name,
            "days":           days,
            "param_overrides": param_overrides,
            "crypto_symbols": crypto_symbols,
            "stock_symbols":  stock_symbols,
            "timeframes":     timeframes,
            "directions":     directions,
            "total_cells":    total_cells,
            "done_cells":     done_cells,
            "cancelled":      cancelled,
            "started_at":     started_at,
            "finished_at":    finished_at,
            "edge_threshold": edge_threshold,
            "min_trades":     min_trades,
        },
        "summary": {
            "overall_avg_pf":    overall_avg_pf,
            "overall_edge_rate": overall_edge_rate,
            "overall_n_edge":    overall_n_edge,
            "total_valid_cells": len(all_pfs),
            "best_conditions":   [{"condition": k, **v} for k, v in ranked_conds[:5]],
            "worst_conditions":  [{"condition": k, **v} for k, v in ranked_conds[-5:]],
            "best_symbols":      [{"symbol": k, **v} for k, v in ranked_syms[:10]],
            "worst_symbols":     [{"symbol": k, **v} for k, v in ranked_syms[-10:]],
        },
        "condition_stats": cond_stats,
        "symbol_stats":    sym_stats,
        "edge_cells":      edge_cells[:50],   # top 50 edge cells
        "all_cells":       cells,
    }


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def print_matrix_table(report: dict):
    """Print the PF/WR% matrix table to stdout."""
    meta       = report["meta"]
    results_raw = {
        (c["symbol"], c["timeframe"], c["direction"]): c
        for c in report["all_cells"]
    }
    timeframes  = meta["timeframes"]
    directions  = meta["directions"]
    all_symbols = meta["crypto_symbols"] + meta["stock_symbols"]
    conditions  = [(tf, d) for tf in timeframes for d in directions]

    col_w   = 13
    name_w  = 16
    hdr_sep = "-" * (name_w + col_w * len(conditions) + 2)

    # Header
    print()
    _days_label = meta['days']
    _has_1h = any(tf in ("1h", "15m", "30m", "5m") for tf in meta.get('timeframes', []))
    if _has_1h and _days_label > 730:
        _days_label = f"{meta['days']}d (1h capped ~730d)"
    else:
        _days_label = f"{_days_label}d"
    print(f"  MATRIX: {meta['strategy']}  |  {_days_label}  |  "
          f"PF / WR%  (>={report['meta']['edge_threshold']:.1f} = edge)")
    print(hdr_sep)

    # Column headers
    col_hdrs = [f"{tf}/{d[:1]}"[:col_w].center(col_w) for tf, d in conditions]
    print(f"  {'Symbol':<{name_w}}" + "".join(col_hdrs))
    print(hdr_sep)

    # Crypto section
    if meta["crypto_symbols"]:
        print(f"  --- CRYPTO ---")
        for sym in meta["crypto_symbols"]:
            cells = [results_raw.get((sym, tf, d)) for tf, d in conditions]
            row   = f"  {sym:<{name_w}}"
            for c in cells:
                row += cell_str(c).center(col_w)
            print(row)

    # Stock section
    if meta["stock_symbols"]:
        print(f"  --- STOCKS ---")
        for sym in meta["stock_symbols"]:
            cells = [results_raw.get((sym, tf, d)) for tf, d in conditions]
            row   = f"  {sym:<{name_w}}"
            for c in cells:
                row += cell_str(c).center(col_w)
            print(row)

    print(hdr_sep)
    # Legend
    legend = "  ".join(f"{tf}/{d[:1]}" for tf, d in conditions)
    print(f"  Columns: {legend}")
    print(f"  Cell: PF/WR%   '--' = 0 trades   'ERR' = backtest error")
    print()


def write_text_report(report: dict, path: str):
    """Write the full human-readable .txt report."""
    meta    = report["meta"]
    summary = report["summary"]
    cstats  = report["condition_stats"]
    sstats  = report["symbol_stats"]
    cells   = report["all_cells"]
    results_raw = {
        (c["symbol"], c["timeframe"], c["direction"]): c
        for c in cells
    }

    lines = []
    W = 72

    def h1(t): lines.append("=" * W); lines.append(f"  {t}"); lines.append("=" * W)
    def h2(t): lines.append(f"\n  -- {t} --")
    def ln(t=""): lines.append(t)

    h1(f"STRATEGY MATRIX REPORT")
    ln(f"  Strategy   : {meta['strategy']}")
    ln(f"  Period     : {meta['days']} days")
    ln(f"  Timeframes : {', '.join(meta['timeframes'])}")
    ln(f"  Directions : {', '.join(meta['directions'])}")
    ln(f"  Crypto     : {', '.join(meta['crypto_symbols']) or 'none'}")
    ln(f"  Stocks     : {', '.join(meta['stock_symbols']) or 'none'}")
    if meta["param_overrides"]:
        ln(f"  Params     : {meta['param_overrides']}")
    ln(f"  Generated  : {meta['finished_at'][:19]}")
    if meta["cancelled"]:
        ln(f"  ** PARTIAL RUN -- cancelled at {meta['done_cells']}/{meta['total_cells']} cells **")
    ln()

    # ── Overall summary ───────────────────────────────────────────────────────
    h1("OVERALL SUMMARY")
    ln(f"  Total cells tested    : {summary['total_valid_cells']}")
    ln(f"  Overall avg PF        : {summary['overall_avg_pf']:.3f}")
    ln(f"  Cells with edge (PF>={meta['edge_threshold']:.1f}) : {summary['overall_n_edge']}  "
       f"({summary['overall_edge_rate']:.1f}%)")

    overall_verdict = (
        "STRONG EDGE -- profitable across many conditions"
        if summary["overall_edge_rate"] >= 50
        else "SELECTIVE EDGE -- works in specific conditions only"
        if summary["overall_edge_rate"] >= 20
        else "WEAK / NO EDGE -- few profitable conditions found"
    )
    ln(f"  Verdict               : {overall_verdict}")

    # ── Best/worst conditions ─────────────────────────────────────────────────
    h2("CONDITIONS RANKED BY AVERAGE PF (best first)")
    ln(f"  {'Condition':<12} {'Avg PF':>7} {'Avg WR%':>8} {'Symbols':>8} "
       f"{'Edge cnt':>9} {'Edge%':>7}")
    ln(f"  {'-'*12} {'-'*7} {'-'*8} {'-'*8} {'-'*9} {'-'*7}")
    all_conds = sorted(cstats.items(), key=lambda x: x[1]["avg_pf"], reverse=True)
    for cond, s in all_conds:
        flag = " <-- best" if cond == all_conds[0][0] else ""
        ln(f"  {cond:<12} {s['avg_pf']:>7.3f} {s['avg_wr']:>7.1f}% "
           f"{s['n_symbols']:>8} {s['n_edge']:>9} {s['edge_rate']:>6.0f}%{flag}")

    # ── Symbol ranking ────────────────────────────────────────────────────────
    h2("SYMBOLS RANKED BY AVERAGE PF (best first)")
    ln(f"  {'Symbol':<16} {'Class':<8} {'Avg PF':>7} {'Avg WR%':>8} "
       f"{'Conditions':>11} {'Edge cnt':>9} {'Edge%':>7}")
    ln(f"  {'-'*16} {'-'*8} {'-'*7} {'-'*8} {'-'*11} {'-'*9} {'-'*7}")
    all_syms = sorted(sstats.items(), key=lambda x: x[1]["avg_pf"], reverse=True)
    for sym, s in all_syms:
        ln(f"  {sym:<16} {s['asset_class']:<8} {s['avg_pf']:>7.3f} "
           f"{s['avg_wr']:>7.1f}% {s['n_conditions']:>11} {s['n_edge']:>9} "
           f"{s['edge_rate']:>6.0f}%")

    # ── Top edge cells ────────────────────────────────────────────────────────
    h2(f"TOP EDGE CELLS  (PF >= {meta['edge_threshold']:.1f}, >= {meta['min_trades']} trades)")
    edge = report["edge_cells"]
    if not edge:
        ln("  No cells met the edge threshold.")
    else:
        ln(f"  {'#':<4} {'Symbol':<16} {'TF':<5} {'Dir':<7} "
           f"{'PF':>6} {'WR%':>6} {'Trades':>7} {'PnL':>10}")
        ln(f"  {'-'*4} {'-'*16} {'-'*5} {'-'*7} {'-'*6} {'-'*6} {'-'*7} {'-'*10}")
        for i, c in enumerate(edge[:30], 1):
            ln(f"  {i:<4} {c['symbol']:<16} {c['timeframe']:<5} {c['direction']:<7} "
               f"{c.get('profit_factor',0):>6.3f} {c.get('win_rate',0):>5.1f}% "
               f"{c.get('trades',0):>7} {c.get('net_pnl',0):>10.2f}")

    # ── Full matrix table ─────────────────────────────────────────────────────
    h1("FULL MATRIX  (PF / WR%  per cell)")

    timeframes = meta["timeframes"]
    directions = meta["directions"]
    conditions = [(tf, d) for tf in timeframes for d in directions]
    all_symbols_list = meta["crypto_symbols"] + meta["stock_symbols"]
    col_w  = 13
    name_w = 16

    col_hdrs = [f"{tf}/{d[:1]}"[:col_w].center(col_w) for tf, d in conditions]
    ln(f"  {'Symbol':<{name_w}}" + "".join(col_hdrs))
    ln(f"  {'-'*name_w}" + "".join(["-"*col_w]*len(conditions)))

    if meta["crypto_symbols"]:
        ln(f"  -- CRYPTO --")
        for sym in meta["crypto_symbols"]:
            row = f"  {sym:<{name_w}}"
            for tf, d in conditions:
                c = results_raw.get((sym, tf, d))
                row += cell_str(c).center(col_w)
            ln(row)

    if meta["stock_symbols"]:
        ln(f"  -- STOCKS --")
        for sym in meta["stock_symbols"]:
            row = f"  {sym:<{name_w}}"
            for tf, d in conditions:
                c = results_raw.get((sym, tf, d))
                row += cell_str(c).center(col_w)
            ln(row)

    ln(f"\n  l=long  s=short  b=both  |  format: PF/WR%")

    # ── Cells with errors ─────────────────────────────────────────────────────
    err_cells = [c for c in cells if c.get("error") and c["error"] not in (None, "not_run")]
    if err_cells:
        h2("CELLS WITH ERRORS")
        for c in err_cells[:20]:
            ln(f"  {c['symbol']:<14} {c['timeframe']:<5} {c['direction']:<7}  {c['error']}")

    ln()
    ln("=" * W)
    ln("  END OF REPORT")
    ln("=" * W)

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def save_reports(report: dict, strategy_name: str, out_dir: str, label: str
                 ) -> Tuple[str, str, str]:
    """Save JSON, CSV, and TXT reports. Returns (json_path, csv_path, txt_path)."""
    os.makedirs(out_dir, exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = f"{strategy_name}_{ts}" + (f"_{label}" if label else "")

    json_path = os.path.join(out_dir, f"{slug}.json")
    csv_path  = os.path.join(out_dir, f"{slug}.csv")
    txt_path  = os.path.join(out_dir, f"{slug}.txt")

    # JSON
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    # CSV (flat cells)
    flat = report["all_cells"]
    if flat:
        fieldnames = list(flat[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(flat)

    # TXT
    write_text_report(report, txt_path)

    return json_path, csv_path, txt_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Strategy matrix -- exhaustive condition test across symbols x timeframes x directions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Discovery
    parser.add_argument("--list-strategies", action="store_true",
                        help="Print all available strategy names and exit.")

    # Target strategy
    parser.add_argument("--strategy", metavar="NAME",
                        help="Strategy to test (required).")

    # Optional param overrides  --param stop_loss_pct=1.5
    parser.add_argument("--param", metavar="name=value",
                        action="append", default=[], dest="params",
                        help="Override a single param value (repeatable). "
                             "Example: --param stop_loss_pct=1.2")

    # Symbol universes
    parser.add_argument("--symbols-crypto",
                        default=",".join(DEFAULT_CRYPTO),
                        metavar="SYM,...",
                        help=f"Crypto symbols (default: {len(DEFAULT_CRYPTO)} symbols)")
    parser.add_argument("--symbols-crypto-file", metavar="FILE",
                        help="Text file with one crypto symbol per line (overrides --symbols-crypto).")
    parser.add_argument("--symbols-stock",
                        default=",".join(DEFAULT_STOCKS),
                        metavar="SYM,...",
                        help=f"Stock symbols (default: {len(DEFAULT_STOCKS)} symbols)")
    parser.add_argument("--symbols-stock-file", metavar="FILE",
                        help="Text file with one stock symbol per line (overrides --symbols-stock).")
    parser.add_argument("--crypto-only", action="store_true",
                        help="Skip all stock symbols.")
    parser.add_argument("--stocks-only", action="store_true",
                        help="Skip all crypto symbols.")

    # Test dimensions
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES),
                        metavar="tf1,tf2,...",
                        help=f"Comma-separated timeframes (default: {','.join(DEFAULT_TIMEFRAMES)})")
    parser.add_argument("--directions", default=",".join(DEFAULT_DIRECTIONS),
                        metavar="long,short,both",
                        help="Comma-separated directions (default: long,short,both)")
    parser.add_argument("--days", type=int, default=365,
                        help="Days of history per backtest (default: 365)")

    # Output
    parser.add_argument("--output-dir", default="reports/strategy_matrix",
                        metavar="DIR",
                        help="Output directory (default: reports/strategy_matrix)")
    parser.add_argument("--label", default="", metavar="TEXT",
                        help="Optional label appended to report filenames.")
    parser.add_argument("--edge-threshold", type=float, default=EDGE_THRESHOLD,
                        metavar="FLOAT",
                        help=f"PF threshold for 'has edge' (default: {EDGE_THRESHOLD})")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES,
                        metavar="N",
                        help=f"Min trades for a cell to count in summary (default: {MIN_TRADES}). "
                             f"Use 1 or 2 for selective strategies that fire rarely.")

    args = parser.parse_args()

    # ── Discovery ─────────────────────────────────────────────────────────────
    if args.list_strategies:
        print("\nAvailable strategies:")
        print("-" * 36)
        for name in sorted(STRATEGY_MAP):
            print(f"  {name}")
        print()
        print("Use  --strategy <name>  to run the matrix.")
        print()
        sys.exit(0)

    if not args.strategy:
        parser.print_help()
        print("\nERROR: --strategy is required.")
        sys.exit(1)

    if args.strategy not in STRATEGY_MAP:
        print(f"\nUnknown strategy '{args.strategy}'. Run --list-strategies.")
        sys.exit(1)

    # ── Parse param overrides ─────────────────────────────────────────────────
    param_overrides = {}
    for spec in args.params:
        if "=" not in spec:
            print(f"Bad --param '{spec}'.  Expected name=value  e.g. --param stop_loss_pct=1.2")
            sys.exit(1)
        name, val = spec.split("=", 1)
        try:
            param_overrides[name.strip()] = float(val)
        except ValueError:
            param_overrides[name.strip()] = val

    # ── Build universes ───────────────────────────────────────────────────────
    def _load_symbols(file_arg, inline_arg):
        if file_arg:
            with open(file_arg, encoding="utf-8") as f:
                return [l.strip() for l in f
                        if l.strip() and not l.strip().startswith("#")]
        return [s.strip() for s in inline_arg.split(",") if s.strip()]

    crypto_syms = [] if args.stocks_only else \
        _load_symbols(args.symbols_crypto_file, args.symbols_crypto)
    stock_syms  = [] if args.crypto_only else \
        _load_symbols(args.symbols_stock_file, args.symbols_stock)

    timeframes  = [t.strip() for t in args.timeframes.split(",")  if t.strip()]
    directions  = [d.strip() for d in args.directions.split(",")  if d.strip()]

    total_cells = (len(crypto_syms) + len(stock_syms)) * len(timeframes) * len(directions)

    # ── Plan printout ─────────────────────────────────────────────────────────
    print(f"\n{'='*64}")
    print(f"  STRATEGY MATRIX  :  {args.strategy}")
    print(f"  Crypto symbols   :  {len(crypto_syms)}  ({', '.join(crypto_syms[:5])}{'...' if len(crypto_syms)>5 else ''})")
    print(f"  Stock  symbols   :  {len(stock_syms)}  ({', '.join(stock_syms[:5])}{'...' if len(stock_syms)>5 else ''})")
    print(f"  Timeframes       :  {', '.join(timeframes)}")
    print(f"  Directions       :  {', '.join(directions)}")
    print(f"  Days per test    :  {args.days}")
    print(f"  Total cells      :  {total_cells}")
    if param_overrides:
        print(f"  Param overrides  :  {param_overrides}")
    print(f"  Output dir       :  {args.output_dir}/")
    print(f"{'='*64}\n")

    # Rough ETA estimate: ~2s per cell typical
    rough_min = total_cells * 2 / 60
    print(f"  Estimated time   :  ~{rough_min:.0f} min  (varies with data cache)")
    print(f"  Ctrl+C any time to save partial results.\n")

    # ── Load backtester ───────────────────────────────────────────────────────
    print("Loading backtester...")
    try:
        from intelligence.backtester import Backtester
    except Exception as e:
        print(f"Import error: {e}")
        sys.exit(1)

    bt = Backtester()

    # ── Run ───────────────────────────────────────────────────────────────────
    started_at  = datetime.now().isoformat()
    cancelled   = False

    raw_results, total_done_total, done = run_matrix(
        bt, args.strategy, crypto_syms, stock_syms,
        timeframes, directions, args.days, param_overrides,
    )
    cancelled = done < total_cells

    finished_at = datetime.now().isoformat()

    edge_threshold = args.edge_threshold
    min_trades     = args.min_trades

    # ── Build & save report ───────────────────────────────────────────────────
    print("\nBuilding report...")
    report = build_report(
        strategy_name   = args.strategy,
        results         = raw_results,
        crypto_symbols  = crypto_syms,
        stock_symbols   = stock_syms,
        timeframes      = timeframes,
        directions      = directions,
        days            = args.days,
        param_overrides = param_overrides,
        total_cells     = total_cells,
        done_cells      = done,
        cancelled       = cancelled,
        started_at      = started_at,
        finished_at     = finished_at,
        edge_threshold  = edge_threshold,
        min_trades      = min_trades,
    )

    # Print matrix table to terminal
    print_matrix_table(report)

    # Print quick summary
    s = report["summary"]
    print(f"  Overall avg PF    : {s['overall_avg_pf']:.3f}")
    print(f"  Edge cells (PF>={edge_threshold:.1f}) : {s['overall_n_edge']} / "
          f"{s['total_valid_cells']} ({s['overall_edge_rate']:.1f}%)")
    if s["best_conditions"]:
        bc = s["best_conditions"][0]
        print(f"  Best condition    : {bc['condition']}  avg PF={bc['avg_pf']:.3f}  "
              f"edge_rate={bc['edge_rate']:.0f}%")
    if s["best_symbols"]:
        bs = s["best_symbols"][0]
        print(f"  Best symbol       : {bs['symbol']}  avg PF={bs['avg_pf']:.3f}")
    print()

    json_p, csv_p, txt_p = save_reports(
        report, args.strategy, args.output_dir, args.label
    )
    print(f"Reports saved:")
    print(f"  JSON : {json_p}")
    print(f"  CSV  : {csv_p}")
    print(f"  TXT  : {txt_p}")

    if cancelled:
        print("\n  ** Partial run -- re-run to complete. **")
    else:
        print(f"\nMatrix complete.  {done}/{total_cells} cells tested.")


if __name__ == "__main__":
    main()
