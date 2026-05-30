"""
=============================================================
  INVERSE ETF MAPPER
  Converts bearish stock signals into inverse ETF buy signals.

  Instead of shorting a stock (requires margin/PDT exposure),
  the bot buys the corresponding inverse ETF — same profit
  potential on downside, no margin required, no PDT issues,
  fully legal in a cash account.

  Also supports leveraged ETFs for bullish signals on
  high-conviction setups (SOXL, TQQQ etc).

  How it works:
  - Stock scanner generates SHORT signal on SPY
  - Mapper intercepts it, replaces SPY with SH
  - Direction flips from SHORT to LONG
  - Bot buys SH — profits when SPY falls
  - Everything else (stop loss, take profit, position size)
    stays exactly the same

  LEVERAGED ETF NOTES:
  - SOXL/SOXS move ~3x the underlying SOXX index
  - Stop loss and take profit are automatically widened
    for leveraged ETFs to avoid noise-triggered exits
  - Position size is automatically reduced for leveraged ETFs
=============================================================
"""

import logging
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  BEARISH SIGNAL → INVERSE ETF
#  When bot generates a SHORT signal on these symbols,
#  buy the inverse ETF instead.
# ---------------------------------------------------------------------------
INVERSE_MAP: Dict[str, str] = {
    # Broad market
    "SPY":   "SH",       # S&P 500 inverse (1x)
    "QQQ":   "PSQ",      # Nasdaq 100 inverse (1x)
    "IWM":   "RWM",      # Russell 2000 inverse (1x)
    "DIA":   "DOG",      # Dow Jones inverse (1x)

    # Sectors
    "XLF":   "SKF",      # Financials inverse (2x)
    "XLE":   "ERY",      # Energy inverse (2x)
    "XLK":   "REW",      # Technology inverse (2x)
    "XLV":   "RXD",      # Healthcare inverse (2x)
    "XLI":   "SIJ",      # Industrials inverse (1x)
    "XLU":   "SDP",      # Utilities inverse (2x)

    # Semiconductors
    "SOXX":  "SOXS",     # Semiconductor inverse (3x)
    "SMH":   "SOXS",     # Semiconductor inverse (3x)
    "NVDA":  "SOXS",     # Use sector inverse for individual chip stocks
    "AMD":   "SOXS",
    "INTC":  "SOXS",
    "TSM":   "SOXS",
    "MRVL":  "SOXS",
    "AVGO":  "SOXS",

    # Tech giants — use QQQ inverse
    "AAPL":  "PSQ",
    "MSFT":  "PSQ",
    "GOOGL": "PSQ",
    "AMZN":  "PSQ",
    "META":  "PSQ",
    "NOW":   "PSQ",
    "PLTR":  "PSQ",
    "ALAB":  "PSQ",
    "TSLA":  "SQQQ",     # Tesla is extra volatile, use 3x inverse

    # Financials
    "JPM":   "SKF",
    "BAC":   "SKF",
    "GS":    "SKF",

    # Energy
    "XOM":   "ERY",
    "CVX":   "ERY",
}

# ---------------------------------------------------------------------------
#  BULLISH SIGNAL → LEVERAGED ETF (optional)
#  When bot generates a LONG signal on these symbols,
#  optionally use the leveraged version for amplified gains.
#  Set USE_LEVERAGED_LONGS = True in config to enable.
#
#  Individual stocks map to their SECTOR leveraged ETF —
#  the same logic as the inverse map (NVDA short → SOXS,
#  so NVDA long → SOXL).
# ---------------------------------------------------------------------------
LEVERAGED_MAP: Dict[str, str] = {
    # Broad market
    "SPY":   "SPXL",     # S&P 500 3x bull
    "QQQ":   "TQQQ",     # Nasdaq 3x bull
    "IWM":   "TNA",      # Russell 2000 3x bull
    "DIA":   "UDOW",     # Dow Jones 3x bull

    # Sectors
    "XLF":   "FAS",      # Financials 3x bull
    "XLE":   "ERX",      # Energy 3x bull
    "XLK":   "TECL",     # Technology 3x bull
    "XLV":   "CURE",     # Healthcare 3x bull
    "XLI":   "WANT",     # Industrials 3x bull (note: lower liquidity)
    "XLU":   "UTSL",     # Utilities 3x bull (note: lower liquidity)

    # Semiconductors — individual stocks and ETFs all map to SOXL
    "SOXX":  "SOXL",
    "SMH":   "SOXL",
    "NVDA":  "SOXL",
    "AMD":   "SOXL",
    "INTC":  "SOXL",
    "TSM":   "SOXL",
    "MRVL":  "SOXL",
    "AVGO":  "SOXL",

    # Tech giants — map to TQQQ (same as QQQ leveraged long)
    "AAPL":  "TQQQ",
    "MSFT":  "TQQQ",
    "GOOGL": "TQQQ",
    "AMZN":  "TQQQ",
    "META":  "TQQQ",
    "NOW":   "TQQQ",
    "PLTR":  "TQQQ",
    "ALAB":  "TQQQ",
    "TSLA":  "TQQQ",     # TSLA long → TQQQ (3x Nasdaq bull, safer than individual)

    # Financials
    "JPM":   "FAS",
    "BAC":   "FAS",
    "GS":    "FAS",

    # Energy
    "XOM":   "ERX",
    "CVX":   "ERX",
}

# ---------------------------------------------------------------------------
#  LEVERAGE MULTIPLIERS
#  Used to adjust stop loss / take profit / position size
#  for leveraged ETFs to account for their higher volatility.
# ---------------------------------------------------------------------------
LEVERAGE_FACTORS: Dict[str, float] = {
    # 3x leveraged long
    "SOXL": 3.0, "TQQQ": 3.0, "SPXL": 3.0,
    "TNA":  3.0, "FAS":  3.0, "ERX":  3.0,
    "TECL": 3.0, "CURE": 3.0, "UDOW": 3.0,
    "WANT": 3.0, "UTSL": 3.0,
    "LABU": 3.0, "MIDU": 3.0,

    # 3x leveraged inverse
    "SOXS": 3.0, "SQQQ": 3.0, "SPXS": 3.0,
    "TZA":  3.0, "FAZ":  3.0, "ERY":  3.0,
    "TECS": 3.0, "LABD": 3.0,
    "UVXY": 3.0,

    # 2x leveraged long
    "SSO":  2.0, "QLD":  2.0, "UYG":  2.0,
    "ROM":  2.0, "RXL":  2.0,

    # 2x leveraged inverse
    "SDS":  2.0, "QID":  2.0, "SKF":  2.0,
    "SDP":  2.0, "REW":  2.0, "RXD":  2.0,
    "SIJ":  2.0,

    # 1x inverse (no volatility adjustment needed, but track them)
    "SH":   1.0, "PSQ":  1.0, "RWM":  1.0,
    "DOG":  1.0,
}


def map_signal(
    symbol: str,
    direction: str,
    score: float,
    current_price: float,
    indicators: dict,
    use_leveraged_longs: bool = False,
) -> Tuple[str, str, dict]:
    """
    Map a stock signal to its inverse/leveraged ETF equivalent.

    Returns:
        (mapped_symbol, mapped_direction, updated_indicators)

    If no mapping applies, returns the original values unchanged.
    """
    mapped_symbol      = symbol
    mapped_direction   = direction
    updated_indicators = dict(indicators)

    if direction == "short" and symbol in INVERSE_MAP:
        # Flip: buy inverse ETF instead of shorting the stock
        mapped_symbol    = INVERSE_MAP[symbol]
        mapped_direction = "long"
        updated_indicators["original_symbol"] = symbol
        updated_indicators["inverse_etf"]     = mapped_symbol
        updated_indicators["mapping_type"]    = "inverse"
        logger.info(
            f"Inverse ETF: SHORT {symbol} → LONG {mapped_symbol} "
            f"(cash account, no margin needed)"
        )

    elif direction == "long" and use_leveraged_longs and symbol in LEVERAGED_MAP:
        # Optionally upgrade bullish signals to leveraged ETF
        mapped_symbol    = LEVERAGED_MAP[symbol]
        mapped_direction = "long"
        updated_indicators["original_symbol"] = symbol
        updated_indicators["leveraged_etf"]   = mapped_symbol
        updated_indicators["mapping_type"]    = "leveraged"
        logger.info(
            f"Leveraged ETF: LONG {symbol} → LONG {mapped_symbol} "
            f"(amplified bull)"
        )

    # Adjust risk parameters for leveraged ETFs
    leverage = LEVERAGE_FACTORS.get(mapped_symbol, 1.0)
    if leverage > 1.0:
        updated_indicators["leverage_factor"] = leverage

        base_sl  = indicators.get("custom_stop_loss_pct")   or 1.5
        base_tp  = indicators.get("custom_take_profit_pct") or 3.0
        base_pos = indicators.get("custom_position_pct")    or 2.0

        # Widen stops proportionally to leverage, capped at sane limits
        updated_indicators["custom_stop_loss_pct"]   = min(base_sl  * leverage, 8.0)
        updated_indicators["custom_take_profit_pct"] = min(base_tp  * leverage, 15.0)
        # Reduce position size so dollar volatility stays constant
        updated_indicators["custom_position_pct"]    = max(base_pos / leverage, 0.5)

        logger.debug(
            f"Leveraged ETF {mapped_symbol} ({leverage}x): "
            f"SL={updated_indicators['custom_stop_loss_pct']:.1f}% "
            f"TP={updated_indicators['custom_take_profit_pct']:.1f}% "
            f"Pos={updated_indicators['custom_position_pct']:.1f}%"
        )

    return mapped_symbol, mapped_direction, updated_indicators


def get_inverse(symbol: str) -> Optional[str]:
    """Get the inverse ETF for a symbol, or None if not mapped."""
    return INVERSE_MAP.get(symbol)


def get_leveraged(symbol: str) -> Optional[str]:
    """Get the leveraged ETF for a symbol, or None if not mapped."""
    return LEVERAGED_MAP.get(symbol)


def is_leveraged_etf(symbol: str) -> bool:
    """Check if a symbol is a leveraged ETF."""
    return symbol in LEVERAGE_FACTORS


def get_leverage_factor(symbol: str) -> float:
    """Get the leverage multiplier for a symbol (1.0 if not leveraged)."""
    return LEVERAGE_FACTORS.get(symbol, 1.0)
