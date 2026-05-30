"""
Comprehensive crypto backtest — all pairs in watchlists/crypto.txt
Strategies: bollinger_breakout, mean_reversion, scalp_master, dca_accumulator
Timeframe: 1h / 730 days (yfinance max)
"""

import os
import sys
import logging
import numpy as np
from datetime import datetime

# ── path setup ───────────────────────────────────────────────────────────────
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_root, ".env"))
except ImportError:
    pass

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

from intelligence.backtester import Backtester, BacktestResult, print_portfolio_summary

# ── config ────────────────────────────────────────────────────────────────────
WATCHLIST_PATH = os.path.join(_root, "watchlists", "crypto.txt")
STRATEGIES     = ["bollinger_breakout", "mean_reversion", "scalp_master", "dca_accumulator"]
DAYS           = 365
TIMEFRAME      = "1h"
CAPITAL        = 10_000.0

# ── load pairs ────────────────────────────────────────────────────────────────
def load_crypto_pairs(path: str):
    pairs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # BTC/USD → BTC-USD (yfinance format)
            pairs.append(line.replace("/", "-"))
    return list(dict.fromkeys(pairs))  # deduplicate, preserve order

# ── summary helpers ───────────────────────────────────────────────────────────
def strategy_summary(strategy: str, results: list[BacktestResult]):
    if not results:
        print(f"\n  [No results for {strategy}]\n")
        return
    print(f"\n{'='*74}")
    print(f"  STRATEGY: {strategy.upper()}  ({len(results)} pairs)")
    print(f"  {'Symbol':<12} {'Return':>8} {'WinRate':>8} {'PF':>6} "
          f"{'MaxDD':>8} {'Sharpe':>7} {'Trades':>7}")
    print(f"  {'-'*63}")
    for r in sorted(results, key=lambda x: x.total_return_pct, reverse=True):
        flag = " <-- BEST" if r == sorted(results, key=lambda x: x.total_return_pct, reverse=True)[0] else ""
        print(f"  {r.symbol:<12} {r.total_return_pct:>+7.1f}% "
              f"{r.win_rate:>7.1f}% {r.profit_factor:>6.2f} "
              f"{r.max_drawdown_pct:>7.1f}% {r.sharpe_ratio:>7.2f} "
              f"{r.total_trades:>7}{flag}")
    avgs = {
        "ret": np.mean([r.total_return_pct for r in results]),
        "wr":  np.mean([r.win_rate         for r in results]),
        "pf":  np.mean([r.profit_factor    for r in results]),
        "dd":  np.mean([r.max_drawdown_pct for r in results]),
        "sh":  np.mean([r.sharpe_ratio     for r in results]),
        "tr":  sum(r.total_trades          for r in results),
    }
    print(f"  {'-'*63}")
    print(f"  {'AVERAGES':<12} {avgs['ret']:>+7.1f}% "
          f"{avgs['wr']:>7.1f}% {avgs['pf']:>6.2f} "
          f"{avgs['dd']:>7.1f}% {avgs['sh']:>7.2f} "
          f"{int(avgs['tr']):>7}")
    print(f"{'='*74}")


def cross_strategy_summary(all_results: dict[str, list[BacktestResult]]):
    print(f"\n\n{'#'*74}")
    print(f"  CROSS-STRATEGY MASTER SUMMARY — {DAYS}d / {TIMEFRAME}")
    print(f"  Run: {datetime.now().strftime('%Y-%m-%d %H:%M PT')}")
    print(f"{'#'*74}")

    print(f"\n  {'Strategy':<22} {'Pairs':>5} {'Trades':>7} {'AvgRet':>8} "
          f"{'WinRate':>8} {'AvgPF':>7} {'AvgDD':>8} {'AvgSharpe':>10}")
    print(f"  {'-'*76}")

    for strat, results in all_results.items():
        if not results:
            print(f"  {strat:<22} {'0':>5} {'—':>7}")
            continue
        print(f"  {strat:<22} "
              f"{len(results):>5} "
              f"{sum(r.total_trades for r in results):>7} "
              f"{np.mean([r.total_return_pct for r in results]):>+7.1f}% "
              f"{np.mean([r.win_rate for r in results]):>7.1f}% "
              f"{np.mean([r.profit_factor for r in results]):>7.2f} "
              f"{np.mean([r.max_drawdown_pct for r in results]):>7.1f}% "
              f"{np.mean([r.sharpe_ratio for r in results]):>9.2f}")

    # Best pair per strategy
    print(f"\n  BEST PAIR PER STRATEGY:")
    for strat, results in all_results.items():
        if not results:
            continue
        best = max(results, key=lambda r: r.total_return_pct)
        worst = min(results, key=lambda r: r.total_return_pct)
        print(f"  {strat:<22}  Best: {best.symbol:<12} {best.total_return_pct:>+6.1f}%  "
              f"Worst: {worst.symbol:<12} {worst.total_return_pct:>+6.1f}%")

    # Top 10 pairs overall (by avg return across strategies)
    pair_scores: dict[str, list] = {}
    for strat, results in all_results.items():
        for r in results:
            pair_scores.setdefault(r.symbol, []).append(r.total_return_pct)
    avg_by_pair = {sym: np.mean(rets) for sym, rets in pair_scores.items()}
    top10 = sorted(avg_by_pair.items(), key=lambda x: x[1], reverse=True)[:10]
    bot10 = sorted(avg_by_pair.items(), key=lambda x: x[1])[:10]

    print(f"\n  TOP 10 PAIRS (avg return across all strategies):")
    for sym, avg in top10:
        print(f"    {sym:<14} {avg:>+7.1f}%")

    print(f"\n  BOTTOM 10 PAIRS (avg return across all strategies):")
    for sym, avg in bot10:
        print(f"    {sym:<14} {avg:>+7.1f}%")

    print(f"\n{'#'*74}\n")


# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    pairs = load_crypto_pairs(WATCHLIST_PATH)
    print(f"\nLoaded {len(pairs)} crypto pairs from {WATCHLIST_PATH}")
    print(f"Strategies : {', '.join(STRATEGIES)}")
    print(f"Timeframe  : {TIMEFRAME}  |  Days: {DAYS}  |  Capital: ${CAPITAL:,.0f}")
    print(f"Started    : {datetime.now().strftime('%Y-%m-%d %H:%M PT')}\n")

    bt = Backtester(starting_capital=CAPITAL)
    all_results: dict[str, list[BacktestResult]] = {}

    for strat in STRATEGIES:
        print(f"\n--- Running {strat} on {len(pairs)} pairs ---")
        results = bt.run_all(
            symbols          = pairs,
            strategy_name    = strat,
            days             = DAYS,
            starting_capital = CAPITAL,
            timeframe        = TIMEFRAME,
        )
        all_results[strat] = results
        strategy_summary(strat, results)

    cross_strategy_summary(all_results)
