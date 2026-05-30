# BACKTEST TEST MATRIX
**Strategy:** `adaptive_regime`  
**Engine:** `intelligence/backtester.py`  
**Run from:** repo root (`trading_bot_v2/`)

---

## Recommended Test Order

Run in this order — each layer builds on the last.

1. Single-symbol sanity (confirm the run works, check diagnostic output)
2. Broad all-stock / all-crypto (baseline numbers)
3. Long-only / short-only split (identify directional bias)
4. Trend-only / mean_rev-only split (isolate mode performance)
5. Stop architecture comparison (standard vs decoupled)
6. Fees on/off comparison (realistic vs optimistic)
7. Trade CSV review — check exit-bucket distribution

---

## A. Single-Symbol Sanity Tests

Quick checks to confirm the pipeline is healthy before running broad sweeps.
Use `--export-trades` so you can inspect the trade CSV if results look odd.

### Stock
```powershell
python intelligence/backtester.py --symbol NVDA --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --initial-stop signal_structural --trail-stop two_bar --export-trades
```

### Crypto
```powershell
python intelligence/backtester.py --symbol BTC/USD --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --initial-stop signal_structural --trail-stop two_bar --export-trades
```

**What to check:**
- Report prints without error
- `ADAPTIVE REGIME DIAGNOSTICS` section appears
- Regime bar distribution looks plausible (not 100% transitional)
- Trade count > 0 (if 0, check diagnostic blockers — not a code error)

---

## B. Broad All-Symbol Runs

Run the full watchlist. Exported trade CSV lands in `exports/` (one file per run,
combined CSV available when using `--all`).

### All stocks — 365 days
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --initial-stop signal_structural --trail-stop two_bar --export-trades
```

### All stocks — 730 days
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 730 --tf 1h --asset-class stock --initial-stop signal_structural --trail-stop two_bar --export-trades
```

### All crypto — 365 days
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --initial-stop signal_structural --trail-stop two_bar --export-trades
```

### All crypto — 730 days
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 730 --tf 1h --asset-class crypto --initial-stop signal_structural --trail-stop two_bar --export-trades
```

> **Note:** Stock and crypto must be evaluated separately. Their friction assumptions,
> price behavior, and regime distributions differ. Do not average them together.

---

## C. AdaptiveRegime Mode & Side Matrix

Run these after the broad baseline. Each narrows the lens to isolate one variable.

### Trend mode only — stock
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --initial-stop signal_structural --trail-stop two_bar --mode trend
```

### Mean-rev mode only — crypto
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --initial-stop signal_structural --trail-stop two_bar --mode mean_rev
```

### Long only — stock
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop two_bar --side long
```

### Short only — stock
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop two_bar --side short
```

### Long only — crypto
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --initial-stop signal_structural --trail-stop two_bar --side long
```

### Short only — crypto
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --initial-stop signal_structural --trail-stop two_bar --side short
```

> **Note:** Review adaptive_regime by mode and side — not just total PnL.
> A flat aggregate can hide a strong trend-long offset by a weak mean_rev-short.

---

## D. Stop Architecture Comparison

Compare the canonical decoupled profile against the plain two_bar stop.

### Canonical decoupled profile (ATR initial + two_bar trail)
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --initial-stop signal_structural --trail-stop two_bar --export-trades
```

### Plain two_bar stop (two_bar initial + two_bar trail)
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop two_bar --export-trades
```

### Standard fixed % stop
```powershell
python intelligence/backtester.py --all --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop standard --export-trades
```

---

## E. Fees On / Off Comparison

Run the same command twice to see how much friction costs.

### Realistic friction (default)
```powershell
python intelligence/backtester.py --symbol BTC/USD --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --initial-stop signal_structural --trail-stop two_bar
```

### Friction-free baseline
```powershell
python intelligence/backtester.py --symbol BTC/USD --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --initial-stop signal_structural --trail-stop two_bar --no-fees
```

> If `--no-fees` results are materially better, friction is a real drag — not a rounding issue.
> If they are roughly the same, the strategy edge (or lack of it) is the primary driver.

---

## Export Discipline

When `--export-trades` is used:
- Per-symbol CSV lands in `exports/` (created automatically if absent)
- On `--all` runs a combined CSV is written alongside the per-symbol files
- Use the CSV to audit exit-bucket distribution: `early_loss`, `adaptive_ema_cross_exit`,
  `adaptive_bb_midline_exit`, `adaptive_rsi_exit`, `stop_loss`, `take_profit`
- High `early_loss` count is a signal the position is going adverse immediately —
  check entry timing and stop distance, not exit logic

---

## Interpreting Results Honestly

| Situation | What it means |
|-----------|--------------|
| < 10 trades | Low sample — statistically meaningless. Check diagnostics for blockers. |
| PF = 999 | Tiny all-win sample, not a genuine edge. |
| Isolated symbol avg ≠ portfolio return | Per-symbol averages are not compounded returns. |
| `[DATA QUALITY FAIL]` in log | Symbol excluded due to corrupted yfinance data — do not hand-wave. |
| `mean_rev` barely fires | Check `MEAN-REV MODE` diagnostics: is `bb_touch` the top blocker? Is `range_bars` low? |
| Stock PF > crypto PF | Normal — evaluate each asset class separately. |
| Aggregate looks flat | Decompose by `--mode` and `--side` before concluding the strategy has no edge. |

---

## Quick Reference — Minimum Command Set

| Run | Command suffix |
|-----|---------------|
| Stock broad | `--all --asset-class stock --initial-stop signal_structural --trail-stop two_bar` |
| Crypto broad | `--all --asset-class crypto --initial-stop signal_structural --trail-stop two_bar` |
| Trend only | add `--mode trend` |
| Mean-rev only | add `--mode mean_rev` |
| Long only | add `--side long` |
| Short only | add `--side short` |
| No fees | add `--no-fees` |
| Export trades | add `--export-trades` |
