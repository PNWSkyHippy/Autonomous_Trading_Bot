"""
=============================================================
  BACKTESTING ENGINE
  Tests your strategy against years of historical data
  BEFORE risking real money. Uses yfinance for free
  historical stock data.

  Run from command line:
    python intelligence/backtester.py --symbol AAPL --days 365
    python intelligence/backtester.py --all --days 730
=============================================================
"""

import logging
import argparse
import os
import sys
import pandas as pd
import numpy as np
import sys

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)


from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    symbol:         str
    direction:      str
    entry_date:     str
    exit_date:      str
    entry_price:    float
    exit_price:     float
    quantity:       float
    position_value: float
    stop_loss:      float
    take_profit:    float
    pnl:            float
    pnl_pct:        float
    exit_reason:    str
    signal_score:   float
    bars_held:      int


@dataclass
class BacktestResult:
    symbol:           str
    start_date:       str
    end_date:         str
    starting_capital: float
    ending_capital:   float
    total_return_pct: float
    total_trades:     int
    winning_trades:   int
    losing_trades:    int
    win_rate:         float
    avg_win_pct:      float
    avg_loss_pct:     float
    profit_factor:    float
    max_drawdown_pct: float
    sharpe_ratio:     float
    largest_win:      float
    largest_loss:     float
    trades:           List[BacktestTrade] = field(default_factory=list)


class Backtester:
    """
    Simulates your exact trading strategy on historical data.
    Uses the same technical indicators and risk rules as the live bot.
    """

    def __init__(self, starting_capital: float = 100.0):
        self.starting_capital = starting_capital
        try:
            import yfinance as yf
            self.yf = yf
        except ImportError:
            raise ImportError("Install yfinance: pip install --upgrade yfinance")

        from scanners.market_scanner import TechnicalAnalysis
        self.ta = TechnicalAnalysis()

    def fetch_history(self, symbol: str,
                      days: int = 365) -> Optional[pd.DataFrame]:
        """Download historical OHLCV data from Yahoo Finance."""
        logger.info(f"Fetching {days} days of daily data for {symbol}...")
        try:
            end   = datetime.now()
            start = end - timedelta(days=days)

            ticker = self.yf.Ticker(symbol)
            df = ticker.history(
                start    = start.strftime("%Y-%m-%d"),
                end      = end.strftime("%Y-%m-%d"),
                interval = "1d",
                auto_adjust = True
            )

            if df is None or df.empty:
                logger.error(f"No data returned for {symbol}")
                return None

            df.columns = [c.lower() for c in df.columns]

            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            if len(df) < 30:
                logger.error(
                    f"Only {len(df)} bars for {symbol} — not enough data"
                )
                return None

            logger.info(f"Downloaded {len(df)} daily bars for {symbol}")
            return df

        except Exception as e:
            logger.error(f"Failed to fetch history for {symbol}: {e}")
            return None

    def _score_bar(self, df: pd.DataFrame,
                   idx: int) -> Tuple[str, float, Dict]:
        """
        Score a single bar for backtest entry signals.
        Uses relaxed thresholds suited to daily bars.
        """
        window = df.iloc[max(0, idx - 60):idx + 1]
        if len(window) < 10:
            return "none", 0.0, {}

        close  = window["close"]
        volume = window["volume"]

        # Pull periods from config tuning
        rsi_period = config.SIGNAL_TUNING.get("rsi_momentum_period", 14)
        ema_fast   = config.SIGNAL_TUNING.get("ema_fast_period", 9)
        ema_slow   = config.SIGNAL_TUNING.get("ema_slow_period", 21)
        ema_trend  = config.SIGNAL_TUNING.get("ema_trend_period", 50)
        bb_period  = config.SIGNAL_TUNING.get("bb_breakout_period", 20)
        bb_std     = config.SIGNAL_TUNING.get("bb_breakout_std", 2.0)

        rsi       = self.ta.rsi(close, rsi_period).iloc[-1]
        macd_df   = self.ta.macd(close)
        macd_hist = macd_df["hist"].iloc[-1]
        macd_prev = macd_df["hist"].iloc[-2] if len(macd_df) > 1 else 0
        ema_f     = self.ta.ema(close, ema_fast).iloc[-1]
        ema_s     = self.ta.ema(close, ema_slow).iloc[-1]
        ema_t     = self.ta.ema(close, ema_trend).iloc[-1]
        bb        = self.ta.bollinger_bands(close, bb_period, bb_std)
        bb_pct    = bb["pct_b"].iloc[-1]
        vol_ratio = self.ta.volume_ratio(volume).iloc[-1]

        long_score = short_score = 0.0

        # RSI — relaxed thresholds for daily bars
        if   rsi < 40:  long_score  += 2.0
        elif rsi < 45:  long_score  += 1.0
        elif rsi > 60:  short_score += 1.0
        elif rsi > 65:  short_score += 2.0

        # MACD crossover
        if   macd_hist > 0 and macd_prev < 0:  long_score  += 2.5
        elif macd_hist > 0 and macd_prev > 0:  long_score  += 1.0
        elif macd_hist < 0 and macd_prev > 0:  short_score += 2.5
        elif macd_hist < 0 and macd_prev < 0:  short_score += 1.0

        # EMA alignment
        if   ema_f > ema_s > ema_t:  long_score  += 2.0
        elif ema_f > ema_s:          long_score  += 1.0
        elif ema_f < ema_s < ema_t:  short_score += 2.0
        elif ema_f < ema_s:          short_score += 1.0

        # Bollinger Band position
        if   bb_pct < 0.15:  long_score  += 1.5
        elif bb_pct < 0.30:  long_score  += 0.75
        elif bb_pct > 0.85:  short_score += 1.5
        elif bb_pct > 0.70:  short_score += 0.75

        # Volume confirmation (relaxed to 1.2x for daily bars)
        if vol_ratio >= 1.2:
            long_score  += 1.0
            short_score += 1.0

        max_p   = 10.0
        long_n  = min(long_score  / max_p, 1.0)
        short_n = min(short_score / max_p, 1.0)

        # Relaxed minimum for backtest daily bars
        MIN_BACKTEST_CONFIDENCE = 0.40

        indicators = {
            "rsi":          round(rsi, 2),
            "macd_hist":    round(macd_hist, 4),
            "ema_fast":     round(ema_f, 4),
            "ema_slow":     round(ema_s, 4),
            "bb_pct":       round(bb_pct, 3),
            "volume_ratio": round(vol_ratio, 2)
        }

        if long_n >= short_n and long_n >= MIN_BACKTEST_CONFIDENCE:
            return "long",  round(long_n, 3),  indicators
        if short_n > long_n  and short_n >= MIN_BACKTEST_CONFIDENCE:
            return "short", round(short_n, 3), indicators
        return "none", 0.0, indicators

    def run(self, symbol: str, days: int = 365,
            starting_capital: float = None) -> Optional[BacktestResult]:
        """Run a full backtest for one symbol."""
        capital = starting_capital or self.starting_capital
        df = self.fetch_history(symbol, days)
        if df is None or len(df) < 20:
            logger.error(f"Insufficient data for {symbol} — only {len(df) if df is not None else 0} bars")
            return None

        trades             = []
        open_trade         = None
        equity_curve       = [capital]
        daily_pnl          = 0.0
        consecutive_losses = 0
        trading_halted     = False
        last_date          = None

        max_daily_loss_pct   = config.MAX_DAILY_LOSS_PCT / 100
        max_consec_losses    = config.MAX_CONSECUTIVE_LOSSES
        max_position_pct     = config.MAX_POSITION_PCT / 100
        stop_loss_pct        = config.DEFAULT_STOP_LOSS_PCT / 100
        take_profit_pct      = config.DEFAULT_TAKE_PROFIT_PCT / 100
        trailing_gap_pct     = config.TRAILING_STOP_PCT / 100

        logger.info(
            f"Backtesting {symbol} over {len(df)} bars ({days} days)..."
        )

        min_bars = min(60, max(20, len(df) // 3))
        for i in range(min_bars, len(df)):
            row          = df.iloc[i]
            current_date = str(df.index[i])[:10]

            # Reset daily tracking on new day
            if current_date != last_date:
                daily_pnl          = 0.0
                trading_halted     = False
                consecutive_losses = 0
                last_date          = current_date

            if trading_halted:
                # Still manage open trade even when halted
                if open_trade:
                    price       = row["close"]
                    exit_reason = self._check_exit(
                        open_trade, price, trailing_gap_pct, take_profit_pct
                    )
                    if exit_reason:
                        open_trade, pnl, trade = self._close_trade(
                            open_trade, price, exit_reason, i, df
                        )
                        trades.append(trade)
                        capital += pnl
                continue

            price = row["close"]

            # Manage open trade
            if open_trade:
                exit_reason = self._check_exit(
                    open_trade, price, trailing_gap_pct, take_profit_pct
                )
                if exit_reason:
                    open_trade, pnl, trade = self._close_trade(
                        open_trade, price, exit_reason, i, df
                    )
                    trades.append(trade)
                    capital      += pnl
                    daily_pnl    += pnl
                    equity_curve.append(capital)

                    won                = pnl > 0
                    consecutive_losses = 0 if won else consecutive_losses + 1

                    start_cap = capital - daily_pnl
                    if (start_cap > 0 and
                            daily_pnl / start_cap <= -max_daily_loss_pct):
                        trading_halted = True
                    if consecutive_losses >= max_consec_losses:
                        trading_halted = True
                    open_trade = None
                continue  # One trade at a time in backtest

            # Look for entry signal
            direction, score, indicators = self._score_bar(df, i)
            if direction == "none":
                continue

            position_value = capital * max_position_pct
            quantity       = position_value / price

            if direction == "long":
                sl = price * (1 - stop_loss_pct)
                tp = price * (1 + take_profit_pct)
            else:
                sl = price * (1 + stop_loss_pct)
                tp = price * (1 - take_profit_pct)

            open_trade = {
                "symbol":         symbol,
                "direction":      direction,
                "entry_idx":      i,
                "entry_date":     str(df.index[i]),
                "entry_price":    price,
                "quantity":       quantity,
                "position_value": position_value,
                "stop_loss":      sl,
                "take_profit":    tp,
                "signal_score":   score,
                "indicators":     indicators,
                "current_sl":     sl,
                "current_tp":     tp
            }

        # Force-close any remaining trade at end of backtest
        if open_trade:
            price = df.iloc[-1]["close"]
            _, pnl, trade = self._close_trade(
                open_trade, price, "backtest_end", len(df) - 1, df
            )
            trades.append(trade)
            capital += pnl
            equity_curve.append(capital)

        return self._compile_results(
            symbol, df, trades, equity_curve,
            self.starting_capital, capital
        )

    def _check_exit(self, trade: Dict, price: float,
                    trailing_gap_pct: float,
                    take_profit_pct: float) -> Optional[str]:
        d  = trade["direction"]
        sl = trade["current_sl"]
        tp = trade["current_tp"]

        if d == "long":
            if price <= sl: return "stop_loss"
            if price >= tp: return "take_profit"
            # Update trailing stop
            new_sl = price * (1 - trailing_gap_pct)
            if new_sl > sl:
                trade["current_sl"] = new_sl
            if price >= tp:
                trade["current_tp"] = price * (1 + take_profit_pct)
        else:
            if price >= sl: return "stop_loss"
            if price <= tp: return "take_profit"
        return None

    def _close_trade(self, trade: Dict, price: float,
                     reason: str, idx: int,
                     df: pd.DataFrame) -> Tuple[None, float, BacktestTrade]:
        d   = trade["direction"]
        qty = trade["quantity"]
        pnl = (
            (price - trade["entry_price"]) * qty if d == "long"
            else (trade["entry_price"] - price) * qty
        )
        pnl_pct = pnl / trade["position_value"] * 100 if trade["position_value"] else 0

        bt_trade = BacktestTrade(
            symbol         = trade["symbol"],
            direction      = d,
            entry_date     = trade["entry_date"],
            exit_date      = str(df.index[idx]),
            entry_price    = trade["entry_price"],
            exit_price     = price,
            quantity       = qty,
            position_value = trade["position_value"],
            stop_loss      = trade["stop_loss"],
            take_profit    = trade["take_profit"],
            pnl            = round(pnl, 4),
            pnl_pct        = round(pnl_pct, 2),
            exit_reason    = reason,
            signal_score   = trade["signal_score"],
            bars_held      = idx - trade["entry_idx"]
        )
        return None, pnl, bt_trade

    def _compile_results(self, symbol, df, trades, equity_curve,
                          start_cap, end_cap) -> BacktestResult:
        wins   = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]

        win_rate   = len(wins) / len(trades) * 100 if trades else 0
        avg_win    = np.mean([t.pnl_pct for t in wins])   if wins   else 0
        avg_loss   = np.mean([t.pnl_pct for t in losses]) if losses else 0
        gross_win  = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        pf         = gross_win / gross_loss if gross_loss else 999

        equity = pd.Series(equity_curve)
        peak   = equity.cummax()
        dd     = (equity - peak) / peak * 100
        max_dd = float(dd.min())

        returns = equity.pct_change().dropna()
        sharpe  = (
            (returns.mean() / returns.std() * np.sqrt(252))
            if returns.std() > 0 else 0
        )

        return BacktestResult(
            symbol           = symbol,
            start_date       = str(df.index[0])[:10],
            end_date         = str(df.index[-1])[:10],
            starting_capital = start_cap,
            ending_capital   = round(end_cap, 2),
            total_return_pct = round((end_cap - start_cap) / start_cap * 100, 2),
            total_trades     = len(trades),
            winning_trades   = len(wins),
            losing_trades    = len(losses),
            win_rate         = round(win_rate, 1),
            avg_win_pct      = round(avg_win, 2),
            avg_loss_pct     = round(avg_loss, 2),
            profit_factor    = round(pf, 2),
            max_drawdown_pct = round(max_dd, 2),
            sharpe_ratio     = round(float(sharpe), 2),
            largest_win      = round(max((t.pnl for t in wins),   default=0), 2),
            largest_loss     = round(min((t.pnl for t in losses), default=0), 2),
            trades           = trades
        )

    def print_report(self, result: BacktestResult):
        print("\n" + "=" * 60)
        print(f"  BACKTEST REPORT — {result.symbol}")
        print("=" * 60)
        print(f"  Period:           {result.start_date} -> {result.end_date}")
        print(f"  Starting Capital: ${result.starting_capital:,.2f}")
        print(f"  Ending Capital:   ${result.ending_capital:,.2f}")
        print(f"  Total Return:     {result.total_return_pct:+.2f}%")
        print("-" * 60)
        print(f"  Total Trades:     {result.total_trades}")
        print(f"  Wins / Losses:    {result.winning_trades}W / {result.losing_trades}L")
        print(f"  Win Rate:         {result.win_rate:.1f}%")
        print(f"  Avg Win:          +{result.avg_win_pct:.2f}%")
        print(f"  Avg Loss:         {result.avg_loss_pct:.2f}%")
        print(f"  Profit Factor:    {result.profit_factor:.2f}  (>1.5 is good)")
        print(f"  Max Drawdown:     {result.max_drawdown_pct:.2f}%")
        print(f"  Sharpe Ratio:     {result.sharpe_ratio:.2f}  (>1.0 is good)")
        print(f"  Largest Win:      ${result.largest_win:.2f}")
        print(f"  Largest Loss:     ${result.largest_loss:.2f}")
        print("=" * 60)

        go = result.win_rate >= 50 and result.profit_factor >= 1.2
        print(
            f"\n  "
            f"{'STRATEGY APPROVED — Safe to continue' if go else 'STRATEGY NEEDS TUNING — Stay on paper trading'}"
        )
        print("=" * 60 + "\n")


# --- Command line interface -------------------------------------------

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
        )
    except ImportError:
        pass

    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    parser = argparse.ArgumentParser(description="Backtest trading strategy")
    parser.add_argument("--symbol",  default="AAPL",
                        help="Stock symbol (default: AAPL)")
    parser.add_argument("--days",    type=int, default=365,
                        help="Days of history")
    parser.add_argument("--capital", type=float, default=100.0,
                        help="Starting capital")
    parser.add_argument("--all",     action="store_true",
                        help="Run on full watchlist")
    args = parser.parse_args()

    backtester = Backtester(starting_capital=args.capital)

    if args.all:
        symbols = config.STOCK_WATCHLIST[:10]
        results = []
        for sym in symbols:
            result = backtester.run(sym, args.days, args.capital)
            if result:
                backtester.print_report(result)
                results.append(result)

        if results:
            avg_wr  = np.mean([r.win_rate        for r in results])
            avg_pf  = np.mean([r.profit_factor   for r in results])
            avg_ret = np.mean([r.total_return_pct for r in results])
            print(f"\n{'='*60}")
            print(f"  PORTFOLIO SUMMARY ({len(results)} symbols)")
            print(f"  Avg Win Rate:      {avg_wr:.1f}%")
            print(f"  Avg Profit Factor: {avg_pf:.2f}")
            print(f"  Avg Return:        {avg_ret:+.2f}%")
            print(f"{'='*60}\n")
    else:
        result = backtester.run(args.symbol, args.days, args.capital)
        if result:
            backtester.print_report(result)
