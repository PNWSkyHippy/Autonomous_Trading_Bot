from typing import Dict

def determine_direction(prices):
    if len(prices) < 5:
        return "NEUTRAL"

    move = (prices[-1] - prices[-5]) / prices[-5] * 100

    if move > 0.5:
        return "LONG"
    elif move < -0.5:
        return "SHORT"
    return "NEUTRAL"


def continuation_probability(signal) -> float:
    score = 0

    # momentum
    if signal.momentum_score > 0.5:
        score += 25

    # volume
    if signal.volume_spike > 2:
        score += 25

    # pattern strength
    if "strong" in signal.pattern_detected:
        score += 25
    elif "early" in signal.pattern_detected:
        score += 15

    # avoid weak signals
    if signal.confidence > 0.6:
        score += 25

    return min(score, 100)


def classify_signal(signal) -> Dict:
    prices = signal.price_history

    direction = determine_direction(prices)
    poc = continuation_probability(signal)

    # classify stage
    if signal.confidence > 0.75:
        stage = "CONFIRMED"
    elif signal.confidence > 0.6:
        stage = "EARLY"
    else:
        stage = "WATCHLIST"

    return {
        "direction": direction,
        "poc": poc,
        "stage": stage
    }