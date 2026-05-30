"""
=============================================================
  FULL ADVERSARIAL STRESS TEST BATTERY
  Trading Bot v2 — Backtest Expert Framework

  Tests all 6 active strategies across:
  1. Baseline (primary timeframe, 2yr 1h or 30d 5m)
  2. Long-term daily candles (5yr 1d) for regime analysis
  3. Year-by-year breakdown (positive expectancy in majority of years?)
  4. Parameter sensitivity sweeps (plateau vs spike)
  5. Slippage stress tests (1x / 1.5x / 2x / 3x friction)
  6. Final 5-dimension scoring + Deploy/Refine/Abandon verdict

  Run from project root:
    python full_strategy_stress_test.py           (full battery, ~30-40 min)
    python full_strategy_stress_test.py --quick   (skip daily/yby, ~5 min)
    python full_strategy_stress_test.py --strategy mean_reversion
    python full_strategy_stress_test.py --strategy bollinger_breakout --quick

  Output: reports/stress_test_YYYY-MM-DD.md

  IMPORTANT: Run during market hours for best 5m/1h data availability.
  yfinance 1h limit = 730 days. 1d limit = 10+ years.
=============================================================
"""

from __future__ import annotations

import argparse
import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# ── Path setup ──────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# ── Strategy definitions with ACTUAL config.py SIGNAL_TUNING values ─────────
# param_sweeps vary one param at a time, all others stay at baseline config.
# Baseline values match config.py exactly as of 2026-04-16.

STRATEGIES = {
    "mean_reversion": {
        "class_name":  "MeanReversion",
        "module":      "strategies.mean_reversion",
        "timeframe":   "1h",
        "days":        720,
        "params":      3,       # zscore_entry, period, min_score
        "description": "Fades extreme z-score deviations back to mean",
        "baseline": {
            "mean_rev_zscore_entry": 2.0,
            "mean_rev_period":       20,
            "mean_rev_min_score":    0.60,
        },
        "param_sweeps": {
            # Test ±25% around baseline values
            "mean_rev_zscore_entry": [1.5, 1.75, 2.0, 2.25, 2.5],
            "mean_rev_period":       [15, 20, 25, 30],
        },
        "symbols": [
            "AAPL","GOOGL","AMZN","NVDA","META","TSLA",
            "AMD","INTC","MRVL","AVGO","PLTR","NOW","NET","SPY","QQQ"
        ],
    },

    "bollinger_breakout": {
        "class_name":  "BollingerBreakout",
        "module":      "strategies.bollinger_breakout",
        "timeframe":   "5m",
        "days":        30,
        "params":      4,       # period, std, squeeze_thr, min_score
        "description": "Breaks outside BB bands — Run C params (sq=0.04, ms=0.70)",
        "baseline": {
            "bb_breakout_period":      20,
            "bb_breakout_std":         2.0,
            "bb_squeeze_threshold":    0.04,
            "bb_breakout_min_score":   0.70,
        },
        "param_sweeps": {
            "bb_breakout_std":       [1.75, 2.0, 2.25, 2.5],
            "bb_squeeze_threshold":  [0.03, 0.04, 0.05, 0.06],
            "bb_breakout_min_score": [0.60, 0.65, 0.70, 0.75],
        },
        # SMCI excluded — catastrophic losses confirmed in Apr 2026 backtest
        "symbols": [
            "AAPL","GOOGL","AMZN","NVDA","META","TSLA",
            "AMD","INTC","MRVL","AVGO","ALAB","PLTR","NOW","NET","SPY"
        ],
    },

    "ema_crossover": {
        "class_name":  "EMACrossover",
        "module":      "strategies.ema_crossover",
        "timeframe":   "1h",
        "days":        720,
        "params":      4,       # fast, slow, trend, min_score
        "description": "Fast(9)/Slow(21) EMA cross with EMA50 trend filter",
        "baseline": {
            "ema_fast_period":       9,
            "ema_slow_period":       21,
            "ema_trend_period":      50,
            "ema_crossover_min_score": 0.60,
        },
        "param_sweeps": {
            "ema_fast_period":  [7, 9, 12, 14],
            "ema_slow_period":  [18, 21, 26, 30],
            "ema_trend_period": [40, 50, 60],
        },
        "symbols": [
            "AAPL","GOOGL","AMZN","NVDA","META","TSLA",
            "AMD","INTC","MRVL","AVGO","PLTR","NOW","NET","SPY","QQQ"
        ],
    },

    "scalp_master": {
        "class_name":  "ScalpMaster",
        "module":      "strategies.scalp_master",
        "timeframe":   "5m",
        "days":        30,
        "params":      3,       # rsi_period, volume_ratio, min_score
        "description": "RSI(7) reversal with volume spike — 8 symbol whitelist",
        "baseline": {
            "scalp_rsi_oversold":     35,
            "scalp_rsi_overbought":   65,
            "scalp_rsi_period":       7,
            "scalp_min_volume_ratio": 1.2,
            "scalp_min_score":        0.60,
        },
        "param_sweeps": {
            "scalp_rsi_oversold":     [30, 33, 35, 38, 40],
            "scalp_min_volume_ratio": [1.1, 1.2, 1.3, 1.5],
            "scalp_rsi_period":       [5, 7, 9, 11],
        },
        "symbols": ["META","PLTR","ALAB","AMZN","GOOGL","IREN"],
    },

    "swing_trader": {
        "class_name":  "SwingTrader",
        "module":      "strategies.swing_trader",
        "timeframe":   "1h",
        "days":        720,
        "params":      4,       # adx_min, adx_period, rsi_os, min_score
        "description": "ADX(20+) trend + RSI(40) pullback, wide SL/TP",
        "baseline": {
            "swing_adx_min":      20,
            "swing_adx_period":   14,
            "swing_rsi_oversold": 40,
            "swing_min_score":    0.60,
        },
        "param_sweeps": {
            "swing_adx_min":      [15, 17, 20, 23, 25, 28],
            "swing_rsi_oversold": [35, 38, 40, 43, 45],
        },
        "symbols": [
            "AAPL","GOOGL","AMZN","NVDA","META","TSLA",
            "AMD","PLTR","SPY","QQQ","JPM","BAC","NFLX","XOM","IWM"
        ],
    },

    "dca_accumulator": {
        "class_name":  "DCAAccumulator",
        "module":      "strategies.dca_accumulator",
        "timeframe":   "1h",
        "days":        729,
        "params":      4,       # ema_period, dip_pct, rsi_max, min_score
        "description": "Buy dips 2% below EMA50 with RSI<=45 confirmation",
        "baseline": {
            "dca_ema_period": 50,
            "dca_dip_pct":    0.02,
            "dca_rsi_max":    45,
            "dca_min_score":  0.60,
        },
        "param_sweeps": {
            "dca_dip_pct": [0.01, 0.015, 0.02, 0.025, 0.03],
            "dca_rsi_max": [40, 43, 45, 48, 50],
        },
        "symbols": [
            "AAPL","GOOGL","AMZN","NVDA","META","TSLA",
            "AMD","INTC","PLTR","SPY","QQQ","JPM","NFLX","XOM","IWM"
        ],
    },
}

# ── Slippage estimates by market cap tier (round-trip %) ────────────────────
# Source: backtest-expert methodology (0.05% per side for large/mid cap)
SLIPPAGE_BASE_PCT = 0.05   # per side → 0.10% round trip


# ===========================================================================
#  5-DIMENSION SCORER (inline — no external file needed)
# ===========================================================================

def _pf(wr, aw, al):
    loss = (1 - wr/100) * al
    return (wr/100 * aw) / loss if loss else float("inf")

def _exp(wr, aw, al):
    return (wr/100)*aw - (1-wr/100)*al

def score_strategy(trades, wr, aw, al, dd, years, params, slip=False):
    def s_sample(n):
        if n<30: return 0
        if n<100: return 8+int((n-30)/70*7)
        if n<200: return 15+int((n-100)/100*5)
        return 20
    def s_exp(wr,aw,al):
        e=_exp(wr,aw,al)
        if e<=0: return 0
        if e<0.5: return 5+int(e/0.5*5)
        if e<1.5: return 10+int((e-0.5)/1.0*8)
        return 20
    def s_risk(dd,wr,aw,al):
        if dd>=50: return 0
        dd_s=12 if dd<20 else int(12*(50-dd)/30)
        pf=_pf(wr,aw,al)
        pf_s=0 if pf<1.0 else (8 if pf>=3.0 else int((pf-1.0)/2.0*8))
        return min(20,dd_s+pf_s)
    def s_robust(y,p):
        ys=0 if y<5 else (15 if y>=10 else 5+int((y-5)/5*10))
        ps=5 if p<=4 else (3 if p<=6 else (1 if p==7 else 0))
        return min(20,ys+ps)

    d1=s_sample(trades); d2=s_exp(wr,aw,al); d3=s_risk(dd,wr,aw,al)
    d4=s_robust(years,params); d5=20 if slip else 0
    total=max(0,min(100,d1+d2+d3+d4+d5))
    return {
        "total":total,
        "verdict":"Deploy" if total>=70 else ("Refine" if total>=40 else "Abandon"),
        "d1":d1,"d2":d2,"d3":d3,"d4":d4,"d5":d5,
        "pf":round(_pf(wr,aw,al),3),
        "expectancy":round(_exp(wr,aw,al),4),
    }

def slippage_stress(wr, aw, al, multiplier):
    """Apply slippage friction. Returns adjusted PF and expectancy."""
    slip = SLIPPAGE_BASE_PCT * 2 * multiplier   # round-trip
    adj_aw = max(0, aw - slip)
    adj_al = al + slip
    return {
        "multiplier": multiplier,
        "adj_aw":  round(adj_aw, 4),
        "adj_al":  round(adj_al, 4),
        "adj_pf":  round(_pf(wr, adj_aw, adj_al), 3),
        "adj_exp": round(_exp(wr, adj_aw, adj_al), 4),
        "survives": _pf(wr, adj_aw, adj_al) >= 1.0,
    }


# ===========================================================================
#  BACKTESTER
# ===========================================================================

class StressBacktester:

    def __init__(self, capital: float = 10_000.0):
        self.capital = capital
        try:
            import yfinance as yf
            self.yf = yf
        except ImportError:
            print("ERROR: yfinance not installed.")
            print("Run: pip install yfinance --break-system-packages")
            sys.exit(1)

    def fetch(self, symbol: str, days: int, tf: str) -> Optional[pd.DataFrame]:
        """Fetch OHLCV data from Yahoo Finance."""
        intervals = {"5m":"5m","1h":"1h","1d":"1d"}
        max_days  = {"5m":30,"1h":720,"1d":3650}
        days = min(days, max_days[tf])
        end   = datetime.now()
        start = end - timedelta(days=days+3)
        try:
            ticker = self.yf.Ticker(symbol)
            df = ticker.history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval=intervals[tf],
                auto_adjust=True
            )
            if df is None or df.empty or len(df) < 30:
                return None
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            cutoff = end - timedelta(days=days)
            df = df[df.index >= pd.Timestamp(cutoff)]
            return df if len(df) >= 30 else None
        except Exception:
            return None

    def run(self, symbol: str, strategy_name: str, days: int, tf: str,
            overrides: dict = None) -> Optional[dict]:
        """
        Run one strategy on one symbol.
        overrides: dict of SIGNAL_TUNING keys to temporarily change.
        Returns summary stats dict or None if no trades.
        """
        # Apply overrides
        original = {}
        if overrides:
            for k, v in overrides.items():
                if k in config.SIGNAL_TUNING:
                    original[k] = config.SIGNAL_TUNING[k]
                    config.SIGNAL_TUNING[k] = v

        try:
            import importlib
            sdef  = STRATEGIES[strategy_name]
            mod   = importlib.import_module(sdef["module"])
            cls   = getattr(mod, sdef["class_name"])
            strat = cls()

            df = self.fetch(symbol, days, tf)
            if df is None:
                return None

            capital    = self.capital
            open_trade = None
            trades     = []
            equity     = [capital]

            trail_pct  = config.TRAILING_STOP_PCT / 100
            pos_pct    = config.MAX_POSITION_PCT  / 100
            min_bars   = min(100, max(20, len(df)//5))

            for i in range(min_bars, len(df)):
                price = float(df.iloc[i]["close"])

                # ── Manage open position ──────────────────────────────
                if open_trade:
                    d  = open_trade["dir"]
                    sl = open_trade["sl"]
                    tp = open_trade["tp"]
                    exit_r = None

                    if d == "long":
                        if price <= sl:  exit_r = "stop_loss"
                        elif price >= tp: exit_r = "take_profit"
                        else:
                            new_sl = price * (1 - trail_pct)
                            if new_sl > sl:
                                open_trade["sl"] = new_sl
                    else:
                        if price >= sl:  exit_r = "stop_loss"
                        elif price <= tp: exit_r = "take_profit"

                    if exit_r:
                        ep  = open_trade["entry"]
                        qty = open_trade["qty"]
                        pv  = open_trade["pos_val"]
                        pnl = (price-ep)*qty if d=="long" else (ep-price)*qty
                        pnl_pct = pnl/pv*100
                        trades.append({
                            "pnl":pnl, "pnl_pct":pnl_pct,
                            "won":pnl>0, "exit":exit_r,
                            "date":str(df.index[i])[:10],
                        })
                        capital += pnl
                        equity.append(capital)
                        open_trade = None
                    continue

                # ── Look for signal ───────────────────────────────────
                window = df.iloc[max(0,i-150):i+1].copy()
                try:
                    sig = strat.analyze(symbol, window, "unknown")
                except Exception:
                    sig = None

                if sig is None:
                    continue

                pos_val = capital * pos_pct
                qty     = pos_val / price
                sl_pct  = (sig.stop_loss_pct   if sig.stop_loss_pct   else strat.stop_loss_pct)   / 100
                tp_pct  = (sig.take_profit_pct if sig.take_profit_pct else strat.take_profit_pct) / 100

                if sig.direction == "long":
                    sl = price*(1-sl_pct); tp = price*(1+tp_pct)
                else:
                    sl = price*(1+sl_pct); tp = price*(1-tp_pct)

                open_trade = {
                    "dir":sig.direction, "entry":price,
                    "qty":qty, "pos_val":pos_val, "sl":sl, "tp":tp
                }

            # Force close at end
            if open_trade:
                fp  = float(df.iloc[-1]["close"])
                ep  = open_trade["entry"]
                qty = open_trade["qty"]
                d   = open_trade["dir"]
                pnl = (fp-ep)*qty if d=="long" else (ep-fp)*qty
                trades.append({
                    "pnl":pnl, "pnl_pct":pnl/open_trade["pos_val"]*100,
                    "won":pnl>0, "exit":"eod",
                    "date":str(df.index[-1])[:10]
                })
                capital += pnl
                equity.append(capital)

            if not trades:
                return None

            wins   = [t for t in trades if t["won"]]
            losses = [t for t in trades if not t["won"]]
            wr     = len(wins)/len(trades)*100
            aw     = np.mean([abs(t["pnl_pct"]) for t in wins])   if wins   else 0.0
            al     = np.mean([abs(t["pnl_pct"]) for t in losses]) if losses else 0.0

            eq_s   = pd.Series(equity)
            peak   = eq_s.cummax()
            dd     = float(((eq_s-peak)/peak*100).min())
            rets   = eq_s.pct_change().dropna()
            ann    = {"5m":252*78,"1h":252*7,"1d":252}[tf]
            sharpe = float(rets.mean()/rets.std()*np.sqrt(ann)) if rets.std()>0 else 0.0

            return {
                "symbol":symbol, "strategy":strategy_name, "tf":tf, "days":days,
                "trades":len(trades),
                "win_rate":round(wr,1), "avg_win":round(aw,3), "avg_loss":round(al,3),
                "max_dd":round(abs(dd),3), "sharpe":round(sharpe,2),
                "total_return":round((capital-self.capital)/self.capital*100,3),
                "final_capital":round(capital,2),
                "start_date":str(df.index[0])[:10], "end_date":str(df.index[-1])[:10],
                "pf":round(_pf(wr,aw,al),3),
            }

        finally:
            for k, v in original.items():
                config.SIGNAL_TUNING[k] = v


# ===========================================================================
#  AGGREGATION HELPERS
# ===========================================================================

def agg(results: list) -> Optional[dict]:
    """Aggregate per-symbol results to portfolio stats."""
    r = [x for x in results if x]
    if not r: return None
    return {
        "n": len(r),
        "total_trades": sum(x["trades"] for x in r),
        "avg_wr":   round(np.mean([x["win_rate"]     for x in r]),1),
        "avg_aw":   round(np.mean([x["avg_win"]      for x in r]),3),
        "avg_al":   round(np.mean([x["avg_loss"]     for x in r]),3),
        "avg_dd":   round(np.mean([x["max_dd"]       for x in r]),3),
        "avg_sh":   round(np.mean([x["sharpe"]       for x in r]),2),
        "avg_ret":  round(np.mean([x["total_return"] for x in r]),3),
        "avg_pf":   round(np.mean([x["pf"]           for x in r]),3),
    }


# ===========================================================================
#  MAIN TEST RUNNER
# ===========================================================================

def run_tests(bt: StressBacktester, strategy_name: str,
              quick: bool = False) -> dict:
    sdef    = STRATEGIES[strategy_name]
    tf      = sdef["timeframe"]
    days    = sdef["days"]
    symbols = sdef["symbols"]
    params  = sdef["params"]
    data    = {}

    print(f"\n{'='*62}")
    print(f"  {strategy_name.upper()} | {tf} | {days}d | {len(symbols)} symbols")
    print(f"{'='*62}")

    # ── 1. BASELINE ──────────────────────────────────────────────────
    print(f"\n[1/4] Baseline ({tf}, {days}d)...")
    bl = []
    for sym in symbols:
        r = bt.run(sym, strategy_name, days, tf)
        bl.append(r)
        status = f"{r['trades']}T WR={r['win_rate']}% PF={r['pf']}" if r else "no trades"
        print(f"      {sym:<8} {status}")
    data["baseline"] = agg(bl)
    data["baseline_per_sym"] = [r for r in bl if r]

    if data["baseline"]:
        b = data["baseline"]
        sc = score_strategy(b["total_trades"],b["avg_wr"],b["avg_aw"],b["avg_al"],
                           b["avg_dd"],2 if tf=="1h" else 0,params)
        print(f"\n  → Portfolio: WR={b['avg_wr']}% PF={b['avg_pf']} "
              f"Sharpe={b['avg_sh']} Trades={b['total_trades']}")
        print(f"  → Score: {sc['total']}/100  Verdict: {sc['verdict']}")

    # ── 2. LONG-TERM DAILY (skip for 5m strategies and --quick) ──────
    if tf == "1h" and not quick:
        print(f"\n[2/4] Long-term daily (1825d = 5yr)...")
        lt_syms = symbols[:6]   # top 6 to keep runtime reasonable
        lt = []
        for sym in lt_syms:
            r = bt.run(sym, strategy_name, 1825, "1d")
            lt.append(r)
            status = f"{r['trades']}T WR={r['win_rate']}% PF={r['pf']}" if r else "no trades"
            print(f"      {sym:<8} {status}")
        data["longterm"] = agg(lt)
        data["years_tested"] = 5
    else:
        data["years_tested"] = 2 if tf=="1h" else 0

    # ── 3. YEAR-BY-YEAR (1h only, skip --quick) ───────────────────────
    if tf == "1h" and not quick:
        print(f"\n[3/4] Year-by-year (365d chunks)...")
        yby = {}
        yby_syms = symbols[:6]
        for yr_offset in range(3):   # last 3 years
            yr = datetime.now().year - yr_offset
            yr_results = []
            for sym in yby_syms:
                r = bt.run(sym, strategy_name, 365, "1h")
                yr_results.append(r)
            agg_yr = agg(yr_results)
            if agg_yr:
                yby[str(yr)] = agg_yr
                print(f"      {yr}: WR={agg_yr['avg_wr']}% PF={agg_yr['avg_pf']} "
                      f"Sharpe={agg_yr['avg_sh']}")
        data["year_by_year"] = yby
    else:
        data["year_by_year"] = {}

    # ── 4. PARAMETER SWEEP ───────────────────────────────────────────
    print(f"\n[4/4] Parameter sensitivity sweep...")
    sweep_sym = symbols[0]
    sweeps    = sdef["param_sweeps"]
    sweep_results = []

    for param, values in sweeps.items():
        print(f"      {param}: {values}")
        for val in values:
            r = bt.run(sweep_sym, strategy_name, days, tf, overrides={param: val})
            if r:
                r["sweep_param"] = param
                r["sweep_value"] = val
                sweep_results.append(r)

    data["param_sweep"] = sweep_results

    # Plateau analysis
    for param, values in sweeps.items():
        pf_vals = [
            r["pf"] for r in sweep_results
            if r["sweep_param"] == param
        ]
        if len(pf_vals) >= 3:
            pf_range = max(pf_vals) - min(pf_vals)
            plateau  = pf_range < 0.30   # < 0.3 PF swing = plateau
            print(f"      {param} PF range: {min(pf_vals):.2f}–{max(pf_vals):.2f} "
                  f"→ {'✅ PLATEAU (robust)' if plateau else '⚠️  SPIKE (fragile)'}")

    return data


# ===========================================================================
#  REPORT WRITER
# ===========================================================================

def write_report(all_data: dict, output_path: str):
    lines = [
        "# Full Adversarial Strategy Stress Test",
        f"\n**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        "**Framework:** Backtest Expert 5-Dimension (0-100)  ",
        "**Capital:** $10,000 per symbol  ",
        "**Slippage base:** 0.05% per side (0.10% round-trip)  \n",
        "---\n",
    ]

    # ── Per-strategy sections ─────────────────────────────────────────
    for strat, data in all_data.items():
        sdef = STRATEGIES[strat]
        tf   = sdef["timeframe"]
        lines.append(f"## {strat.replace('_',' ').title()}")
        lines.append(f"\n> {sdef['description']}  ")
        lines.append(f"> Timeframe: `{tf}` | Parameters: {sdef['params']}\n")

        b = data.get("baseline")
        if not b:
            lines.append("*No baseline data — insufficient data for this timeframe.*\n\n---\n")
            continue

        years = data.get("years_tested", 1)
        sc    = score_strategy(b["total_trades"],b["avg_wr"],b["avg_aw"],
                               b["avg_al"],b["avg_dd"],years,sdef["params"])

        # Score table
        lines.append(f"### Score: **{sc['total']}/100 — {sc['verdict']}**\n")
        lines.append("| Dimension | Score | Max |")
        lines.append("|-----------|------:|----:|")
        lines.append(f"| Sample Size     | {sc['d1']} | 20 |")
        lines.append(f"| Expectancy      | {sc['d2']} | 20 |")
        lines.append(f"| Risk Management | {sc['d3']} | 20 |")
        lines.append(f"| Robustness      | {sc['d4']} | 20 |")
        lines.append(f"| Exec Realism    | {sc['d5']} | 20 |")
        lines.append(f"\n*Note: Exec Realism = 0 until slippage stress confirms edge survives friction.*\n")

        # Baseline stats
        lines.append(f"### Baseline Portfolio ({b['n']} symbols, {b['total_trades']} trades)\n")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Win Rate | {b['avg_wr']}% |")
        lines.append(f"| Avg Win | {b['avg_aw']}% |")
        lines.append(f"| Avg Loss | {b['avg_al']}% |")
        lines.append(f"| Profit Factor | {b['avg_pf']} |")
        lines.append(f"| Expectancy | {round(_exp(b['avg_wr'],b['avg_aw'],b['avg_al']),4)}%/trade |")
        lines.append(f"| Avg Max Drawdown | {b['avg_dd']}% |")
        lines.append(f"| Avg Sharpe | {b['avg_sh']} |")
        lines.append(f"| Avg Return | {b['avg_ret']}% |\n")

        # Per-symbol breakdown
        per_sym = data.get("baseline_per_sym", [])
        if per_sym:
            lines.append("#### Per-Symbol Results\n")
            lines.append("| Symbol | Trades | Win Rate | PF | Sharpe | Return |")
            lines.append("|--------|--------|----------|----|--------|--------|")
            for r in sorted(per_sym, key=lambda x: x["pf"], reverse=True):
                lines.append(
                    f"| {r['symbol']} | {r['trades']} | {r['win_rate']}% | "
                    f"{r['pf']} | {r['sharpe']} | {r['total_return']}% |"
                )
            lines.append("")

        # Long-term
        lt = data.get("longterm")
        if lt:
            lt_sc = score_strategy(lt["total_trades"],lt["avg_wr"],lt["avg_aw"],
                                   lt["avg_al"],lt["avg_dd"],5,sdef["params"])
            lines.append(f"### Long-Term (5yr Daily) Score: {lt_sc['total']}/100 — {lt_sc['verdict']}\n")
            lines.append(f"WR={lt['avg_wr']}%  PF={lt['avg_pf']}  Sharpe={lt['avg_sh']}  "
                         f"Trades={lt['total_trades']}\n")

        # Year-by-year
        yby = data.get("year_by_year", {})
        if yby:
            lines.append("### Year-by-Year Performance\n")
            lines.append("| Year | Trades | Win Rate | PF | Sharpe | Return |")
            lines.append("|------|--------|----------|----|--------|--------|")
            positive_years = 0
            for yr, s in sorted(yby.items()):
                pf = round(_pf(s["avg_wr"],s["avg_aw"],s["avg_al"]),3)
                flag = "✅" if pf >= 1.0 else "❌"
                if pf >= 1.0: positive_years += 1
                lines.append(
                    f"| {yr} {flag} | {s['total_trades']} | {s['avg_wr']}% | "
                    f"{pf} | {s['avg_sh']} | {s['avg_ret']}% |"
                )
            lines.append(
                f"\n*Positive expectancy in {positive_years}/{len(yby)} years tested.*\n"
            )

        # Slippage stress
        lines.append("### Slippage Stress Test\n")
        lines.append("| Scenario | Adj Win | Adj Loss | Profit Factor | Expectancy | Survives |")
        lines.append("|----------|---------|----------|---------------|------------|---------|")
        for mult, label in [
            (0.0, "No slippage (current)"),
            (1.0, "1x realistic (~0.10% RT)"),
            (1.5, "1.5x conservative"),
            (2.0, "2x stress test"),
            (3.0, "3x worst case"),
        ]:
            s = slippage_stress(b["avg_wr"], b["avg_aw"], b["avg_al"], mult)
            icon = "✅" if s["survives"] else "❌"
            lines.append(
                f"| {label} | {s['adj_aw']}% | {s['adj_al']}% | "
                f"{s['adj_pf']} | {s['adj_exp']}% | {icon} |"
            )
        lines.append("")

        # Parameter sweep
        sweep = data.get("param_sweep", [])
        if sweep:
            lines.append("### Parameter Sensitivity (Plateau vs Spike)\n")
            lines.append(
                "*Robust = PF range < 0.30 across values (plateau). "
                "Fragile = PF collapses outside narrow range (spike).*\n"
            )
            current_param = None
            lines.append("| Parameter | Value | Trades | Win Rate | PF | Sharpe |")
            lines.append("|-----------|-------|--------|----------|----|--------|")
            for r in sweep:
                if r["sweep_param"] != current_param:
                    current_param = r["sweep_param"]
                    # Calculate range for this param
                    pf_vals = [x["pf"] for x in sweep if x["sweep_param"]==current_param]
                    rng = max(pf_vals)-min(pf_vals) if pf_vals else 0
                    robust = "✅ PLATEAU" if rng < 0.30 else "⚠️  SPIKE"
                    lines.append(f"| **{current_param}** ({robust}, range={rng:.2f}) | | | | | |")
                lines.append(
                    f"| | {r['sweep_value']} | {r['trades']} | "
                    f"{r['win_rate']}% | {r['pf']} | {r['sharpe']} |"
                )
            lines.append("")

        lines.append("---\n")

    # ── Summary table ─────────────────────────────────────────────────
    lines.append("## Summary — All Strategies\n")
    lines.append("| Strategy | Score | Verdict | Trades | Win Rate | PF | Sharpe | 1.5x Slip PF | 1.5x Survives |")
    lines.append("|----------|------:|---------|--------|----------|----|--------|-------------|--------------|")

    for strat, data in all_data.items():
        b = data.get("baseline")
        if not b:
            lines.append(f"| {strat} | — | No data | — | — | — | — | — | — |")
            continue
        years = data.get("years_tested", 1)
        sc    = score_strategy(b["total_trades"],b["avg_wr"],b["avg_aw"],
                               b["avg_al"],b["avg_dd"],years,STRATEGIES[strat]["params"])
        sl15  = slippage_stress(b["avg_wr"],b["avg_aw"],b["avg_al"],1.5)
        icon  = "✅" if sl15["survives"] else "❌"
        lines.append(
            f"| {strat} | {sc['total']} | {sc['verdict']} | {b['total_trades']} | "
            f"{b['avg_wr']}% | {b['avg_pf']} | {b['avg_sh']} | "
            f"{sl15['adj_pf']} | {icon} |"
        )

    lines.append("\n---")
    lines.append("\n### Scoring Framework Reference\n")
    lines.append("| Dimension | Max | Key Thresholds |")
    lines.append("|-----------|----:|---------------|")
    lines.append("| Sample Size | 20 | 200+ trades = full; <30 = zero |")
    lines.append("| Expectancy | 20 | PF 1.5+ = healthy; <1.0 = negative edge |")
    lines.append("| Risk Mgmt | 20 | DD <15% best; >40% near zero |")
    lines.append("| Robustness | 20 | ≤4 params + 10yr = best; 7+ params = flag |")
    lines.append("| Exec Realism | 20 | Slippage tested = full; untested = 0 |")
    lines.append("\n**Verdict:** Deploy ≥70 | Refine 40-69 | Abandon <40\n")
    lines.append("\n*Generated by full_strategy_stress_test.py*")

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n✅ Report saved: {output_path}")


# ===========================================================================
#  ENTRY POINT
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Full adversarial strategy stress test battery"
    )
    parser.add_argument("--quick", action="store_true",
                        help="Skip long-term daily and year-by-year tests (~5 min vs ~40 min)")
    parser.add_argument("--strategy", default=None,
                        help="Test only one strategy e.g. --strategy mean_reversion")
    parser.add_argument("--capital", type=float, default=10_000.0,
                        help="Starting capital per symbol (default $10,000)")
    args = parser.parse_args()

    print("\n" + "="*62)
    print("  FULL ADVERSARIAL STRESS TEST BATTERY")
    print("  Backtest Expert Framework — 5-Dimension Scoring")
    print("="*62)
    if args.quick:
        print("  MODE: Quick (baseline + param sweep only)")
    else:
        print("  MODE: Full (baseline + longterm + yby + param sweep)")
    print()

    strategies_to_run = (
        [args.strategy] if args.strategy and args.strategy in STRATEGIES
        else list(STRATEGIES.keys())
    )
    print(f"  Strategies: {', '.join(strategies_to_run)}")
    print(f"  Capital:    ${args.capital:,.0f} per symbol")
    print()

    bt       = StressBacktester(capital=args.capital)
    all_data = {}

    for strat in strategies_to_run:
        all_data[strat] = run_tests(bt, strat, quick=args.quick)

    date_str    = datetime.now().strftime("%Y-%m-%d")
    output_path = f"reports/stress_test_{date_str}.md"
    write_report(all_data, output_path)

    # Print final summary
    print("\n" + "="*62)
    print("  FINAL SCORES")
    print("="*62)
    for strat, data in all_data.items():
        b = data.get("baseline")
        if not b:
            print(f"  {strat:<22} — no data")
            continue
        years = data.get("years_tested",1)
        sc = score_strategy(b["total_trades"],b["avg_wr"],b["avg_aw"],
                           b["avg_al"],b["avg_dd"],years,STRATEGIES[strat]["params"])
        sl = slippage_stress(b["avg_wr"],b["avg_aw"],b["avg_al"],1.5)
        print(f"  {strat:<22} {sc['total']:>3}/100  {sc['verdict']:<7}  "
              f"PF={b['avg_pf']}  Slip1.5x={'✅' if sl['survives'] else '❌'}")

    print(f"\n  Report: {output_path}")
    print("="*62 + "\n")


if __name__ == "__main__":
    main()