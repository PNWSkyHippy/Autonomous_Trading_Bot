"""
config.py — All settings, API keys, and tunable parameters
Trading Bot v2
"""
from cryptography.fernet import InvalidToken, Fernet
import os
from dotenv import load_dotenv
import keyring
load_dotenv()

# =============================================================================
# FERNET DECRYPT HELPER
# =============================================================================
# All sensitive API keys are stored as Fernet-encrypted tokens in .env /
# scanner_config.json.  Use your key-encryptor frontend to produce tokens,
# then paste the output into .env (or scanner_config.json for scanner keys).
#
# Fernet tokens always start with "gAAAAA".  Any value that does NOT start
# with that prefix is treated as plaintext and returned unchanged — this lets
# you migrate keys one at a time without breaking unencrypted ones.
#
# If an encrypted token is found but the Fernet key is missing from Windows
# Credential Manager, a RuntimeError is raised at startup (fail fast).
# =============================================================================

_fernet_raw = keyring.get_password("trading_bot_v2", "fernet_key")
_BASE_KEY   = _fernet_raw.encode() if _fernet_raw else None

def _decrypt(val: str) -> str:
    """
    Decrypt a Fernet-encrypted API key loaded from .env.

    - Empty string        → returned unchanged (key not configured).
    - Plaintext value     → returned unchanged (migration-safe).
    - Fernet token        → decrypted and returned.
    - Encrypted but no Fernet key in Credential Manager → RuntimeError.
    """
    if not val:
        return val
    val = val.strip()
    if not val.startswith("gAAAAA"):
        return val                          # plaintext — not yet encrypted
    if not _BASE_KEY:
        raise RuntimeError(
            "Encrypted API key found in .env but the Fernet key is not in "
            "Windows Credential Manager.  "
            "Run: python Scripts\\store_fernet_key.py"
        )
    try:
        return Fernet(_BASE_KEY).decrypt(val.encode()).decode().strip()
    except InvalidToken:
        raise RuntimeError(
            f"Failed to decrypt an API key — token is corrupt or was encrypted "
            f"with a different Fernet key.  First 20 chars: {val[:20]}…"
        )

# =============================================================================
# DB-BACKED API KEY LOOKUP
# =============================================================================
# Priority:  DB (encrypted)  →  .env / config fallback
#
# To add or update a key:
#   python Scripts/set_api_key.py              (interactive)
#   python Scripts/set_api_key.py --list       (show stored services/names)
#
# Keys stored in DB as Fernet-encrypted tokens (same key as dashboard auth).
# Missing rows silently fall back to the .env / hardcoded default below.
# =============================================================================

def _db_key(service: str, key_name: str, fallback: str = "") -> str:
    """Load a key from the DB api_keys table, decrypt it, fall back to env."""
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        _db_path = _P(__file__).parent / "data" / "trading_bot.db"
        if not _db_path.exists():
            return fallback
        _conn = _sq.connect(str(_db_path))
        _row  = _conn.execute(
            "SELECT value_enc FROM api_keys WHERE service=? AND key_name=?",
            (service, key_name)
        ).fetchone()
        _conn.close()
        if _row and _row[0]:
            return _decrypt(_row[0])   # Fernet token → plaintext
    except Exception:
        pass
    return fallback


# =============================================================================
# API KEYS  — DB first, .env fallback (both go through _decrypt)
# =============================================================================

ALPACA_API_KEY      = _db_key("alpaca",    "api_key",    _decrypt(os.getenv("ALPACA_API_KEY",      "")))
ALPACA_SECRET_KEY   = _db_key("alpaca",    "secret_key", _decrypt(os.getenv("ALPACA_SECRET_KEY",   "")))
ALPACA_BASE_URL     = _db_key("alpaca",    "base_url",            os.getenv("ALPACA_BASE_URL",     "https://paper-api.alpaca.markets"))

COINBASE_API_KEY    = _db_key("coinbase",  "api_key",    _decrypt(os.getenv("COINBASE_API_KEY",    "")))
COINBASE_SECRET_KEY = _db_key("coinbase",  "secret_key", _decrypt(os.getenv("COINBASE_SECRET_KEY", "")))

KRAKEN_API_KEY      = _db_key("kraken",    "api_key",    _decrypt(os.getenv("KRAKEN_API_KEY",      "")))
KRAKEN_SECRET_KEY   = _db_key("kraken",    "secret_key", _decrypt(os.getenv("KRAKEN_SECRET_KEY",   "")))

ANTHROPIC_API_KEY   = _db_key("anthropic", "api_key",    _decrypt(os.getenv("ANTHROPIC_API_KEY",  "")))

COINDESK_API_KEY    = _db_key("coindesk",  "api_key",    _decrypt(os.getenv("COINDESK_API_KEY", "")))

KRAKEN_ENABLED      = True

# Leverage for Kraken margin (short) trades.
# KRAKEN_SHORT_LEVERAGE = the leverage level we actually want to use (e.g. 2 = 2x).
# KRAKEN_MAX_LEVERAGE   = the ceiling Kraken offers (used to scale position size).
#
# Position sizing math:
#   scale_factor = KRAKEN_SHORT_LEVERAGE / KRAKEN_MAX_LEVERAGE   (= 0.20 at defaults)
#   order_notional = normal_position_value * scale_factor
#   margin_required = order_notional / KRAKEN_SHORT_LEVERAGE
#
# At defaults ($200 normal position):
#   order notional = $200 * 0.20 = $40, leverage=2x, margin=$20
#   This keeps effective exposure conservative — 20% of max-leveraged size.
KRAKEN_SHORT_LEVERAGE = 2    # desired leverage level (1-5 typical; Kraken allows up to 10)
KRAKEN_MAX_LEVERAGE   = 10   # Kraken's advertised max for most pairs

# Interactive Brokers — add keys to .env when account is ready
# IBKR uses TWS or IB Gateway running locally (no REST key — just account ID)
# TWS paper port: 7497  |  TWS live port: 7496
IBKR_ACCOUNT        = _db_key("ibkr",       "account",             os.getenv("IBKR_ACCOUNT", ""))
IBKR_ENABLED        = True
IBKR_PAPER_MODE     = True

CLAUDE_REVIEWER_ENABLED = True
CLAUDE_REVIEWER_MODE = "strict"
CLAUDE_REVIEWER_APPLY_SIZE_ADVICE = True

MASSIVE_API_KEY     = _db_key("massive",   "api_key",    _decrypt(os.getenv("MASSIVE_API_KEY",    "")))

EMAIL_SENDER        = _db_key("email",     "sender",              os.getenv("EMAIL_SENDER",       ""))
EMAIL_PASSWORD      = _db_key("email",     "password",   _decrypt(os.getenv("EMAIL_PASSWORD",     "")))
EMAIL_RECIPIENT     = _db_key("email",     "recipient",           os.getenv("EMAIL_RECIPIENT",    ""))

# =============================================================================
# CAPITAL & TRADING PARAMETERS
# =============================================================================

STARTING_CAPITAL        = 100000.00
BOT_CAPITAL_ALLOCATION  = 0.00
DAILY_PROFIT_TARGET_PCT = 2.0
GOAL_CAPITAL            = 1038198.96
GOAL_DAYS               = 480

# Position sizing
MAX_POSITION_PCT        = 2.0
MIN_SIGNAL_CONFIDENCE   = 0.65

# Stop-loss / take-profit defaults
DEFAULT_STOP_LOSS_PCT   = 1.5
DEFAULT_TAKE_PROFIT_PCT = 3.0
TRAILING_STOP_PCT       = 1.5

# ---------------------------------------------------------------------------
# Backtest friction assumptions
# Applied as round-trip cost (commission + slippage) per closed trade.
# Per-leg means entry AND exit each incur the cost (total = 2× per trade).
#
# Stocks  — Alpaca: zero commission; assume 1 bp half-spread for fills.
# Crypto  — Kraken taker fee 0.26% (standard tier); 5 bp half-spread.
#            Higher-volume traders pay less (0.10% at highest tier).
# Set both to 0.0 to reproduce the old friction-free (optimistic) results.
# ---------------------------------------------------------------------------
BT_COMMISSION_STOCK_PCT  = 0.00   # Alpaca is commission-free
BT_COMMISSION_CRYPTO_PCT = 0.26   # Kraken taker fee, standard tier
BT_SLIPPAGE_STOCK_PCT    = 0.01   # stock NBBO half-spread estimate
BT_SLIPPAGE_CRYPTO_PCT   = 0.05   # crypto bid-ask half-spread estimate

# ---------------------------------------------------------------------------
# Fee-hurdle floors (derived)  —  added by Opus audit 2026-05-29
# Round-trip cost = (commission + slippage) * 2 legs.
#   crypto = (0.26 + 0.05) * 2 = 0.62 %
#   stock  = (0.00 + 0.01) * 2 = 0.02 %
# Any exit that books less than its asset-class hurdle is a NET LOSS even if it
# is "in profit". Every profit-lock / time-stop threshold below MUST clear this.
# ---------------------------------------------------------------------------
FEE_RT_CRYPTO_PCT  = (BT_COMMISSION_CRYPTO_PCT + BT_SLIPPAGE_CRYPTO_PCT) * 2   # 0.62
FEE_RT_STOCK_PCT   = (BT_COMMISSION_STOCK_PCT  + BT_SLIPPAGE_STOCK_PCT)  * 2   # 0.02
# Minimum NET profit a trailing stop / profit-lock is allowed to book.
# = fee hurdle + buffer. Below this the protective stop stands instead.
MIN_NET_PROFIT_CRYPTO_PCT = round(FEE_RT_CRYPTO_PCT + 0.40, 2)   # ~1.02 %
MIN_NET_PROFIT_STOCK_PCT  = round(FEE_RT_STOCK_PCT  + 0.30, 2)   # ~0.32 %

# Momentum rider
MOMENTUM_RIDER_ENABLED        = True
MOMENTUM_RIDER_RSI_MIN        = 50
MOMENTUM_RIDER_VOL_RATIO      = 1.3
MOMENTUM_RIDER_MIN_SCORE      = 0.67
MOMENTUM_RIDER_MAX_EXTENSIONS = 3
MOMENTUM_RIDER_LOCK_PROFIT_RATIO = 0.50  # lock half of open profit when TP is extended
MOMENTUM_RIDER_MIN_LOCK_PCT      = 1.00  # RAISED: Opus audit — 0.25% was below 0.62% crypto fee hurdle

# Risk circuit breakers
MAX_DAILY_LOSS_PCT      = 15.0
MAX_CONSECUTIVE_LOSSES  = 10
MAX_OPEN_POSITIONS      = 30

# Price filters
STOCK_MIN_PRICE     = 0.40
STOCK_MAX_PRICE     = 5000.00
STOCK_MIN_VOLUME    = 100000
CRYPTO_MIN_PRICE    = 0.001
CRYPTO_MAX_PRICE    = 999999.00

# Inverse ETF / Leveraged ETF settings
USE_LEVERAGED_LONGS = False

# Cash account / PDT settings
# T+1 is the SEC standard since May 2024 (was T+2).
# IBKR cash accounts use T+1. Alpaca cash accounts use T+1.
# Set SETTLEMENT_DAYS=2 only if your broker still settles T+2.
CASH_ACCOUNT_MODE   = True
SETTLEMENT_DAYS     = 1

# Market timing (Eastern Time)
MARKET_OPEN_BUFFER_MIN   = 30
STOCK_SCAN_INTERVAL_SEC  = 60
CRYPTO_SCAN_INTERVAL_SEC = 300

# =============================================================================
# BROKER SETTINGS
# =============================================================================

ALPACA_PAPER_MODE   = True
KRAKEN_PAPER_MODE   = True

# =============================================================================
# WATCHLISTS
# =============================================================================

def _load_watchlist(filepath: str, fallback: list) -> list:
    """Load a watchlist from a text file, falling back to hardcoded list."""
    try:
        with open(filepath, "r") as f:
            symbols = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
        if symbols:
            return symbols
    except FileNotFoundError:
        pass
    return fallback

_STOCK_FALLBACK = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "AMD",  "INTC", "BABA",  "NFLX", "SPY",  "QQQ",  "IWM",
    "JPM",  "BAC",  "GS",    "XOM",  "CVX",  "PFE",
]

_CRYPTO_FALLBACK = [
    "BTC/USD",  "ETH/USD",  "SOL/USD",  "ADA/USD",
    "MATIC/USD","DOT/USD",  "AVAX/USD", "LINK/USD",
    "LTC/USD",  "XRP/USD",
]

STOCK_WATCHLIST  = _load_watchlist("watchlists/stocks.txt",  _STOCK_FALLBACK)
CRYPTO_WATCHLIST = _load_watchlist("watchlists/crypto.txt",  _CRYPTO_FALLBACK)

# Strategy whitelists are conservative backtest filters. Manual/scanner
# injected symbols are deliberate overrides, so let them reach strategy logic
# instead of being rejected at the first whitelist gate.
BYPASS_STRATEGY_WHITELIST_FOR_INJECTED_SYMBOLS = True

# =============================================================================
# DATABASE
# =============================================================================

DB_PATH = "data/trading_bot.db"

# =============================================================================
# REPORTING
# =============================================================================

EOD_REPORT_HOUR_ET  = 17
ML_RETRAIN_HOUR_ET  = 18
CAPITAL_SYNC_MIN    = 15
ML_MIN_TRADES       = 50

# =============================================================================
# STRATEGY AUTO-DISABLE
# =============================================================================

STRATEGY_MIN_TRADES         = 20
STRATEGY_DISABLE_WIN_RATE   = 45.0
STRATEGY_REENABLE_WIN_RATE  = 50.0

# =============================================================================
# VERBOSE MODE
# =============================================================================

VERBOSE_MODE = False

# =============================================================================
# POSITION MONITOR EXIT SETTINGS
# =============================================================================

# Performance-contingent time stops
# Closes trades that aren't producing within expected timeframes.
# Frees up capital for better opportunities rather than waiting for stop loss.
PERF_TIME_STOP_ENABLED   = True
PERF_CHECK_1HR_MIN_PCT   = 0.2    # need at least 0.5% profit after 1 hour
PERF_CHECK_3HR_MIN_PCT   = 1.5    # need at least 1.5% profit after 3 hours
HARD_TIME_STOP_HOURS     = 3.5    # hard kill after 3.5 hours regardless

# Early loss identification
# At 20 min: if trade is negative AND still moving wrong direction → close
EARLY_LOSS_ENABLED       = True
EARLY_LOSS_CHECK_MINUTES = 20
EARLY_LOSS_MIN_PCT       = -0.3   # trigger threshold (negative = loss)

# Pivot point break exit
# Long breaks below S1 → close | Short breaks above R1 → close
# Only fires after 30 minutes to allow trade to breathe past entry noise
PIVOT_EXIT_ENABLED       = True
PIVOT_EXIT_BARS          = 20     # bars used to calculate pivot levels

# =============================================================================
# DAILY MARKET SCANNER
# =============================================================================

SCANNER_ENABLED          = True
SCANNER_TOP_N            = 10
SCANNER_RUN_TIME_ET      = "16:30"   # 4:30 PM ET = 1:30 PM PT

# Stock filter thresholds
STOCK_PCT_OF_52W_HIGH    = 0.98
STOCK_VOL_SURGE_MIN      = 1.30
STOCK_HISTORY_DAYS       = "1y"

# Crypto filter thresholds
CRYPTO_RSI_MIN           = 50
CRYPTO_RSI_MAX           = 80
CRYPTO_VOL_SURGE_MIN     = 1.20

# CoinGecko rate limiting
COINGECKO_API_KEY        = ""     # leave blank for free tier
COINGECKO_DELAY          = 2.5
COINGECKO_MAX_RETRIES    = 3
COINGECKO_BASE_WAIT      = 30
WIKIPEDIA_DELAY          = 3.0

# Scanner file paths
SCANNER_REPORTS_DIR      = "reports"
SCANNER_WATCHLIST_DIR    = "watchlist"
TEMP_STOCKS_FILE         = "watchlist/scanned_stocks.txt"
TEMP_CRYPTO_FILE         = "watchlist/scanned_crypto.txt"

# =============================================================================
# SIGNAL FINE-TUNING
# =============================================================================

SIGNAL_TUNING = {

    # -------------------------------------------------------------------------
    # GLOBAL
    # -------------------------------------------------------------------------
    "min_signal_confidence":        0.65,

    # -------------------------------------------------------------------------
    # ORIGINAL SCANNER
    # -------------------------------------------------------------------------
    "stock_rsi_oversold":           35,
    "stock_rsi_overbought":         65,
    "stock_rsi_weight":             2.0,
    "stock_macd_weight":            2.5,
    "stock_ema_weight":             2.0,
    "stock_bb_weight":              1.5,
    "stock_volume_weight":          1.0,
    "stock_volume_spike_ratio":     1.5,
    "stock_bb_lower_pct":           0.05,
    "stock_bb_upper_pct":           0.95,
    "crypto_rsi_oversold":          38,
    "crypto_rsi_overbought":        62,
    "crypto_rsi_weight":            2.5,
    "crypto_macd_weight":           2.5,
    "crypto_ema_weight":            2.0,
    "crypto_bb_weight":             1.5,
    "crypto_volume_weight":         1.0,
    "crypto_volume_spike_ratio":    1.5,
    "crypto_bb_lower_pct":          0.05,
    "crypto_bb_upper_pct":          0.95,

    # -------------------------------------------------------------------------
    # STRATEGY 1: RSI Momentum
    # -------------------------------------------------------------------------
    "rsi_momentum_oversold":        30,
    "rsi_momentum_overbought":      70,
    "rsi_momentum_period":          14,
    "rsi_momentum_min_score":       0.60,

    # -------------------------------------------------------------------------
    # STRATEGY 2: Bollinger Breakout
    # -------------------------------------------------------------------------
    # Audit 2026-05-16: ADX 20→30, squeeze now a hard gate (was score bonus)
    #   Sweep (72 combos, BTC 1h): ADX30+squeeze only combo with PF>1.2 (+net)
    #   ADX20 no-squeeze = PF~1.07, -net. ADX30+squeeze = PF 1.28, +$2,658
    # -------------------------------------------------------------------------
    "bb_breakout_period":           20,
    "bb_breakout_std":              2.0,
    "bb_squeeze_threshold":         0.04,
    "bb_breakout_min_score":        0.70,
    "bb_adx_min":                   30,    # was hardcoded 20 — raised per audit sweep

    # -------------------------------------------------------------------------
    # STRATEGY 3: EMA Crossover
    # -------------------------------------------------------------------------
    "ema_fast_period":              9,
    "ema_slow_period":              21,
    "ema_trend_period":             50,
    "ema_crossover_min_score":      0.60,

    # -------------------------------------------------------------------------
    # STRATEGY 4: Mean Reversion
    # Audit 2026-05-22: period 20→10, zscore 2.0→1.5, min_score 0.60→0.70
    #   225-combo sweep (50 stocks, 90d): period=10 dominates universally.
    #   zscore=1.5 + period=10 + two_bar_2 stop → PF 1.626, $8,260 total profit
    #   (vs zscore=2.0 → PF 1.704, $3,163 — fewer trades, lower absolute return)
    #   min_score was a dead filter at 0.55-0.65 (identical results across all values)
    #   Raised to 0.70 to actually have filtering effect.
    # -------------------------------------------------------------------------
    "mean_rev_zscore_entry":        1.5,   # was 2.0 — lowered per sweep (max total profit)
    "mean_rev_period":              10,    # was 20 — biggest single improvement per sweep
    "mean_rev_min_score":           0.70,  # was 0.60 — raised; was filtering nothing at 0.55-0.65

    # -------------------------------------------------------------------------
    # STRATEGY 5: Scalp Master
    # -------------------------------------------------------------------------
    "scalp_rsi_oversold":           35,
    "scalp_rsi_overbought":         65,
    "scalp_rsi_period":             7,
    "scalp_min_volume_ratio":       1.2,
    "scalp_min_score":              0.60,
    "scalp_adx_min":                20,

    # -------------------------------------------------------------------------
    # STRATEGY 6: Swing Trader
    # -------------------------------------------------------------------------
    "swing_adx_min":                28,    # was 20 — too permissive, 60% of bars qualify; 28+ = strong trend only
    "swing_adx_period":             14,
    "swing_rsi_oversold":           32,    # was 40 — fired on every minor dip; 32 = genuine pullback in trend
    "swing_rsi_overbought":         68,    # was 60 — fired constantly in uptrends; 68 = genuine extension
    "swing_min_score":              0.60,

    # -------------------------------------------------------------------------
    # STRATEGY 7: Grid Bot
    # Audit 2026-05-16: ADX20 → ADX15 (Iter1 confirmed improvement)
    #   BTC: WR 42%→50%, PF 1.109→1.621, Sharpe -0.99→+0.975, DD 4.3%→1.3%
    #   ADX<20 lets in "quiet trend" periods; ADX<15 = genuinely flat/ranging only
    # -------------------------------------------------------------------------
    "grid_adx_max":                 15,   # was 20 — lowered per audit Iter1
    "grid_adx_period":              14,
    "grid_bb_period":               30,
    "grid_long_edge_pct":           0.20, # long when price is in bottom 20% of BB range
    "grid_short_edge_pct":          0.80, # short when price is in top 20% of BB range
    "grid_min_score":               0.70,

    # -------------------------------------------------------------------------
    # STRATEGY 8: DCA Accumulator
    # Audit 2026-05-16: rsi_max 45→35 (sweep: RSI35 dominates all top slots)
    #   RSI45/55 results: PF ~1.01-1.02. RSI35: PF 1.115. Tighter = better entries.
    # -------------------------------------------------------------------------
    "dca_ema_period":               50,
    "dca_dip_pct":                  0.02,
    "dca_rsi_max":                  35,    # was 45 — tightened per audit sweep
    "dca_min_score":                0.60,

    # -------------------------------------------------------------------------
    # STRATEGY 9: VWAP Momentum
    # -------------------------------------------------------------------------
    "vwap_mom_adx_min":             25,
    "vwap_mom_adx_period":          14,
    "vwap_mom_rsi_period":          14,
    "vwap_mom_rsi_low":             40,
    "vwap_mom_rsi_high":            60,
    "vwap_mom_volume_ratio":        1.5,
    "vwap_mom_min_score":           0.65,

    # -------------------------------------------------------------------------
    # ML SCORER BOOST
    # -------------------------------------------------------------------------
    "ml_score_multiplier":          1.5,
    "ml_min_confidence":            0.55,
}

# ── Alert Digest Queue ──────────────────────────────────────────────────────────────
# Trade open/close alerts are batched into a single digest email sent every
# ALERT_DIGEST_HOURS hours instead of one email per trade.
# Errors and trading halts always bypass the queue and send immediately.
# Set to 0 to disable batching (reverts to one email per trade — old behavior).
ALERT_DIGEST_HOURS = 1   # Change to 2 or 3 to reduce email further

# ── Breakout Signal API ────────────────────────────────────────
BOT_API_KEY           = _db_key("trading_bot", "bot_api_key", "")
BOT_API_PORT          = 8181
BREAKOUT_MIN_ESCALATION = 1   # 0-3; only inject escalation >= this


