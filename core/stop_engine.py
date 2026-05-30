"""
=============================================================
  STOP ENGINE  —  Two-Bar Structural Trailing Stop
  Single source of truth for stop logic across all environments.

  Used by:
    risk_manager.calculate_trailing_stop()   live / paper trailing
    trade_executor.execute_signal()          live / paper initial stop
    market_scanner (structural_stop_price embedded in signal)
    backtester._check_exit_two_bar()         (uses its own TwoBarStop;
                                              this module handles live side)

  Algorithm (mirrors Pine Script, no lookahead):
    Initial stop  (long):  lowest  low  of prior N closed bars
    Initial stop  (short): highest high of prior N closed bars
    Trail trigger (long):  new closed bar's high > prior-N highest high
    Trail trigger (short): new closed bar's low  < prior-N lowest  low
    New stop      (long):  min(prior-N lows)  — only if > current stop
    New stop      (short): max(prior-N highs) — only if < current stop
=============================================================
"""

import logging
from typing import Tuple

import pandas as pd

logger = logging.getLogger(__name__)


class TwoBarStopEngine:
    """Two-bar structural trailing stop, shared across live, paper, and backtest."""

    def __init__(self, lookback: int = 2):
        self.lookback = max(1, min(20, lookback))

    # ------------------------------------------------------------------
    # Initial stop
    # ------------------------------------------------------------------

    def initial_stop(self, df: pd.DataFrame, entry_idx: int,
                     direction: str) -> float:
        """
        Backtest entry: stop from df[entry_idx-lookback : entry_idx].
        No lookahead — the entry bar itself is excluded.
        """
        start  = max(0, entry_idx - self.lookback)
        window = df.iloc[start:entry_idx]
        return self._stop_from_window(window, direction, float(df.iloc[entry_idx]["close"]))

    def initial_stop_from_tail(self, bars: pd.DataFrame,
                               direction: str) -> float:
        """
        Live/paper entry: bars = all recently fetched CLOSED bars.
        Stop uses the N bars immediately before the last bar (the signal bar).

        Side-validation: if the computed stop is on the wrong side of the
        signal-bar close (e.g. all prior lows are above entry after a sharp
        gap-down long entry), fall back to a 2% percent-based stop so callers
        always receive a logically valid value rather than passing a bad stop
        all the way to the executor.
        """
        end       = len(bars) - 1          # exclude the signal bar
        start     = max(0, end - self.lookback)
        window    = bars.iloc[start:end]
        entry     = float(bars.iloc[-1]["close"])
        raw_stop  = self._stop_from_window(window, direction, entry)

        # Validate side: stop must protect the position, not imprison it.
        if direction == "long" and raw_stop >= entry:
            import logging as _l
            _l.getLogger(__name__).debug(
                "initial_stop_from_tail: long stop %.6f >= entry %.6f — "
                "using 2%% fallback", raw_stop, entry
            )
            return round(entry * 0.98, 6)
        if direction == "short" and raw_stop <= entry:
            import logging as _l
            _l.getLogger(__name__).debug(
                "initial_stop_from_tail: short stop %.6f <= entry %.6f — "
                "using 2%% fallback", raw_stop, entry
            )
            return round(entry * 1.02, 6)

        return raw_stop

    def _stop_from_window(self, window: pd.DataFrame,
                          direction: str, fallback_price: float) -> float:
        if window.empty:
            return fallback_price * 0.98 if direction == "long" else fallback_price * 1.02
        if direction == "long":
            return float(window["low"].min())
        return float(window["high"].max())

    # ------------------------------------------------------------------
    # Trail update — call once per newly closed bar while in position
    # ------------------------------------------------------------------

    def check_for_trail_update(self, bars: pd.DataFrame,
                               current_stop: float,
                               direction: str) -> Tuple[bool, float]:
        """
        bars: at least lookback+1 closed bars; LAST row = the newly closed bar.

        Returns (triggered, new_stop).
        new_stop == current_stop when no improvement (caller should check triggered).
        """
        if len(bars) < self.lookback + 1:
            return False, current_stop

        trigger_bar  = bars.iloc[-1]
        prior_window = bars.iloc[-(self.lookback + 1):-1]

        if direction == "long":
            prior_highest = float(prior_window["high"].max())
            if float(trigger_bar["high"]) > prior_highest:
                candidate = float(prior_window["low"].min())
                if candidate > current_stop:
                    return True, candidate
        else:
            prior_lowest = float(prior_window["low"].min())
            if float(trigger_bar["low"]) < prior_lowest:
                candidate = float(prior_window["high"].max())
                if candidate < current_stop:
                    return True, candidate

        return False, current_stop

    # ------------------------------------------------------------------
    # Stop hit checks
    # ------------------------------------------------------------------

    def is_stop_hit_intrabar(self, df: pd.DataFrame, bar_idx: int,
                             stop_price: float, direction: str) -> bool:
        """Backtest: intrabar low/high touched stop price."""
        bar = df.iloc[bar_idx]
        if direction == "long":
            return float(bar["low"]) <= stop_price
        return float(bar["high"]) >= stop_price

    def is_stop_hit_price(self, current_price: float,
                          stop_price: float, direction: str) -> bool:
        """Live/paper: current tick price crossed stop."""
        if direction == "long":
            return current_price <= stop_price
        return current_price >= stop_price


# Module-level singleton — import this from other modules
stop_engine = TwoBarStopEngine(lookback=2)
