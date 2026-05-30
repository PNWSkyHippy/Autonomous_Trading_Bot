from typing import List

def is_clean_breakout(prices: List[float], volumes: List[float], signal) -> bool:
    """
    Returns True only for high-quality early breakout setups.
    """

    if len(prices) < 20 or len(volumes) < 5:
        return False

    current_price = prices[-1]

    # --- 1) Compression (tight range)
    high = max(prices[-20:])
    low = min(prices[-20:])
    range_pct = (high - low) / max(low, 1e-9)

    if range_pct > 0.03:  # >3% = too loose
        return False

    # --- 2) Breakout trigger
    recent_high = max(prices[-10:])
    if current_price < recent_high * 1.01:
        return False

    # --- 3) Volume must lead
    if signal.volume_spike < 2:
        return False

    if volumes[-1] < volumes[-2]:
        return False

    # --- 4) Not extended
    base = min(prices[-10:])
    move_pct = (current_price - base) / max(base, 1e-9)

    if move_pct > 0.05:  # already moved >5%
        return False

    # --- 5) Momentum
    if signal.momentum_score < 0.5:
        return False

    return True