# BACKTEST COMMAND CHEATSHEET
**Project:** Trading Bot V2  
**Location:** `C:\Users\Linda\trading_bot_v2`  
**Use case:** Run `intelligence/backtester.py` directly from PowerShell with full CLI control.

---

## 1. Open PowerShell and activate the project

```powershell
cd C:\Users\Linda\trading_bot_v2
.\venv312\Scrpython intelligence/backtester.py --help
Current supported flags include:

--symbol
--days
--capital
--tf
--strategy
--all
--chart
--ascii
--export
--stop
--lookback
--asset-class
--no-fees
3. Important gotchas
Crypto symbols should be quoted
Use quotes around symbols with /:

--symbol "BTC/USD"
--symbol "ETH/USD"
--symbol "SOL/USD"
Current limitation of --all
As currently coded, --all uses:

symbols = config.STOCK_WATCHLIST[:15]
That means:

--all currently runs the first 15 stocks only
even if you pass --asset-class crypto
So do not trust --all --asset-class crypto unless that code has been patched.

4. Basic one-symbol commands
One stock, one strategy
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock
One crypto, one strategy
python intelligence/backtester.py --symbol "BTC/USD" --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto
Run generic/default scorer instead of a named strategy
python intelligence/backtester.py --symbol AAPL --days 180 --tf 1h --asset-class stock
5. Stop model comparisons
Standard stop
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop standard
Two-bar stop
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop two_bar --lookback 2
Compare the same crypto symbol with two-bar stop
python intelligence/backtester.py --symbol "BTC/USD" --strategy grid_bot --days 365 --tf 1h --asset-class crypto --stop two_bar --lookback 2
6. Fees vs no-fees comparisons
Realistic friction model
python intelligence/backtester.py --symbol "BTC/USD" --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto
Friction-free baseline
python intelligence/backtester.py --symbol "BTC/USD" --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --no-fees
Stock with and without fees
python intelligence/backtester.py --symbol AAPL --strategy scalp_master --days 180 --tf 5m --asset-class stock
python intelligence/backtester.py --symbol AAPL --strategy scalp_master --days 180 --tf 5m --asset-class stock --no-fees
7. Export results
Export JSON
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --export
Export plus ASCII chart
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --export --ascii
Export with matplotlib chart
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --chart
8. Save output to text files
Save one run to a report file
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock *> reports\adaptive_AAPL_1h.txt
Append multiple runs into one file
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock *>> reports\adaptive_batch.txt
python intelligence/backtester.py --symbol MSFT --strategy adaptive_regime --days 365 --tf 1h --asset-class stock *>> reports\adaptive_batch.txt
python intelligence/backtester.py --symbol NVDA --strategy adaptive_regime --days 365 --tf 1h --asset-class stock *>> reports\adaptive_batch.txt
Save crypto run
python intelligence/backtester.py --symbol "BTC/USD" --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto *> reports\adaptive_BTC_1h.txt
9. Strategy-specific quick commands
Adaptive Regime — 1h stock
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop standard
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop two_bar --lookback 2
Grid Bot — 1h crypto
python intelligence/backtester.py --symbol "ETH/USD" --strategy grid_bot --days 365 --tf 1h --asset-class crypto --stop two_bar --lookback 2
DCA / DipBuy style — 1h stock
python intelligence/backtester.py --symbol MSFT --strategy dca_accumulator --days 365 --tf 1h --asset-class stock --stop two_bar --lookback 2
Scalp Master — 5m stock
python intelligence/backtester.py --symbol NVDA --strategy scalp_master --days 30 --tf 5m --asset-class stock --stop two_bar --lookback 2
Hammer Reversal — 5m crypto
python intelligence/backtester.py --symbol "SOL/USD" --strategy hammer_reversal --days 30 --tf 5m --asset-class crypto --stop two_bar --lookback 2
ORB Breakout — 5m stock
python intelligence/backtester.py --symbol TSLA --strategy orb_breakout --days 30 --tf 5m --asset-class stock --stop standard
10. Recommended test matrix for a new strategy
When validating a new strategy, run all of these:

A. One stock, 1h, standard stop
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop standard
B. Same stock, 1h, two-bar stop
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 365 --tf 1h --asset-class stock --stop two_bar --lookback 2
C. One crypto, 1h, realistic fees
python intelligence/backtester.py --symbol "BTC/USD" --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --stop two_bar --lookback 2
D. Same crypto, no-fees baseline
python intelligence/backtester.py --symbol "BTC/USD" --strategy adaptive_regime --days 365 --tf 1h --asset-class crypto --stop two_bar --lookback 2 --no-fees
E. Shorter timeframe stress test
python intelligence/backtester.py --symbol AAPL --strategy adaptive_regime --days 30 --tf 5m --asset-class stock --stop two_bar --lookback 2
Use this matrix to answer:

does the strategy only work on one timeframe?
does profit disappear once fees are added?
does standard stop look better only because it is unrealistic?
does two-bar materially improve expectancy?
11. Recommended validation workflow
Fast manual workflow
Run one symbol
Run same symbol with --stop standard
Run same symbol with --stop two_bar
Run same symbol with and without --no-fees
Save outputs to reports\
Compare results side by side
Better workflow
Test:

one stock winner
one stock loser
one crypto major
one crypto high-vol
5m
1h
standard
two_bar
fees
no-fees
That gives a much more honest picture than one giant batch run.

ipts\activate