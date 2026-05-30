"""
chart_patterns.py
=================
Geometric chart pattern detection for BreakoutScanner and Trading_Bot_V2.

Patterns detected:
  - Ascending Triangle      (rising lows, flat resistance)
  - Descending Triangle     (falling highs, flat support)
  - Symmetrical Triangle    (converging highs and lows)
  - Bull Flag               (sharp rally + tight downward channel)
  - Bear Flag               (sharp drop + tight upward channel)
  - Consolidation Breakout  (tight range then expansion)
  - Parabolic Acceleration  (momentum second derivative rising)
  - Volume Climax           (volume spike at key price level)

Usage:
    from chart_patterns import PatternDetector, PatternResult

    detector = PatternDetector()
    results  = detector.detect(prices, volumes)   # lists of floats
    for r in results:
        print(r.name, r.confidence, r.direction, r.description)
"""

from dataclasses import dataclass
from typing import List, Optional
import math


@dataclass
class PatternResult:
    name:        str        # pattern name
    confidence:  float      # 0.0 – 1.0
    direction:   str        # "LONG", "SHORT", "NEUTRAL"
    description: str        # human-readable summary
    score:       float      # raw score before normalisation


class PatternDetector:
    """
    Stateless detector — pass price and volume history each call.
    Minimum 20 candles required for most patterns.
    """

    MIN_CANDLES = 20

    def detect(self, prices: List[float], volumes: List[float]) -> List[PatternResult]:
        """Run all detectors and return a list of PatternResult (may be empty)."""
        if len(prices) < self.MIN_CANDLES:
            return []

        results = []
        for fn in [
            self._ascending_triangle,
            self._descending_triangle,
            self._symmetrical_triangle,
            self._bull_flag,
            self._bear_flag,
            self._consolidation_breakout,
            self._parabolic_acceleration,
            self._volume_climax,
        ]:
            try:
                r = fn(prices, volumes)
                if r and r.confidence >= 0.40:
                    results.append(r)
            except Exception:
                continue

        results.sort(key=lambda x: x.confidence, reverse=True)
        return results

    def best(self, prices: List[float], volumes: List[float]) -> Optional[PatternResult]:
        """Return the single highest-confidence pattern, or None."""
        results = self.detect(prices, volumes)
        return results[0] if results else None

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _linreg_slope(values: List[float]) -> float:
        """Slope of a linear regression through values (normalised by mean)."""
        n   = len(values)
        if n < 2: return 0.0
        xs  = list(range(n))
        mx  = sum(xs) / n
        my  = sum(values) / n
        num = sum((xs[i]-mx)*(values[i]-my) for i in range(n))
        den = sum((xs[i]-mx)**2 for i in range(n))
        if den == 0: return 0.0
        slope = num / den
        return slope / (my if my != 0 else 1.0)   # normalise by mean

    @staticmethod
    def _range_pct(values: List[float]) -> float:
        mn, mx = min(values), max(values)
        if mn == 0: return 0.0
        return (mx - mn) / mn * 100

    @staticmethod
    def _pivot_highs(prices: List[float], window: int = 3) -> List[float]:
        highs = []
        for i in range(window, len(prices)-window):
            if prices[i] == max(prices[i-window:i+window+1]):
                highs.append(prices[i])
        return highs

    @staticmethod
    def _pivot_lows(prices: List[float], window: int = 3) -> List[float]:
        lows = []
        for i in range(window, len(prices)-window):
            if prices[i] == min(prices[i-window:i+window+1]):
                lows.append(prices[i])
        return lows

    # ── Pattern implementations ──────────────────────────────────────────

    def _ascending_triangle(self, prices, volumes) -> Optional[PatternResult]:
        """
        Rising lows (positive slope) + flat/slightly rising highs.
        Bullish — breakout expected upward.
        """
        highs = self._pivot_highs(prices)
        lows  = self._pivot_lows(prices)
        if len(highs) < 2 or len(lows) < 2:
            return None

        high_slope = self._linreg_slope(highs)
        low_slope  = self._linreg_slope(lows)

        # Highs should be flat (|slope| < 0.005) or gently rising
        # Lows should be clearly rising
        flat_highs  = abs(high_slope) < 0.008
        rising_lows = low_slope > 0.003

        if not (flat_highs and rising_lows):
            return None

        # Convergence: range shrinking
        range_early = self._range_pct(prices[:len(prices)//2])
        range_late  = self._range_pct(prices[len(prices)//2:])
        converging  = range_late < range_early * 0.75

        # Volume: should be declining during formation
        vol_slope = self._linreg_slope(volumes)
        vol_declining = vol_slope < 0

        score = 0.0
        score += 0.35 if rising_lows else 0
        score += 0.25 if flat_highs  else 0
        score += 0.25 if converging  else 0
        score += 0.15 if vol_declining else 0

        return PatternResult(
            name        = "Ascending Triangle",
            confidence  = min(score, 1.0),
            direction   = "LONG",
            description = f"Rising lows (slope={low_slope:.4f}) + flat resistance. "
                          f"Converging={converging}. Breakout likely upward.",
            score       = score,
        )

    def _descending_triangle(self, prices, volumes) -> Optional[PatternResult]:
        """
        Falling highs + flat/slightly falling support. Bearish.
        """
        highs = self._pivot_highs(prices)
        lows  = self._pivot_lows(prices)
        if len(highs) < 2 or len(lows) < 2:
            return None

        high_slope = self._linreg_slope(highs)
        low_slope  = self._linreg_slope(lows)

        falling_highs = high_slope < -0.003
        flat_lows     = abs(low_slope) < 0.008

        if not (falling_highs and flat_lows):
            return None

        range_early = self._range_pct(prices[:len(prices)//2])
        range_late  = self._range_pct(prices[len(prices)//2:])
        converging  = range_late < range_early * 0.75
        vol_slope   = self._linreg_slope(volumes)

        score = 0.0
        score += 0.35 if falling_highs else 0
        score += 0.25 if flat_lows     else 0
        score += 0.25 if converging    else 0
        score += 0.15 if vol_slope < 0 else 0

        return PatternResult(
            name        = "Descending Triangle",
            confidence  = min(score, 1.0),
            direction   = "SHORT",
            description = f"Falling highs (slope={high_slope:.4f}) + flat support. "
                          f"Breakdown likely.",
            score       = score,
        )

    def _symmetrical_triangle(self, prices, volumes) -> Optional[PatternResult]:
        """
        Both highs falling and lows rising — neutral until breakout direction.
        Use momentum to guess direction.
        """
        highs = self._pivot_highs(prices)
        lows  = self._pivot_lows(prices)
        if len(highs) < 2 or len(lows) < 2:
            return None

        high_slope = self._linreg_slope(highs)
        low_slope  = self._linreg_slope(lows)

        if not (high_slope < -0.002 and low_slope > 0.002):
            return None

        range_early = self._range_pct(prices[:len(prices)//2])
        range_late  = self._range_pct(prices[len(prices)//2:])
        converging  = range_late < range_early * 0.70

        # Direction hint: which way is recent price leaning?
        recent = prices[-5:]
        mid    = (max(prices[-20:])+min(prices[-20:])) / 2
        direction = "LONG" if recent[-1] > mid else "SHORT"

        score = 0.0
        score += 0.40 if converging else 0
        score += 0.30  # both slopes correct
        score += 0.15 if self._linreg_slope(volumes) < 0 else 0
        score += 0.15  # symmetry bonus

        return PatternResult(
            name        = "Symmetrical Triangle",
            confidence  = min(score, 1.0),
            direction   = direction,
            description = f"Converging highs and lows. Compression={converging}. "
                          f"Recent price leans {direction}.",
            score       = score,
        )

    def _bull_flag(self, prices, volumes) -> Optional[PatternResult]:
        """
        Sharp pole up (>5% in first half) followed by tight downward channel.
        Very bullish continuation.
        """
        n    = len(prices)
        pole = prices[:n//3]
        flag = prices[n//3:]

        pole_move = (pole[-1] - pole[0]) / pole[0] * 100 if pole[0] else 0
        flag_move = (flag[-1] - flag[0]) / flag[0] * 100 if flag[0] else 0
        flag_range = self._range_pct(flag)

        # Pole must be strong rally, flag must be slight decline, tight range
        strong_pole  = pole_move > 4.0
        slight_pullback = -4.0 < flag_move < 0.5
        tight_flag   = flag_range < 3.0

        if not (strong_pole and slight_pullback and tight_flag):
            return None

        # Volume: high on pole, declining on flag
        pole_vol = sum(volumes[:n//3]) / max(len(volumes[:n//3]),1)
        flag_vol = sum(volumes[n//3:]) / max(len(volumes[n//3:]),1)
        vol_pattern = flag_vol < pole_vol

        score = 0.0
        score += 0.35 if strong_pole     else 0
        score += 0.25 if slight_pullback else 0
        score += 0.25 if tight_flag      else 0
        score += 0.15 if vol_pattern     else 0

        return PatternResult(
            name        = "Bull Flag",
            confidence  = min(score, 1.0),
            direction   = "LONG",
            description = f"Pole +{pole_move:.1f}% then tight flag ({flag_range:.1f}% range). "
                          f"Vol declining on flag={vol_pattern}. Strong continuation signal.",
            score       = score,
        )

    def _bear_flag(self, prices, volumes) -> Optional[PatternResult]:
        """
        Sharp drop followed by tight upward channel. Bearish continuation.
        """
        n    = len(prices)
        pole = prices[:n//3]
        flag = prices[n//3:]

        pole_move  = (pole[-1] - pole[0]) / pole[0] * 100 if pole[0] else 0
        flag_move  = (flag[-1] - flag[0]) / flag[0] * 100 if flag[0] else 0
        flag_range = self._range_pct(flag)

        sharp_drop    = pole_move < -4.0
        slight_bounce = -0.5 < flag_move < 4.0
        tight_flag    = flag_range < 3.0

        if not (sharp_drop and slight_bounce and tight_flag):
            return None

        pole_vol = sum(volumes[:n//3]) / max(len(volumes[:n//3]),1)
        flag_vol = sum(volumes[n//3:]) / max(len(volumes[n//3:]),1)
        vol_pattern = flag_vol < pole_vol

        score = 0.0
        score += 0.35 if sharp_drop    else 0
        score += 0.25 if slight_bounce else 0
        score += 0.25 if tight_flag    else 0
        score += 0.15 if vol_pattern   else 0

        return PatternResult(
            name        = "Bear Flag",
            confidence  = min(score, 1.0),
            direction   = "SHORT",
            description = f"Pole {pole_move:.1f}% then tight flag ({flag_range:.1f}% range). "
                          f"Breakdown continuation expected.",
            score       = score,
        )

    def _consolidation_breakout(self, prices, volumes) -> Optional[PatternResult]:
        """
        Tight range for most of the window, then sudden expansion.
        Direction determined by which way price breaks.
        """
        body  = prices[:-5]
        tail  = prices[-5:]
        body_range = self._range_pct(body)
        tail_move  = (tail[-1] - tail[0]) / tail[0] * 100 if tail[0] else 0
        tail_range = self._range_pct(tail)

        tight_body = body_range < 2.5
        expanding  = tail_range > body_range * 1.5 or abs(tail_move) > 1.0

        if not (tight_body and expanding):
            return None

        direction = "LONG" if tail_move > 0 else "SHORT"

        # Volume expansion on the breakout candle
        avg_vol  = sum(volumes[:-1]) / max(len(volumes[:-1]), 1)
        vol_spike = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

        score = 0.0
        score += 0.30 if tight_body       else 0
        score += 0.30 if expanding        else 0
        score += 0.25 if vol_spike > 1.5  else (0.10 if vol_spike > 1.0 else 0)
        score += 0.15 if abs(tail_move) > 1.5 else 0

        return PatternResult(
            name        = "Consolidation Breakout",
            confidence  = min(score, 1.0),
            direction   = direction,
            description = f"Body range {body_range:.2f}% (tight) then {tail_move:+.2f}% expansion. "
                          f"Vol spike {vol_spike:.1f}x. Breaking {direction}.",
            score       = score,
        )

    def _parabolic_acceleration(self, prices, volumes) -> Optional[PatternResult]:
        """
        Momentum (rate of change) is itself increasing — second derivative positive.
        The classic 'about to go parabolic' signal.
        """
        if len(prices) < 10:
            return None

        # Calculate per-candle returns
        returns = [(prices[i]-prices[i-1])/prices[i-1]*100
                   for i in range(1, len(prices))]

        # Split into two halves and compare average momentum
        half    = len(returns) // 2
        early_mom = sum(returns[:half]) / max(half, 1)
        late_mom  = sum(returns[half:]) / max(len(returns[half:]), 1)
        accel     = late_mom - early_mom   # positive = accelerating

        # Second derivative: is the acceleration itself increasing?
        thirds = len(returns) // 3
        m1 = sum(returns[:thirds]) / max(thirds, 1)
        m2 = sum(returns[thirds:2*thirds]) / max(thirds, 1)
        m3 = sum(returns[2*thirds:]) / max(len(returns[2*thirds:]), 1)
        second_deriv = (m3 - m2) - (m2 - m1)  # positive = curvature up

        accelerating   = accel > 0.1
        curving_up     = second_deriv > 0
        not_overextended = self._range_pct(prices) < 15.0

        if not accelerating:
            return None

        direction = "LONG" if late_mom > 0 else "SHORT"

        score = 0.0
        score += 0.40 if accelerating      else 0
        score += 0.30 if curving_up        else 0
        score += 0.15 if not_overextended  else 0
        score += 0.15 if abs(late_mom) > 0.3 else 0

        return PatternResult(
            name        = "Parabolic Acceleration",
            confidence  = min(score, 1.0),
            direction   = direction,
            description = f"Momentum accelerating: early={early_mom:.3f}% → late={late_mom:.3f}%. "
                          f"2nd deriv={second_deriv:.4f}. Curvature up={curving_up}.",
            score       = score,
        )

    def _volume_climax(self, prices, volumes) -> Optional[PatternResult]:
        """
        Extreme volume spike (4x+) at a key price level.
        Can signal reversal or continuation depending on price action.
        """
        if len(volumes) < 5:
            return None

        avg_vol   = sum(volumes[:-1]) / max(len(volumes[:-1]), 1)
        last_vol  = volumes[-1]
        spike     = last_vol / avg_vol if avg_vol > 0 else 1.0

        if spike < 3.0:
            return None

        # Price action on the climax candle
        price_move = (prices[-1] - prices[-2]) / prices[-2] * 100 if prices[-2] else 0
        direction  = "LONG" if price_move > 0 else "SHORT"

        score = 0.0
        if spike >= 6.0:   score += 0.50
        elif spike >= 4.0: score += 0.35
        else:              score += 0.20
        score += 0.30 if abs(price_move) > 1.0 else 0.15
        score += 0.20  # climax baseline

        return PatternResult(
            name        = "Volume Climax",
            confidence  = min(score, 1.0),
            direction   = direction,
            description = f"Volume spike {spike:.1f}x average on {price_move:+.2f}% candle. "
                          f"Institutional activity detected.",
            score       = score,
        )


# ── Convenience function for use in Trading_Bot_V2 strategies ────────────────

def score_patterns(prices: List[float], volumes: List[float]) -> dict:
    """
    Returns a summary dict compatible with Trading_Bot_V2 strategy scoring.
    {
      "best_pattern": str,
      "best_confidence": float,
      "direction": str,
      "pattern_score": float,    # 0-1, use directly in strategy scoring
      "all_patterns": [...]
    }
    """
    detector = PatternDetector()
    results  = detector.detect(prices, volumes)
    if not results:
        return {
            "best_pattern":    "none",
            "best_confidence": 0.0,
            "direction":       "NEUTRAL",
            "pattern_score":   0.0,
            "all_patterns":    [],
        }
    best = results[0]
    return {
        "best_pattern":    best.name,
        "best_confidence": best.confidence,
        "direction":       best.direction,
        "pattern_score":   best.confidence,
        "all_patterns":    [{"name": r.name, "confidence": r.confidence,
                             "direction": r.direction} for r in results],
    }
