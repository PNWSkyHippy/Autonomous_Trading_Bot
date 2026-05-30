"""
Ron's Market Breakout Scanner
==============================
Scans stocks and crypto for early breakout patterns.

TWO-PHASE DETECTION:
  Phase 1 — WATCHLIST: quiet setup spotted, symbol tracked silently.
  Phase 2 — ALERT:     price actually starts moving in the right direction
                        within the first two scan cycles → THEN alert fires.

Escalating alerts:
  Move >2%  → double beep
  Move >5%  → triple high beep + row flashes orange
  Move >10% → rapid triple beep + row flashes red/white

CSV self-cleaning:
  • Max 500 rows kept
  • DEAD/FADING entries older than 48 h are pruned automatically
  • Each symbol appears only once (latest entry wins)
  • 30-minute per-symbol cooldown before re-alerting

Historical bar data:
  Stocks  → Massive.com REST API (5-min OHLC, no Claude involvement at runtime)
            Falls back to yfinance if massive_api_key is blank.
  Crypto  → Kraken / Coinbase via CCXT (unchanged)

Run:
    python BreakoutScanner.py           # GUI (tkinter) or console fallback
    python BreakoutScanner.py --console # Force console mode
"""

import csv
import json
import logging
import os
import queue
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from enum import Enum
from typing import Any, Dict, List, Optional
from breakout_sender import BreakoutSender
from scanners.gap_watchlist import GapWatchlist, GapSetup
from scanners.catalyst_checker import CatalystChecker
from scanners.volume_surge_scanner import VolumeSurgeTracker


BASE_DIR = Path(__file__).resolve().parent

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    import requests as _requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import yfinance as yf
    import logging as _logging
    _logging.getLogger("yfinance").setLevel(_logging.CRITICAL)
    _logging.getLogger("yfinance.base").setLevel(_logging.CRITICAL)
    _logging.getLogger("yfinance.utils").setLevel(_logging.CRITICAL)
    _logging.getLogger("peewee").setLevel(_logging.CRITICAL)
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

try:
    import alpaca_trade_api as tradeapi
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False

try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False

try:
    import tkinter as tk
    from tkinter import ttk
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False

try:
    import winsound
    WINSOUND_AVAILABLE = True
except ImportError:
    WINSOUND_AVAILABLE = False

# pygame-based audio — plays through actual sound card, not PC speaker
try:
    from alert_sound import init_audio as _init_audio, play_alert as _play_alert
    ALERT_SOUND_AVAILABLE = True
except ImportError:
    ALERT_SOUND_AVAILABLE = False

# geometric chart pattern detection
try:
    from chart_patterns import score_patterns as _score_patterns
    PATTERNS_AVAILABLE = True
except ImportError:
    PATTERNS_AVAILABLE = False

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "scanner.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

CSV_FILE            = BASE_DIR / "signals_log.csv"
CSV_MAX_ROWS        = 500
CSV_PRUNE_AGE       = 48        # hours
ALERT_COOLDOWN_SECS = 1800      # 30 min per symbol
WATCHLIST_DIR       = BASE_DIR / "watchlist"
REPORTS_DIR         = BASE_DIR / "reports"
TEMP_STOCKS_FILE    = WATCHLIST_DIR / "scanned_stocks.txt"
TEMP_CRYPTO_FILE    = WATCHLIST_DIR / "scanned_crypto.txt"

MASSIVE_BASE        = "https://api.massive.com"

# ── Fernet decrypt helper (mirrors config.py — used for scanner_config.json keys) ──
# Sensitive keys stored as Fernet tokens start with "gAAAAA".
# Plaintext values (not yet migrated) are returned unchanged — migration-safe.
try:
    import keyring as _keyring
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken
    _scanner_fernet_raw = _keyring.get_password("trading_bot_v2", "fernet_key")
    _SCANNER_BASE_KEY   = _scanner_fernet_raw.encode() if _scanner_fernet_raw else None
except Exception:
    _SCANNER_BASE_KEY   = None

# Keys inside scanner_config.json that hold sensitive secrets
_SCANNER_SENSITIVE_KEYS = {
    "massive_api_key",
    "alpaca_api_key", "alpaca_secret_key",
    "kraken_api_key", "kraken_api_secret",
    "coinbase_api_key", "coinbase_api_secret",
    "bot_api_key",
    "email_password",
    "alphavantage_api_key",
}

# Maps scanner_config.json key names → (service, key_name) in the bot DB.
# When a key is blank after JSON/Fernet decryption the DB is tried as a fallback.
_SCANNER_DB_MAP: Dict[str, tuple] = {
    "alpaca_api_key":       ("alpaca",        "api_key"),
    "alpaca_secret_key":    ("alpaca",        "secret_key"),
    "kraken_api_key":       ("kraken",        "api_key"),
    "kraken_api_secret":    ("kraken",        "secret_key"),
    "coinbase_api_key":     ("coinbase",      "api_key"),
    "coinbase_api_secret":  ("coinbase",      "secret_key"),
    "massive_api_key":      ("massive",       "api_key"),
    "email_password":       ("email",         "password"),
    "alphavantage_api_key": ("alphavantage",  "api_key"),
    "bot_api_key":          ("trading_bot",   "bot_api_key"),
}

def _encrypt_cfg(val: str) -> str:
    """
    Encrypt a plaintext secret for writing back to scanner_config.json.
    - Already-encrypted (starts "gAAAAA") → returned unchanged (no double-encrypt).
    - No Fernet key in Credential Manager → returns val unchanged (safe degradation).
    - Empty string → returned unchanged.
    """
    if not val or not isinstance(val, str):
        return val
    if val.startswith("gAAAAA"):
        return val   # already encrypted — don't double-encrypt
    if not _SCANNER_BASE_KEY:
        return val   # no key available — preserve as-is rather than blank
    try:
        return _Fernet(_SCANNER_BASE_KEY).encrypt(val.encode()).decode()
    except Exception as e:
        logger.warning(f"[CONFIG] Failed to encrypt key: {e}")
        return val   # encryption failed — return plaintext rather than lose the value


def _decrypt_cfg(val: str) -> str:
    """
    Decrypt a Fernet-encrypted value read from scanner_config.json.
    - Empty / plaintext → returned unchanged.
    - Fernet token (starts "gAAAAA") → decrypted.
    - Encrypted but no key in Credential Manager → RuntimeError.
    """
    if not val or not isinstance(val, str):
        return val
    val = val.strip()
    if not val.startswith("gAAAAA"):
        return val                               # plaintext — not yet encrypted
    if not _SCANNER_BASE_KEY:
        raise RuntimeError(
            "Encrypted key found in scanner_config.json but the Fernet key is "
            "not in Windows Credential Manager.  "
            "Run: python Scripts\\store_fernet_key.py"
        )
    try:
        return _Fernet(_SCANNER_BASE_KEY).decrypt(val.encode()).decode().strip()
    except _InvalidToken:
        raise RuntimeError(
            f"Failed to decrypt a scanner_config.json key — token corrupt or "
            f"wrong Fernet key.  First 20 chars: {val[:20]}…"
        )


def _db_fallback(scanner_key: str) -> str:
    """
    Look up a blank scanner key in the trading bot's encrypted api_keys DB table.
    Uses the same Fernet key as the rest of the bot — no extra setup required.
    Returns empty string if the DB is unavailable or the key is not stored.
    """
    mapping = _SCANNER_DB_MAP.get(scanner_key)
    if not mapping:
        return ""
    service, key_name = mapping
    try:
        import sqlite3 as _sq
        db_path = BASE_DIR / "data" / "trading_bot.db"
        if not db_path.exists():
            return ""
        conn = _sq.connect(str(db_path))
        row  = conn.execute(
            "SELECT value_enc FROM api_keys WHERE service=? AND key_name=?",
            (service, key_name)
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return ""
        enc = row[0]
        # Decrypt: plain: prefix (pre-Fernet installs) or Fernet token
        if enc.startswith("plain:"):
            return enc[6:]
        if not _SCANNER_BASE_KEY:
            logger.warning(
                f"[CONFIG] DB has '{service}/{key_name}' but Fernet key not in keyring — "
                "run Scripts/setup_encryption.py"
            )
            return ""
        return _Fernet(_SCANNER_BASE_KEY).decrypt(enc.encode()).decode().strip()
    except Exception as e:
        logger.debug(f"[CONFIG] DB fallback failed for '{scanner_key}': {e}")
        return ""


def _unique_keep_order(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        value = str(value or "").strip().upper()
        if not value or value.startswith("#") or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _read_symbol_file(path: Path) -> List[str]:
    try:
        if not path.exists():
            return []
        return _unique_keep_order(path.read_text().splitlines())
    except Exception as e:
        logger.warning(f"Could not read watchlist file {path}: {e}")
        return []


def _latest_scan_csv() -> Optional[Path]:
    try:
        files = list(REPORTS_DIR.glob("*market_scan.csv"))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)
    except Exception as e:
        logger.warning(f"Could not inspect scan reports: {e}")
        return None


def _read_scan_csv(asset_class: str) -> List[str]:
    path = _latest_scan_csv()
    if not path:
        return []
    try:
        symbols = []
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                row_asset = (row.get("AssetClass") or row.get("asset_class") or "").strip().lower()
                ticker = (row.get("Ticker") or row.get("Symbol") or row.get("symbol") or "").strip().upper()
                if not ticker:
                    continue
                if row_asset and row_asset != asset_class.lower():
                    continue
                if asset_class.lower() == "crypto" and "/" not in ticker:
                    ticker = f"{ticker}/USD"
                symbols.append(ticker)
        loaded = _unique_keep_order(symbols)
        if loaded:
            logger.info(f"[WATCHLIST CSV] Loaded {len(loaded)} {asset_class} symbols from {path}")
        return loaded
    except Exception as e:
        logger.warning(f"Could not read scan CSV {path}: {e}")
        return []


def load_injected_stock_symbols() -> List[str]:
    symbols = _read_symbol_file(TEMP_STOCKS_FILE)
    if not symbols:
        symbols = _read_scan_csv("Stock")
    return _unique_keep_order(symbols)


def load_injected_crypto_symbols() -> List[str]:
    symbols = _read_symbol_file(TEMP_CRYPTO_FILE)
    if not symbols:
        symbols = _read_scan_csv("Crypto")
    return _unique_keep_order(
        s if "/" in s else f"{s}/USD"
        for s in symbols
    )

# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_CONFIG = {
    "stock_market_open":  "06:30",
    "stock_market_close": "13:00",
    "crypto_24_7":        True,
    "scan_interval_seconds": 30,
    "hot_symbol_scan_seconds": 5,
    "top_stocks_count":  100,
    "top_cryptos_count": 100,
    "enable_sound_alert":  True,
    "enable_visual_alert": True,
    "enable_email_alert":  False,
    "email_smtp_server":   "smtp.gmail.com",
    "email_smtp_port":     587,
    "email_from":          "",
    "email_to":            "",
    "email_password":      "",
    "enable_auto_trade":          False,
    "auto_trade_without_confirm": False,
    "broker":                     "alpaca",
    "alpaca_api_key":    "",
    "alpaca_secret_key": "",
    "alpaca_base_url":   "https://paper-api.alpaca.markets",
    "coinbase_api_key":    "",
    "coinbase_api_secret": "",
    "kraken_api_key":    "",
    "kraken_api_secret": "",
    "massive_api_key":   "",
    "default_trade_amount_usd": 1000,
    "max_position_size_usd":    5000,
    "stop_loss_percent":        3,
    "take_profit_percent":      20,
    "run_days":      ["Monday","Tuesday","Wednesday","Thursday","Friday"],
    "exclude_dates": [],
    "alert_sound_frequency":   800,
    "alert_sound_duration_ms": 1000,
    "watchlist_min_score":  0.40,
    "momentum_gate_pct":    0.80,
    "momentum_gate_scans":  3,
    "escalate_2pct":        2.0,
    "escalate_5pct":        5.0,
    "escalate_10pct":       10.0,
    "bot_min_escalation":   1,
}

# =============================================================================
# DATA CLASSES
# =============================================================================

class AlertType(Enum):
    SOUND  = "sound"
    VISUAL = "visual"
    EMAIL  = "email"
    TRADE  = "trade"

class AssetType(Enum):
    STOCK  = "stock"
    CRYPTO = "crypto"

class SignalPhase(Enum):
    WATCHLIST = "watchlist"
    ALERTED   = "alerted"
    DEAD      = "dead"

@dataclass
class PriceData:
    symbol:     str
    price:      float
    open_price: float
    high:       float
    low:        float
    volume:     float
    timestamp:  datetime
    asset_type: AssetType

    @property
    def daily_change_pct(self) -> float:
        if self.open_price == 0:
            return 0.0
        return ((self.price - self.open_price) / self.open_price) * 100

@dataclass
class BreakoutSignal:
    symbol:             str
    asset_type:         AssetType
    current_price:      float
    predicted_move_pct: float
    confidence:         float
    volume_spike:       float
    momentum_score:     float
    pattern_detected:   str
    timestamp:          datetime
    broker:             str         = "UNKNOWN"
    phase:              SignalPhase = SignalPhase.WATCHLIST
    entry_price:        float       = 0.0
    scan_count:         int         = 0
    best_move_pct:      float       = 0.0
    price_history:      List[float] = field(default_factory=list)
    volume_history:     List[float] = field(default_factory=list)
    # Scanner-owned analysis — set explicitly at signal creation time
    direction:          str         = ""          # "long" | "short" — never silent-default
    enrichment:         Dict[str, Any] = field(default_factory=dict)
    # Escalation tracking — populated by EscalationTracker
    bars_since_breakout:        int   = 0
    breakout_level:             float = 0.0
    distance_from_breakout_pct: float = 0.0

@dataclass
class Alert:
    signal:       BreakoutSignal
    alert_type:   AlertType
    message:      str
    timestamp:    datetime
    escalation:   int  = 0
    acknowledged: bool = False

# =============================================================================
# SIGNAL CLASSIFICATION
# =============================================================================

def classify_signal(signal: BreakoutSignal) -> dict:
    score     = signal.confidence
    direction = signal_direction(signal).upper()   # explicit direction wins; momentum fallback only
    if "strong" in signal.pattern_detected:
        stage = "CONFIRMED"
    elif "early" in signal.pattern_detected:
        stage = "EARLY"
    else:
        stage = "WATCHLIST"
    poc = min(95, score * 100)
    if signal.volume_spike > 3.0:
        poc = min(95, poc + 10)
    return {"direction": direction, "stage": stage, "poc": poc}

def move_pct(entry: float, current: float) -> float:
    if entry == 0:
        return 0.0
    return ((current - entry) / entry) * 100.0

def favorable_move_pct(direction: str, raw_move_pct: float) -> float:
    """Normalize move_pct so positive always means good for the trade direction.
    Longs:  price went up   → positive = winning.
    Shorts: price went down → negative raw becomes positive favorable.
    """
    if str(direction).lower() == "short":
        return -raw_move_pct
    return raw_move_pct


def resolved_direction(direction: str = "", momentum_score: float = 0.0) -> str:
    """
    Resolve trade direction with explicit priority order:
      1. explicit direction field ("long" / "short")
      2. momentum_score fallback (only when direction is absent/invalid)
    Never infer direction from momentum alone when the signal already carries one.
    """
    d = str(direction or "").strip().lower()
    if d in ("long", "short"):
        return d
    try:
        return "short" if float(momentum_score) < -0.5 else "long"
    except Exception:
        return "long"


def signal_direction(signal) -> str:
    """Convenience wrapper — reads direction from a BreakoutSignal."""
    return resolved_direction(
        getattr(signal, "direction", ""),
        getattr(signal, "momentum_score", 0.0),
    )

# =============================================================================
# CSV  (self-cleaning)
# =============================================================================

CSV_FIELDS = ["Symbol","Broker","Phase","Score","Confidence","Date","Time",
              "EntryPrice","t_5m","t_15m","t_1h","t_4h","t_24h",
              "momentum","move_pct","best_move_pct"]

def _load_csv() -> List[dict]:
    if not CSV_FILE.is_file():
        return []
    with CSV_FILE.open("r", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for col in CSV_FIELDS:
            if col not in r:
                r[col] = ""
    return rows

def _save_csv(rows: List[dict]):
    with CSV_FILE.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

def _prune_csv(rows: List[dict]) -> List[dict]:
    cutoff = datetime.now() - timedelta(hours=CSV_PRUNE_AGE)
    kept = []
    for r in rows:
        phase = r.get("Phase","").upper()
        try:
            ts = datetime.strptime(r["Date"] + " " + r["Time"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            kept.append(r); continue
        if phase in ("DEAD","FADING") and ts < cutoff:
            continue
        kept.append(r)
    if len(kept) > CSV_MAX_ROWS:
        dead   = [r for r in kept if r.get("Phase","").upper() in ("DEAD","FADING")]
        live   = [r for r in kept if r.get("Phase","").upper() not in ("DEAD","FADING")]
        remove = len(kept) - CSV_MAX_ROWS
        dead   = dead[remove:]
        kept   = live + dead
    if len(kept) > CSV_MAX_ROWS:
        kept = kept[-CSV_MAX_ROWS:]
    return kept

def log_signal_to_csv(signal: BreakoutSignal):
    rows = _load_csv()
    rows = [r for r in rows if r.get("Symbol") != signal.symbol]
    rows.append({
        "Symbol":        signal.symbol,
        "Broker":        signal.broker,
        "Phase":         signal.phase.value.upper(),
        "Score":         round(signal.predicted_move_pct, 2),
        "Confidence":    round(signal.confidence * 100, 2),
        "Date":          signal.timestamp.strftime("%Y-%m-%d"),
        "Time":          signal.timestamp.strftime("%H:%M:%S"),
        "EntryPrice":    signal.entry_price,
        "t_5m": "", "t_15m": "", "t_1h": "",
        "t_4h": "", "t_24h": "",
        "momentum":      "",
        "move_pct":      "",
        "best_move_pct": "",
    })
    rows = _prune_csv(rows)
    _save_csv(rows)

def update_signal_tracking(scanner):
    rows = _load_csv()
    if not rows:
        return
    updated = False
    now     = datetime.now()
    for row in rows:
        sym = row.get("Symbol","")
        try:
            if "/" in sym:
                if not scanner.crypto_fetcher.exchange:
                    continue
                ticker = scanner.crypto_fetcher.exchange.fetch_ticker(sym)
                price  = ticker["last"]
            else:
                h     = scanner.stock_fetcher.get_historical_data(sym)
                price = h[-1].price if h else None
            if not price:
                continue
            try:
                ts = datetime.strptime(row["Date"]+" "+row["Time"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            elapsed = (now - ts).total_seconds()

            def fill(col, secs):
                nonlocal updated
                if row.get(col,"") == "" and elapsed >= secs:
                    row[col] = round(price, 6)
                    updated  = True

            fill("t_5m",300); fill("t_15m",900)
            fill("t_1h",3600); fill("t_4h",14400); fill("t_24h",86400)

            ep   = float(row.get("EntryPrice") or row.get("Price") or price)
            mpct = move_pct(ep, price)
            row["move_pct"] = round(mpct, 2)

            # Direction-aware favorable move tracking
            direction = ""
            tracked = getattr(getattr(scanner, "escalation", None), "_tracked", {})
            entry = tracked.get(sym)
            if entry:
                direction = getattr(entry.get("signal"), "direction", "") or ""
            if not direction:
                wl = getattr(scanner, "watchlist", None)
                wl_sig = wl.get(sym) if (wl and hasattr(wl, "get")) else None
                if wl_sig:
                    direction = getattr(wl_sig, "direction", "") or ""
            fav = favorable_move_pct(direction, mpct)

            try:
                prev_best = float(row.get("best_move_pct") or 0)
                if fav > prev_best:
                    row["best_move_pct"] = round(fav, 2)
                    updated = True
            except Exception:
                pass

            if   fav >= 5.0:  row["momentum"] = "STRONG"
            elif fav >= 2.0:  row["momentum"] = "MOVING"
            elif fav >= 0.5:  row["momentum"] = "WEAK"
            elif fav <= -3.0: row["momentum"] = "FADING"
            else:             row["momentum"] = "DEAD"
            updated = True
        except Exception:
            continue

    if updated:
        rows = _prune_csv(rows)
        _save_csv(rows)

# =============================================================================
# CONFIGURATION MANAGER
# =============================================================================

class ConfigManager:
    def __init__(self, config_path: str = "scanner_config.json"):
        path = Path(config_path)
        self.config_path = path if path.is_absolute() else BASE_DIR / path
        self.config      = {}
        self.load()

    def load(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "r") as f:
                stored = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(stored)
            # Decrypt any Fernet-encrypted sensitive keys in-place,
            # then fall back to the bot DB for any that are still blank.
            for key in _SCANNER_SENSITIVE_KEYS:
                if key in merged and merged[key]:
                    try:
                        merged[key] = _decrypt_cfg(str(merged[key]))
                    except RuntimeError as e:
                        logger.error(f"[CONFIG] Key decrypt failed for '{key}': {e}")
                        merged[key] = ""   # blank it out — safer than crashing
                # If still blank after decryption attempt, try the bot DB
                if not merged.get(key):
                    db_val = _db_fallback(key)
                    if db_val:
                        merged[key] = db_val
                        logger.info(f"[CONFIG] '{key}' loaded from bot DB (scanner_config blank).")
            self.config = merged
            logger.info(f"Config loaded from {self.config_path}")
        else:
            self.config = DEFAULT_CONFIG.copy()
            self.save()
            logger.info(f"Default config created at {self.config_path}")

    def save(self):
        # Re-encrypt sensitive keys before writing so secrets are never stored plaintext.
        # Keys that were originally encrypted and whose Fernet key is unavailable
        # are preserved as-is (already ciphertext) rather than written as plaintext.
        to_write = dict(self.config)
        for key in _SCANNER_SENSITIVE_KEYS:
            if key in to_write and to_write[key]:
                to_write[key] = _encrypt_cfg(str(to_write[key]))
        with open(self.config_path, "w") as f:
            json.dump(to_write, f, indent=2)

    def get(self, key: str, default=None):
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        self.config[key] = value
        self.save()

    def is_market_open(self, asset_type: AssetType) -> bool:
        now = datetime.now()
        if now.strftime("%Y-%m-%d") in self.config.get("exclude_dates", []):
            return False
        if asset_type == AssetType.CRYPTO and self.config.get("crypto_24_7", True):
            return True
        if now.strftime("%A") not in self.config.get("run_days", []):
            return False
        if asset_type == AssetType.STOCK:
            oh, om = map(int, self.config.get("stock_market_open",  "06:30").split(":"))
            ch, cm = map(int, self.config.get("stock_market_close", "13:00").split(":"))
            return dt_time(oh, om) <= now.time() <= dt_time(ch, cm)
        return False

# =============================================================================
# MASSIVE.COM DATA FETCHER
# =============================================================================

class MassiveDataFetcher:
    _cache: Dict[str, tuple] = {}
    CACHE_TTL_SECS = 240

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = _requests.Session() if REQUESTS_AVAILABLE else None
        if self.session:
            self.session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Accept":        "application/json",
            })

    def get_historical_data(self, symbol: str) -> List[PriceData]:
        if not self.session or not self.api_key:
            return []
        cached = self._cache.get(symbol)
        if cached:
            fetched_at, bars = cached
            if (datetime.now() - fetched_at).total_seconds() < self.CACHE_TTL_SECS:
                return bars
        try:
            to_date   = datetime.now().strftime("%Y-%m-%d")
            from_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
            url       = (f"{MASSIVE_BASE}/v2/aggs/ticker/{symbol}"
                         f"/range/5/minute/{from_date}/{to_date}")
            resp = self.session.get(url, params={
                "adjusted": "true",
                "sort":     "asc",
                "limit":    100,
            }, timeout=5)
            if resp.status_code == 403:
                logger.warning(f"Massive.com: not authorized for {symbol} bars")
                return []
            if resp.status_code != 200:
                logger.debug(f"Massive.com {symbol}: HTTP {resp.status_code}")
                return []
            data    = resp.json()
            results = data.get("results", [])
            if not results:
                return []
            bars = [
                PriceData(
                    symbol=symbol, price=float(r["c"]), open_price=float(r["o"]),
                    high=float(r["h"]), low=float(r["l"]), volume=float(r["v"]),
                    timestamp=datetime.fromtimestamp(r["t"]/1000),
                    asset_type=AssetType.STOCK,
                )
                for r in results
            ]
            self._cache[symbol] = (datetime.now(), bars)
            return bars
        except Exception as e:
            logger.debug(f"Massive.com fetch error {symbol}: {e}")
            return []

# =============================================================================
# DATA FETCHERS
# =============================================================================

class StockDataFetcher:
    FALLBACK = [
        "AAPL","MSFT","GOOGL","AMZN","TSLA","META","NVDA","JPM","V","JNJ",
        "WMT","UNH","PG","HD","MA","BAC","ABBV","PFE","KO","AVGO","PEP",
        "LLY","COST","TMO","CSCO","ACN","CVX","MRK","NFLX","QCOM","TXN",
        "AMD","INTC","CRM","AMGN","IBM","GE","HON","SBUX","GS","CAT","BLK",
        "PANW","SNOW","PLTR","COIN","SHOP","CRWD","DDOG","NET","ZS","OKTA",
        "SMCI","ARM","MSTR","IONQ","SOUN","BBAI","ASTS","NVAX","MRNA",
    ]

    def __init__(self, config: ConfigManager):
        self.config  = config
        self.api     = None
        massive_key  = config.get("massive_api_key", "")
        self.massive = MassiveDataFetcher(massive_key) if massive_key else None
        if self.massive:
            logger.info("Massive.com stock bar fetcher active")
        else:
            logger.info("Massive.com key not set — using yfinance for stock bars")
        if ALPACA_AVAILABLE:
            try:
                self.api = tradeapi.REST(
                    config.get("alpaca_api_key"), config.get("alpaca_secret_key"),
                    config.get("alpaca_base_url"), api_version="v2",
                )
                logger.info("Alpaca API initialized")
            except Exception as e:
                logger.error(f"Alpaca init failed: {e}")

    def _stock_universe(self) -> List[str]:
        injected = load_injected_stock_symbols()
        symbols = _unique_keep_order([*injected, *self.FALLBACK])
        if injected:
            logger.info(f"[WATCHLIST] Loaded {len(injected)} injected stock symbols: {injected}")
        return symbols

    def get_top_movers(self, count: int = 100) -> List[PriceData]:
        stocks = []
        if self.api:
            try:
                assets    = self.api.list_assets(status="active", asset_class="us_equity")
                symbols   = _unique_keep_order([*load_injected_stock_symbols(),
                                                 *[a.symbol for a in assets[:500]]])
                snapshots = self.api.get_snapshots(symbols)
                for sym, snap in snapshots.items():
                    if snap and snap.daily_bar:
                        b = snap.daily_bar
                        stocks.append(PriceData(sym, b.c, b.o, b.h, b.l, b.v,
                                                datetime.now(), AssetType.STOCK))
            except Exception as e:
                logger.error(f"Alpaca snapshot error: {e}")
        if not stocks and YFINANCE_AVAILABLE:
            stocks = self._yfinance_movers()
        stocks.sort(key=lambda x: x.daily_change_pct, reverse=True)
        injected = set(load_injected_stock_symbols())
        pinned = [s for s in stocks if s.symbol in injected]
        others = [s for s in stocks if s.symbol not in injected]
        return pinned + others[:count]

    def _yfinance_movers(self) -> List[PriceData]:
        stocks = []
        try:
            symbols = self._stock_universe()
            tickers = yf.download(symbols, period="1d", interval="1m",
                                  progress=False, group_by="ticker")
            for sym in symbols:
                if sym.startswith("$") or sym.startswith("^"):
                    continue
                try:
                    df = tickers[sym].dropna()
                    if df.empty: continue
                    stocks.append(PriceData(
                        sym, float(df["Close"].iloc[-1]), float(df["Open"].iloc[0]),
                        float(df["High"].max()), float(df["Low"].min()),
                        float(df["Volume"].sum()), datetime.now(), AssetType.STOCK,
                    ))
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"yfinance error: {e}")
        return stocks

    def get_historical_data(self, symbol: str, period: str = "5d",
                             interval: str = "5m") -> List[PriceData]:
        if self.massive:
            bars = self.massive.get_historical_data(symbol)
            if bars:
                return bars
        if not YFINANCE_AVAILABLE:
            return []
        # Skip index/invalid symbols — yfinance can't fetch $-prefixed tickers
        if symbol.startswith("$") or symbol.startswith("^"):
            return []
        try:
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=True).dropna()
            result = []
            for ts, r in df.iterrows():
                try:
                    result.append(PriceData(
                        symbol,
                        float(r["Close"].iloc[0]) if hasattr(r["Close"], "iloc") else float(r["Close"]),
                        float(r["Open"].iloc[0])  if hasattr(r["Open"],  "iloc") else float(r["Open"]),
                        float(r["High"].iloc[0])  if hasattr(r["High"],  "iloc") else float(r["High"]),
                        float(r["Low"].iloc[0])   if hasattr(r["Low"],   "iloc") else float(r["Low"]),
                        float(r["Volume"].iloc[0]) if hasattr(r["Volume"],"iloc") else float(r["Volume"]),
                        ts.to_pydatetime(), AssetType.STOCK
                    ))
                except Exception:
                    continue
            return result
        except Exception as e:
            logger.error(f"yfinance history error {symbol}: {e}")
            return []


# ---------------------------------------------------------------------------
# CoinGecko — real-time aggregated price lookup (no API key required)
# ---------------------------------------------------------------------------
_COINGECKO_IDS: Dict[str, str] = {
    "BTC":   "bitcoin",       "ETH":   "ethereum",
    "SOL":   "solana",        "ADA":   "cardano",
    "MATIC": "matic-network", "DOT":   "polkadot",
    "AVAX":  "avalanche-2",   "LINK":  "chainlink",
    "LTC":   "litecoin",      "XRP":   "ripple",
    "DOGE":  "dogecoin",      "TRX":   "tron",
    "UNI":   "uniswap",       "ATOM":  "cosmos",
    "XLM":   "stellar",       "ALGO":  "algorand",
    "NEAR":  "near",          "FTM":   "fantom",
    "SAND":  "the-sandbox",   "MANA":  "decentraland",
    "SHIB":  "shiba-inu",     "PEPE":  "pepe",
    "ARB":   "arbitrum",      "OP":    "optimism",
}

_COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"


def _coingecko_price(symbol: str) -> Optional[float]:
    """Return current USD price from CoinGecko for a /USD watchlist symbol.
    Returns None on any error so callers fall back to ccxt."""
    if not REQUESTS_AVAILABLE:
        return None
    try:
        base  = symbol.split("/")[0].upper()
        cg_id = _COINGECKO_IDS.get(base, base.lower())
        resp  = _requests.get(
            _COINGECKO_PRICE_URL,
            params={"ids": cg_id, "vs_currencies": "usd"},
            timeout=5,
        )
        price = resp.json().get(cg_id, {}).get("usd")
        return float(price) if price else None
    except Exception:
        return None


class CryptoDataFetcher:
    def __init__(self, config: ConfigManager):
        self.config        = config
        self.exchange      = None
        self.exchange_name = "UNKNOWN"
        self._init()

    def _init(self):
        if not CCXT_AVAILABLE:
            return
        # Load ALL available exchanges, not just the first one that succeeds.
        # self.exchange      = primary exchange (first to load) — kept for
        #                      backward compatibility with price-fetch calls.
        # self.exchanges     = dict of {name: exchange} for all loaded exchanges.
        self.exchanges = {}
        for name, kwargs in [
            ("kraken",   {"apiKey": self.config.get("kraken_api_key"),
                          "secret": self.config.get("kraken_api_secret"),
                          "enableRateLimit": True, "timeout": 10000}),
            ("coinbase", {"apiKey": self.config.get("coinbase_api_key"),
                          "secret": self.config.get("coinbase_api_secret"),
                          "enableRateLimit": True}),
        ]:
            try:
                exc = getattr(ccxt, name)(kwargs)
                exc.load_markets()
                self.exchanges[name.upper()] = exc
                if self.exchange is None:
                    self.exchange      = exc
                    self.exchange_name = name.upper()
                logger.info(f"{name.capitalize()} exchange initialized")
            except Exception as e:
                logger.warning(f"{name} init failed: {e}")

    def get_top_movers(self, count: int = 100) -> List[PriceData]:
        """Scan ALL loaded exchanges. Each PriceData is tagged with its
        source exchange via a custom attribute ._exchange_name so that
        _scan_cryptos can set the correct broker on the signal."""
        cryptos = []
        exchanges = getattr(self, "exchanges", {})
        if not exchanges:
            exchanges = {self.exchange_name: self.exchange} if self.exchange else {}
        seen_symbols = set()   # de-duplicate across exchanges (first exchange wins)
        for exch_name, exc in exchanges.items():
            if not exc:
                continue
            try:
                for sym, t in exc.fetch_tickers().items():
                    if "/USD" not in sym and "/USDT" not in sym:
                        continue
                    if sym in seen_symbols:
                        continue
                    try:
                        chg = t.get("percentage", 0) or 0
                        if chg == 0: continue
                        op  = t["open"] if t.get("open") else t["last"] / (1 + chg/100)
                        pd_obj = PriceData(
                            sym, t["last"], op,
                            t.get("high") or t["last"], t.get("low") or t["last"],
                            t.get("quoteVolume") or t.get("baseVolume", 0),
                            datetime.now(), AssetType.CRYPTO,
                        )
                        pd_obj._exchange_name = exch_name   # tag with source
                        cryptos.append(pd_obj)
                        seen_symbols.add(sym)
                    except Exception:
                        continue
            except Exception as e:
                logger.error(f"Crypto fetch error ({exch_name}): {e}")
        self._append_injected_crypto(cryptos, exchanges, seen_symbols)
        cryptos.sort(key=lambda x: abs(x.daily_change_pct), reverse=True)
        injected = set(load_injected_crypto_symbols())
        pinned = [c for c in cryptos if c.symbol in injected]
        others = [c for c in cryptos if c.symbol not in injected]
        return pinned + others[:count]

    def _append_injected_crypto(self, cryptos: List[PriceData],
                                exchanges: Dict[str, Any],
                                seen_symbols: set):
        injected = load_injected_crypto_symbols()
        if not injected:
            return
        logger.info(f"[WATCHLIST] Loaded {len(injected)} injected crypto symbols: {injected}")
        for symbol in injected:
            if symbol in seen_symbols:
                continue

            # Try CoinGecko first for an accurate aggregated price
            cg_price = _coingecko_price(symbol)
            if cg_price:
                pd_obj = PriceData(
                    symbol, cg_price, cg_price,
                    cg_price, cg_price, 0,
                    datetime.now(), AssetType.CRYPTO,
                )
                pd_obj._exchange_name = "COINGECKO"
                cryptos.append(pd_obj)
                seen_symbols.add(symbol)
                continue

            # Fallback: try ccxt exchanges
            for exch_name, exc in exchanges.items():
                if not exc or symbol not in getattr(exc, "symbols", []):
                    continue
                try:
                    ticker = exc.fetch_ticker(symbol)
                    last = ticker.get("last") or ticker.get("close")
                    if not last:
                        continue
                    chg = ticker.get("percentage", 0) or 0
                    op = ticker.get("open") or (last / (1 + chg / 100) if chg else last)
                    pd_obj = PriceData(
                        symbol, last, op,
                        ticker.get("high") or last, ticker.get("low") or last,
                        ticker.get("quoteVolume") or ticker.get("baseVolume", 0),
                        datetime.now(), AssetType.CRYPTO,
                    )
                    pd_obj._exchange_name = exch_name
                    cryptos.append(pd_obj)
                    seen_symbols.add(symbol)
                    break
                except Exception as e:
                    logger.debug(f"Injected crypto fetch skip {symbol} on {exch_name}: {e}")

    def get_historical_data(self, symbol: str, timeframe: str = "5m",
                            limit: int = 100,
                            exchange=None) -> List[PriceData]:
        """
        Fetch OHLCV history for a crypto symbol.
        `exchange` may be a CCXT exchange instance or name string.
        Falls back to the primary exchange if the supplied one can't fetch the symbol.
        Returns [] if no exchange can provide data.
        """
        # Resolve to a CCXT exchange instance
        exc = None
        if exchange is not None:
            if isinstance(exchange, str):
                # Accept exchange name — look it up among known instances
                exc = self._resolve_exchange_by_name(exchange)
            else:
                exc = exchange   # already a CCXT instance
        if exc is None:
            exc = self.exchange   # primary exchange fallback

        if exc is None:
            return []

        # If primary was tried and symbol not listed, nothing to do
        try:
            return [
                PriceData(symbol, c, o, h, l, v,
                          datetime.fromtimestamp(ts/1000), AssetType.CRYPTO)
                for ts, o, h, l, c, v in exc.fetch_ohlcv(symbol, timeframe, limit=limit)
            ]
        except Exception:
            # Supplied exchange failed — try primary if different
            if exc is not self.exchange and self.exchange:
                try:
                    return [
                        PriceData(symbol, c, o, h, l, v,
                                  datetime.fromtimestamp(ts/1000), AssetType.CRYPTO)
                        for ts, o, h, l, c, v in self.exchange.fetch_ohlcv(
                            symbol, timeframe, limit=limit)
                    ]
                except Exception as e:
                    logger.debug(f"Crypto history skip {symbol} (both exchanges): {e}")
            return []

    def _resolve_exchange_by_name(self, name: str):
        """Return the CCXT exchange instance that matches `name`, or None."""
        if not CCXT_AVAILABLE:
            return None
        name_upper = name.upper()
        # Walk any known exchange instances stored on the fetcher
        for attr in ("exchange", "exchanges"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            if isinstance(obj, dict):
                # Dict of {name: instance}
                match = obj.get(name_upper) or obj.get(name.lower())
                if match:
                    return match
            elif hasattr(obj, "id") and obj.id.upper() == name_upper:
                return obj
        return None

# =============================================================================
# BREAKOUT DETECTION ENGINE
# =============================================================================

class BreakoutDetector:
    def __init__(self, config: ConfigManager):
        self.config         = config
        self.price_history  = {}
        self.volume_history = {}
        self.history_length = 200

    def update_history(self, pd: PriceData):
        s = pd.symbol
        if s not in self.price_history:
            self.price_history[s]  = deque(maxlen=self.history_length)
            self.volume_history[s] = deque(maxlen=self.history_length)
        price = self._num(pd.price, 0.0)
        volume = self._num(pd.volume, 0.0)
        if price <= 0:
            return
        self.price_history[s].append(price)
        self.volume_history[s].append(volume)

    def _num(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def score(self, pd: PriceData) -> float:
        s = pd.symbol
        self.update_history(pd)
        if len(self.price_history[s]) < 25:
            return 0.0
        prices  = [self._num(x, 0.0) for x in self.price_history[s]]
        volumes = [self._num(x, 0.0) for x in self.volume_history[s]]
        sc = 0.0
        if self._compressing(prices):          sc += 0.20
        if self._early_volume(volumes, prices): sc += 0.25
        if self._expanding(prices):            sc += 0.20
        if self._near_vwap(prices, volumes):   sc += 0.15
        if self._momentum(prices) > 0.5:       sc += 0.15
        if self._not_overextended(prices):     sc += 0.05
        vs = self._vol_spike(volumes)
        if vs >= 3.0:   sc = min(1.0, sc + 0.10)
        elif vs >= 2.0: sc = min(1.0, sc + 0.05)

        # Chart pattern confirmation — adds up to +0.20 when a strong pattern aligns
        if PATTERNS_AVAILABLE:
            try:
                pat = _score_patterns(prices, volumes)
                if pat["best_confidence"] >= 0.55:
                    sc = min(1.0, sc + pat["pattern_score"] * 0.20)
                    pd._pattern     = pat["best_pattern"]
                    pd._pattern_dir = pat["direction"]
            except Exception:
                pass

        return round(sc, 3)

    def vol_spike(self, pd: PriceData) -> float:
        v = self.volume_history.get(pd.symbol)
        return self._vol_spike(list(v)) if v else 1.0

    def momentum_val(self, pd: PriceData) -> float:
        p = self.price_history.get(pd.symbol)
        return self._momentum(list(p)) if p else 0.0

    def _build_enrichment(self, pd: PriceData) -> Dict[str, Any]:
        """Build the enrichment dict for a signal.
        Mirrors the logic previously in MarketScanner._build_signal_enrichment().
        """
        data: Dict[str, Any] = {}
        sym = pd.symbol
        data["open"]   = pd.open_price if pd.open_price else pd.price
        data["high"]   = pd.high       if pd.high       else pd.price
        data["low"]    = pd.low        if pd.low        else pd.price
        data["close"]  = pd.price
        data["volume"] = pd.volume
        # RSI(14) — Wilder's smoothed average from tick-price history
        prices = list(self.price_history.get(sym, []))
        if len(prices) >= 15:
            try:
                period = 14
                gains, losses = [], []
                for i in range(1, len(prices)):
                    delta = prices[i] - prices[i - 1]
                    gains.append(max(delta, 0.0))
                    losses.append(max(-delta, 0.0))
                avg_gain = sum(gains[:period]) / period
                avg_loss = sum(losses[:period]) / period
                for i in range(period, len(gains)):
                    avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                    avg_loss = (avg_loss * (period - 1) + losses[i]) / period
                rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
                data["rsi"] = round(rsi, 2)
            except Exception:
                pass
        # BTC/USD proxy for crypto signals
        if "/" in sym or pd.asset_type == AssetType.CRYPTO:
            for btc_key in ("BTC/USD", "BTC/USDT", "BTC-USD", "BTCUSDT"):
                btc_hist = self.price_history.get(btc_key)
                if btc_hist and len(btc_hist) >= 2:
                    try:
                        btc = list(btc_hist)
                        if btc[0] > 0:
                            data["market_pct_change"] = round(
                                (btc[-1] - btc[0]) / btc[0] * 100, 4
                            )
                    except Exception:
                        pass
                    break
        # SMA(200) — only computable now that history_length=200
        if len(prices) >= 200:
            data["sma200"] = round(sum(prices[-200:]) / 200, 6)
        # Structural stop — 0.3% buffer beyond current-bar wick
        direction = getattr(pd, "_pattern_dir", None)
        if direction not in ("long", "short"):
            direction = "short" if self._momentum(prices) < -0.5 else "long"
        low  = data["low"]
        high = data["high"]
        if direction == "long" and low > 0:
            data["structural_stop_price"] = round(low * 0.997, 6)
        elif direction == "short" and high > 0:
            data["structural_stop_price"] = round(high * 1.003, 6)
        # Market context for stocks (SPY/QQQ proxy)
        if "/" not in sym and pd.asset_type != AssetType.CRYPTO:
            for spy_key in ("SPY", "QQQ"):
                spy_hist = self.price_history.get(spy_key)
                if spy_hist and len(spy_hist) >= 2:
                    try:
                        spy = list(spy_hist)
                        if spy[0] > 0:
                            data["market_pct_change"] = round(
                                (spy[-1] - spy[0]) / spy[0] * 100, 4
                            )
                    except Exception:
                        pass
                    break
        pat = getattr(pd, "_pattern", None)
        if pat:
            data["pattern"] = pat
        data["volume_spike"]   = self.vol_spike(pd)
        data["momentum_score"] = self.momentum_val(pd)
        return data

    def detect_breakout(self, pd: PriceData) -> Optional[BreakoutSignal]:
        """
        Full detection pipeline.  Calls score(), then builds and returns a
        fully-populated BreakoutSignal when a setup is detected.
        Returns None when there is not enough history or no signal (score==0.0).
        broker is intentionally left unset — MarketScanner._process_symbol()
        sets it immediately after calling this method.
        """
        sc = self.score(pd)
        if sc == 0.0:
            return None
        sym     = pd.symbol
        pattern = ("early_breakout_strong" if sc > 0.75 else
                   "early_breakout"         if sc > 0.55 else "watchlist")
        pat_dir = getattr(pd, "_pattern_dir", None)
        if pat_dir in ("long", "short"):
            direction = pat_dir
        elif self.momentum_val(pd) < -0.5:
            direction = "short"
        else:
            direction = "long"
        sig = BreakoutSignal(
            symbol=sym, asset_type=pd.asset_type, current_price=pd.price,
            predicted_move_pct=sc * 100, confidence=sc,
            volume_spike=self.vol_spike(pd),
            momentum_score=self.momentum_val(pd),
            pattern_detected=pattern, timestamp=datetime.now(),
            phase=SignalPhase.WATCHLIST, entry_price=pd.price,
            direction=direction,
        )
        sig.price_history  = list(self.price_history.get(sym, []))
        sig.volume_history = list(self.volume_history.get(sym, []))
        sig.enrichment     = self._build_enrichment(pd)
        return sig

    def _compressing(self, p):
        r = p[-20:]; avg = sum(r)/len(r)
        return (max(r)-min(r))/avg*100 < 2.0
    def _expanding(self, p):
        return (p[-1]-p[-3])/p[-3]*100 > 1.2
    def _early_volume(self, v, p):
        if len(v) < 2 or len(p) < 5 or p[-5] == 0: return False
        avg = sum(v[:-1])/len(v[:-1])
        return (v[-1]/avg > 2.5 if avg>0 else False) and (p[-1]-p[-5])/p[-5]*100 < 2.0
    def _near_vwap(self, p, v):
        tv = sum(v)
        if tv==0: return False
        vwap = sum(x*y for x,y in zip(p,v))/tv
        return abs(p[-1]-vwap)/vwap*100 < 1.0
    def _not_overextended(self, p):
        return (p[-1]-p[-10])/p[-10]*100 < 8.0
    def _momentum(self, p):
        ch = [(p[i]-p[i-1])/p[i-1]*100 for i in range(1,len(p))]
        return sum(ch[-5:])/5 if len(ch)>=5 else 0.0
    def _vol_spike(self, v):
        if len(v) < 2: return 1.0
        avg = sum(v[:-1])/len(v[:-1])
        return v[-1]/avg if avg>0 else 1.0

# =============================================================================
# WATCHLIST MANAGER
# =============================================================================

class WatchlistManager:
    def __init__(self, config: ConfigManager):
        self.config   = config
        self._watched: Dict[str, BreakoutSignal] = {}

    def add(self, signal: BreakoutSignal):
        sym = signal.symbol
        if sym in self._watched:
            logger.info(f"[WATCHLIST] {signal.broker} {sym} @ ${signal.current_price:.4f} " f"early exit")
            return
        signal.entry_price = signal.current_price
        signal.scan_count  = 0
        signal.phase       = SignalPhase.WATCHLIST
        self._watched[sym] = signal
        logger.info(f"[WATCHLIST] {signal.broker} {sym} @ ${signal.current_price:.4f} "
                    f"score={signal.confidence:.2f}")

    def check(self, pd: PriceData) -> Optional[BreakoutSignal]:
        sym = pd.symbol
        if sym not in self._watched:
            return None
        sig       = self._watched[sym]
        sig.scan_count += 1
        gate_pct  = self.config.get("momentum_gate_pct",  0.80)
        max_scans = self.config.get("momentum_gate_scans", 3)
        mpct      = move_pct(sig.entry_price, pd.price)
        direction = signal_direction(sig).upper()   # explicit direction wins; no momentum re-inference

        moved_right = (
            mpct >= gate_pct  if direction == "LONG"  else
            mpct <= -gate_pct if direction == "SHORT" else False
        )
        if moved_right:
            sig.phase         = SignalPhase.ALERTED
            sig.current_price = pd.price
            sig.best_move_pct = favorable_move_pct(direction, mpct)  # positive = good for both sides
            del self._watched[sym]
            logger.warning(
                f"[GATE PASSED] {sig.broker} {sym} raw_move={mpct:+.2f}% dir={direction}"
            )
            return sig
        if sig.scan_count >= max_scans:
            logger.info(f"[WATCHLIST EXPIRED] {sym} — dropped after {max_scans} scans")
            del self._watched[sym]
        return None

    def get(self, symbol: str) -> Optional[BreakoutSignal]:
        """Return the watched signal for a symbol, or None."""
        return self._watched.get(symbol)

    def is_watching(self, symbol: str) -> bool:
        return symbol in self._watched

    def size(self) -> int:
        return len(self._watched)

# =============================================================================
# ESCALATION TRACKER
# =============================================================================

class EscalationTracker:
    def __init__(self, config: ConfigManager):
        self.config   = config
        self._tracked: Dict[str, dict] = {}

    def register(self, signal: BreakoutSignal):
        signal.breakout_level = signal.entry_price  # price at initial detection
        self._tracked[signal.symbol] = {"signal": signal, "level": 0, "bars_since": 0}

    def check(self, pd: PriceData) -> Optional[tuple]:
        sym = pd.symbol
        if sym not in self._tracked:
            return None
        entry    = self._tracked[sym]
        entry["bars_since"] += 1
        sig      = entry["signal"]
        # Update bars count and distance on every cycle
        sig.bars_since_breakout = entry["bars_since"]
        if sig.breakout_level > 0:
            raw = (pd.price - sig.breakout_level) / sig.breakout_level * 100
            sig.distance_from_breakout_pct = raw if sig.direction == "long" else -raw
        mpct     = move_pct(sig.entry_price, pd.price)
        fav_move = favorable_move_pct(sig.direction, mpct)
        if fav_move > sig.best_move_pct:
            sig.best_move_pct = fav_move
        lvl     = entry["level"]
        t10     = self.config.get("escalate_10pct", 10.0)
        t5      = self.config.get("escalate_5pct",   5.0)
        t2      = self.config.get("escalate_2pct",   2.0)
        new_lvl = lvl
        if   fav_move >= t10 and lvl < 3: new_lvl = 3
        elif fav_move >= t5  and lvl < 2: new_lvl = 2
        elif fav_move >= t2  and lvl < 1: new_lvl = 1
        if new_lvl > lvl:
            entry["level"]    = new_lvl
            sig.current_price = pd.price
            return sig, new_lvl
        return None

    def unregister(self, symbol: str):
        self._tracked.pop(symbol, None)

# =============================================================================
# ALERT SYSTEM
# =============================================================================

class AlertSystem:
    def __init__(self, config: ConfigManager, detector=None):
        self.config           = config
        self.detector         = detector   # BreakoutDetector ref for BTC market context
        self.alert_queue      = queue.Queue()
        self.alert_history:   List[Alert] = []
        self.email_last_sent: Dict[str, datetime] = {}
        self.gui              = None
        self.running          = False
        import threading as _thr
        # Bot injection sender
        self.bot_sender = BreakoutSender(self.config)
        self._digest_lock  = _thr.Lock()
        self._last_digest  = datetime.now()

    def start(self):
        self.running      = True
        self.alert_thread = threading.Thread(target=self._process_alerts, daemon=True)
        self.alert_thread.start()
        logger.info("Alert system started")

    def stop(self):
        self.running = False

    def send_alert(self, signal: BreakoutSignal, escalation: int = 0):
        message = self._format_alert_message(signal, escalation)
        if self.config.get("enable_visual_alert", True):
            self.alert_queue.put(Alert(signal, AlertType.VISUAL, message,
                                       datetime.now(), escalation))
        if self.config.get("enable_email_alert", False):
            self._email_alert(signal, message, escalation=escalation)
                # Inject to Trading Bot V2 if escalation threshold met
        if hasattr(self, "bot_sender"):
            candle_data = self._build_candle_data(signal)
            self.bot_sender.send_signal(signal, escalation, candle_data=candle_data)


    def _build_candle_data(self, signal: BreakoutSignal) -> dict:
        """
        Compatibility shim — returns scanner-built enrichment when present.

        As of BREAKOUT 8A the scanner populates signal.enrichment at signal
        creation time via BreakoutDetector._build_enrichment().
        This method forwards that dict so the sender gets the data the scanner
        already computed.  If market_pct_change is missing and a detector is
        wired in, it back-fills it from the BTC price history so the receiver's
        market-context soft check has something to work with.

        Falls back to a minimal dict (close only) if enrichment is missing,
        which will cause receiver soft checks to SKIP (neutral) rather than FAIL.
        """
        if signal.enrichment:
            d = dict(signal.enrichment)
            if d.get("market_pct_change") is None and self.detector:
                mkt = self._btc_market_pct_change()
                if mkt is not None:
                    d["market_pct_change"] = mkt
            return d

        # Legacy fallback — enrichment not populated (pre-BREAKOUT 8A signals)
        d = {"close": signal.current_price}
        if self.detector:
            mkt = self._btc_market_pct_change()
            if mkt is not None:
                d["market_pct_change"] = mkt
        return d

    def _btc_market_pct_change(self) -> Optional[float]:
        """Derive a market proxy from BTC/USDT price history in the detector."""
        if not self.detector:
            return None
        for proxy in ("BTC/USDT", "BTC/USD", "BTC-USD"):
            ph = self.detector.price_history.get(proxy)
            if ph and len(ph) >= 2:
                try:
                    oldest = ph[0]
                    newest = ph[-1]
                    if oldest > 0:
                        return ((newest - oldest) / oldest) * 100.0
                except Exception:
                    pass
        return None


    def _format_alert_message(self, signal: BreakoutSignal, escalation: int) -> str:
        stage = "⚡ EARLY" if signal.confidence > 0.7 else "👀 WATCH"

        return f"""
    🚨 BREAKOUT SCAN 🚨

    Symbol: {signal.symbol}
    Type: {signal.asset_type.value.upper()}

    Strength Score: {signal.confidence*100:.1f}%
    Stage: {stage}

    Pattern: {signal.pattern_detected}
    Momentum: {signal.momentum_score:.2f}
    Volume Spike: {signal.volume_spike:.2f}x

    Price: ${signal.current_price:.4f}
    Time: {signal.timestamp.strftime('%H:%M:%S')}

    ➡️ Focus: Watching for breakout continuation
"""


    def _format_message(self, signal: BreakoutSignal, escalation: int) -> str:
        meta  = classify_signal(signal)
        flags = {0:"NEW ALERT", 1:"MOVING +2%", 2:"HOT +5%", 3:"ROCKET +10%"}
        emoji = {0:"🚀", 1:"📈", 2:"🔥", 3:"⚡"}
        flag  = f"{emoji.get(escalation,'')} {flags.get(escalation,'ALERT')}"
        mpct  = move_pct(signal.entry_price, signal.current_price)
        return (
            f"{'='*50}\n"
            f"{flag}  [{signal.broker}] {signal.symbol}\n"
            f"Dir: {meta['direction']}  Stage: {meta['stage']}  PoC: {meta['poc']:.0f}%\n"
            f"Entry: ${signal.entry_price:.4f}  Now: ${signal.current_price:.4f}  "
            f"Move: {mpct:+.2f}%\n"
            f"VolSpike: {signal.volume_spike:.1f}x  BestMove: {signal.best_move_pct:+.2f}%\n"
            f"Time: {signal.timestamp.strftime('%H:%M:%S')}\n"
        )

    def _email_alert(self, signal: BreakoutSignal, message: str,
                     escalation: int = 0):
        """
        Email rules:
          ROCKET (escalation 3, >10% move) -> immediate email
          All others (watchlist, new alert, moving, hot) -> NO email
          Serious errors -> use send_error_email() directly
        Per-symbol cooldown: 30 minutes on rockets.
        """
        if escalation < 3:
            return   # only email on rockets and errors

        sym  = signal.symbol
        last = self.email_last_sent.get(sym)
        if last and (datetime.now()-last).total_seconds() < 1800:
            return   # 30-min per-symbol cooldown

        ef = self.config.get("email_from","")
        et = self.config.get("email_to","")
        pw = self.config.get("email_password","")
        if not all([ef, et, pw]):
            return

        self.email_last_sent[sym] = datetime.now()
        mpct = move_pct(signal.entry_price, signal.current_price)
        self._send_email_now(
            ef, et, pw,
            subject=f"ROCKET {mpct:+.1f}%: [{signal.broker}] {sym}",
            body=message
        )

    def send_error_email(self, subject: str, body: str):
        """Send an immediate email for serious errors (exchange down, etc.)."""
        ef = self.config.get("email_from","")
        et = self.config.get("email_to","")
        pw = self.config.get("email_password","")
        if not all([ef, et, pw]):
            return
        self._send_email_now(ef, et, pw, subject=f"[SCANNER ERROR] {subject}", body=body)

    def _send_email_now(self, ef: str, et: str, pw: str,
                        subject: str, body: str):
        try:
            msg = MIMEMultipart()
            msg["From"]    = ef
            msg["To"]      = et
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))
            with smtplib.SMTP(self.config.get("email_smtp_server", "smtp.gmail.com"),
                              self.config.get("email_smtp_port", 587)) as srv:
                srv.starttls()
                srv.login(ef, pw)
                srv.sendmail(ef, et, msg.as_bytes())
            logger.info(f"Email sent: {subject}")
        except Exception as e:
            logger.error(f"Email failed: {e}")

    def _process_alerts(self):
        while self.running:
            try:
                alert = self.alert_queue.get(timeout=1)
                self.alert_history.append(alert)
                logger.warning(
                    f"ALERT(esc={alert.escalation}): [{alert.signal.broker}] "
                    f"{alert.signal.symbol} — {alert.signal.pattern_detected}"
                )
                if self.gui and hasattr(self.gui, "root") and self.gui.root:
                    try:
                        self.gui.root.after(
                            0, self.gui.add_alert,
                            alert.message, alert.signal, alert.escalation
                        )
                    except Exception:
                        pass
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Alert error: {e}")

# =============================================================================
# TRADING ENGINE
# =============================================================================

class TradingEngine:
    def __init__(self, config: ConfigManager):
        self.config          = config
        self.alpaca_api      = None
        self.crypto_exchange = None
        self._init_brokers()

    def _init_brokers(self):
        if ALPACA_AVAILABLE and self.config.get("broker") == "alpaca":
            try:
                self.alpaca_api = tradeapi.REST(
                    self.config.get("alpaca_api_key"), self.config.get("alpaca_secret_key"),
                    self.config.get("alpaca_base_url"), api_version="v2",
                )
            except Exception as e:
                logger.error(f"Alpaca trading init failed: {e}")
        if CCXT_AVAILABLE:
            for name, kw in [
                ("kraken",   {"apiKey": self.config.get("kraken_api_key"),
                              "secret": self.config.get("kraken_api_secret"),
                              "enableRateLimit": True}),
                ("coinbase", {"apiKey": self.config.get("coinbase_api_key"),
                              "secret": self.config.get("coinbase_api_secret"),
                              "enableRateLimit": True}),
            ]:
                try:
                    self.crypto_exchange = getattr(ccxt, name)(kw)
                    break
                except Exception:
                    continue

    def can_auto_trade(self):       return self.config.get("enable_auto_trade", False)
    def should_confirm_trade(self): return not self.config.get("auto_trade_without_confirm", False)

    def execute_signal(self, signal: BreakoutSignal) -> dict:
        if not self.can_auto_trade():   return {"status": "disabled"}
        if self.should_confirm_trade(): return {"status": "confirmation_required"}
        amount    = self.config.get("default_trade_amount_usd", 1000)
        direction = signal_direction(signal)   # "long" or "short"
        if signal.asset_type == AssetType.STOCK:
            if not self.alpaca_api: return {"error": "no alpaca"}
            side = "buy" if direction == "long" else "sell"
            try:
                o = self.alpaca_api.submit_order(
                    symbol=signal.symbol, notional=amount,
                    side=side, type="market", time_in_force="day")
                return {"success": True, "order_id": o.id, "side": side}
            except Exception as e:
                return {"error": str(e)}
        else:
            if not self.crypto_exchange: return {"error": "no exchange"}
            if direction == "short":
                # Most spot crypto exchanges don't support native short selling.
                # Return a clear error rather than silently sending a buy.
                supports_short = getattr(self.crypto_exchange, "has", {}).get("createShortOrder", False)
                if not supports_short:
                    return {"error": f"Broker does not support short orders for {signal.symbol}"}
            side = "buy" if direction == "long" else "sell"
            try:
                qty = amount / signal.current_price
                o   = self.crypto_exchange.create_order(
                    signal.symbol, "market", side, qty)
                return {"success": True, "order_id": o.get("id"), "side": side}
            except Exception as e:
                return {"error": str(e)}

# =============================================================================
# MAIN SCANNER
# =============================================================================

class MarketScanner:
    def __init__(self, config_path: str = "scanner_config.json"):
        self.config          = ConfigManager(config_path)
        self.stock_fetcher   = StockDataFetcher(self.config)
        self.crypto_fetcher  = CryptoDataFetcher(self.config)
        self.detector        = BreakoutDetector(self.config)
        logger.info("Made it to MarketScanner: before watchlist init")
        self.watchlist       = WatchlistManager(self.config)
        logger.info("Made it to MarketScanner: after watchlist init")
        self._log_injected_watchlists()
        self.escalation      = EscalationTracker(self.config)
        self.alert_system    = AlertSystem(self.config, detector=self.detector)
        self.trading         = TradingEngine(self.config)
        self.running         = False
        self.gui             = None
        self.latest_movers:        List[PriceData]     = []
        self.latest_crypto_movers: List[PriceData]     = []
        self._alerted_at:          Dict[str, datetime] = {}

        # Gap watchlist queue + catalyst checker
        self.gap_watchlist   = GapWatchlist()
        self.catalyst_checker = CatalystChecker(
            api_key=self.config.get("alphavantage_api_key", "")
        )
        self._gap_stock_session:  Optional[str] = None
        self._gap_crypto_session: Optional[str] = None

        # Volume surge tracker (intraday 5m bar detector)
        self.vol_surge = VolumeSurgeTracker(cooldown_hours=4.0)

    def _log_injected_watchlists(self):
        stocks = load_injected_stock_symbols()
        crypto = load_injected_crypto_symbols()
        logger.info(
            f"[WATCHLIST LOAD] stocks={len(stocks)} {stocks} | "
            f"crypto={len(crypto)} {crypto}"
        )

    def start(self):
        self.running     = True
        self.alert_system.start()
        logger.info(
            "[SCAN LOOP] cadence full=%ss hot=%ss bot_min_escalation=%s",
            self.config.get("scan_interval_seconds", 30),
            self.config.get("hot_symbol_scan_seconds", 5),
            self.config.get("bot_min_escalation", 1),
        )
        self.scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self.scan_thread.start()
        logger.info("Market scanner started")

    def stop(self):
        self.running = False
        self.alert_system.stop()
        logger.info("Scanner stopped")

    def _scan_loop(self):
        while self.running:
            try:
                stock_open = self.config.is_market_open(AssetType.STOCK)
                crypto_open = self.config.is_market_open(AssetType.CRYPTO)
                logger.info(
                    f"[SCAN LOOP] stock_open={stock_open} crypto_open={crypto_open} "
                    f"time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                if stock_open:
                    self._scan_stocks()
                # Gap detection runs slightly before open (9:25 ET) -- don't gate on stock_open
                self._gap_detect_stocks()
                if crypto_open:
                    self._scan_cryptos()
                    self._gap_detect_crypto()
                self.gap_watchlist.expire_stale(datetime.now(timezone.utc), datetime.now())
                update_signal_tracking(self)
            except Exception as e:
                logger.error(f"Scan loop error: {e}", exc_info=True)
            interval = max(1, int(self.config.get("scan_interval_seconds", 30)))
            hot_interval = max(1, int(self.config.get("hot_symbol_scan_seconds", 5)))
            next_full_scan = time.time() + interval
            while self.running and time.time() < next_full_scan:
                time.sleep(min(hot_interval, max(0.1, next_full_scan - time.time())))
                if self.running:
                    self._scan_hot_symbols()

    def _can_alert(self, symbol: str) -> bool:
        last = self._alerted_at.get(symbol)
        return not (last and (datetime.now()-last).total_seconds() < ALERT_COOLDOWN_SECS)

    def _mark_alerted(self, symbol: str):
        self._alerted_at[symbol] = datetime.now()

    def _scan_hot_symbols(self):
        """
        Recheck only symbols already in the WATCHLIST or escalation tracker.
        Full market scans stay on scan_interval_seconds; hot symbols get a
        faster cadence so breakout handoff can happen near the start of a move.
        """
        hot: Dict[str, BreakoutSignal] = {}
        hot.update(getattr(self.watchlist, "_watched", {}))
        for sym, entry in getattr(self.escalation, "_tracked", {}).items():
            sig = entry.get("signal") if isinstance(entry, dict) else None
            if sig:
                hot[sym] = sig
        if not hot:
            return

        logger.debug(f"[HOT SCAN] checking {len(hot)} watched/tracked symbols")
        for sym, sig in list(hot.items()):
            try:
                pd = self._fetch_hot_price_data(sym, sig)
                if not pd:
                    continue
                broker = getattr(sig, "broker", "") or getattr(pd, "_exchange_name", "")
                self._process_hot_symbol(pd, broker or self.crypto_fetcher.exchange_name)
            except Exception as e:
                logger.debug(f"[HOT SCAN] {sym} skipped: {e}")

    def _fetch_hot_price_data(self, symbol: str, sig: BreakoutSignal) -> Optional[PriceData]:
        if sig.asset_type == AssetType.STOCK:
            bars = self.stock_fetcher.get_historical_data(symbol)
            return bars[-1] if bars else None

        broker = (getattr(sig, "broker", "") or "").upper()
        exchanges = getattr(self.crypto_fetcher, "exchanges", {})
        exc = exchanges.get(broker) or self.crypto_fetcher.exchange

        if broker == "COINGECKO" or not exc:
            price = _coingecko_price(symbol)
            if not price:
                return None
            pd = PriceData(symbol, price, price, price, price, 0, datetime.now(), AssetType.CRYPTO)
            pd._exchange_name = "COINGECKO"
            return pd

        ticker = exc.fetch_ticker(symbol)
        last = ticker.get("last") or ticker.get("close")
        if not last:
            return None
        chg = ticker.get("percentage", 0) or 0
        op = ticker.get("open") or (last / (1 + chg / 100) if chg else last)
        pd = PriceData(
            symbol, float(last), float(op),
            float(ticker.get("high") or last), float(ticker.get("low") or last),
            float(ticker.get("quoteVolume") or ticker.get("baseVolume") or 0),
            datetime.now(), AssetType.CRYPTO,
        )
        pd._exchange_name = broker or self.crypto_fetcher.exchange_name
        return pd

    def _process_hot_symbol(self, pd: PriceData, broker: str):
        sym = pd.symbol
        result = self.escalation.check(pd)
        if result:
            sig, level = result
            sig.enrichment   = self.detector._build_enrichment(pd)
            sig.volume_spike = self.detector.vol_spike(pd)
            logger.warning(f"[HOT ESCALATE {level}] {broker} {sym} move={sig.best_move_pct:+.2f}%")
            self.alert_system.send_alert(sig, escalation=level)
            return

        if self.watchlist.is_watching(sym):
            promoted = self.watchlist.check(pd)
            if promoted and self._can_alert(sym):
                promoted.broker = broker
                self._mark_alerted(sym)
                self.escalation.register(promoted)
                self.alert_system.send_alert(promoted, escalation=0)
                log_signal_to_csv(promoted)

    # -----------------------------------------------------------------------
    # Scanner-owned signal enrichment (computed at signal creation time)
    # -----------------------------------------------------------------------

    def _build_signal_enrichment(self, pd: PriceData, sym: str) -> Dict[str, Any]:
        """
        Thin wrapper — delegates to BreakoutDetector._build_enrichment() which
        is the single authoritative implementation.  This method is kept for
        any legacy call sites but no longer duplicates the logic.
        """
        return self.detector._build_enrichment(pd)

    def _process_symbol(self, pd: PriceData, broker: str):
        sym = pd.symbol
        # Check gap watchlist first -- uses data already fetched, no extra cost
        self._check_gap_pending(pd)

        # Volume surge check -- accumulates per-tick volume, fires at 3x avg
        asset_class = "stock" if broker == "alpaca" else "crypto"
        vol_payload = self.vol_surge.update_from_pricedata(pd, asset_class)
        if vol_payload and hasattr(self, "bot_sender"):
            self.bot_sender.send_raw_payload(vol_payload)
        result = self.escalation.check(pd)
        if result:
            sig, level = result
            # Refresh enrichment so receiver evaluates current-bar RSI/OHLC,
            # not detection-moment data that may be 90+ seconds stale.
            sig.enrichment   = self.detector._build_enrichment(pd)
            sig.volume_spike = self.detector.vol_spike(pd)
            logger.warning(f"[ESCALATE {level}] {broker} {sym} move={sig.best_move_pct:+.2f}%")
            self.alert_system.send_alert(sig, escalation=level)
            return
        if self.watchlist.is_watching(sym):
            promoted = self.watchlist.check(pd)
            if promoted and self._can_alert(sym):
                promoted.broker = broker
                self._mark_alerted(sym)
                self.escalation.register(promoted)
                self.alert_system.send_alert(promoted, escalation=0)
                log_signal_to_csv(promoted)
            return
        sig = self.detector.detect_breakout(pd)
        logger.info(
            f"[SIGNAL CHECK] {broker} {sym} score={sig.confidence if sig else 0.0:.3f} "
            f"threshold={self.config.get('watchlist_min_score', 0.40):.3f}"
        )
        if sig is None:
            return
        if sig.confidence < self.config.get("watchlist_min_score", 0.40):
            return
        sig.broker = broker
        logger.info(
            f"[SCANNER] {broker} {sym} dir={sig.direction} "
            f"enrich=yes ohlc={'open' in sig.enrichment} "
            f"rsi={'rsi' in sig.enrichment} "
            f"market={'market_pct_change' in sig.enrichment}"
        )
        self.watchlist.add(sig)

    def _scan_cryptos(self):
        try:
            logger.info("[CRYPTO SCAN] fetching movers")
            movers = self.crypto_fetcher.get_top_movers(
                self.config.get("top_cryptos_count", 100))
            self.latest_crypto_movers = movers
            exchanges = getattr(self.crypto_fetcher, "exchanges", {})
            logger.info(f"Crypto scan: {len(movers)} movers via "
                        f"{list(exchanges.keys()) or self.crypto_fetcher.exchange_name} | "
                        f"watching={self.watchlist.size()}")
            try:
                btc_exchange = (
                    exchanges.get("KRAKEN") or
                    exchanges.get("COINBASE") or
                    self.crypto_fetcher.exchange
                )
                if btc_exchange:
                    for btc_symbol in ("BTC/USD", "BTC/USDT"):
                        if btc_symbol in getattr(btc_exchange, "symbols", []):
                            btc_bars = self.crypto_fetcher.get_historical_data(
                                btc_symbol, exchange=btc_exchange
                            )
                            for h in btc_bars:
                                self.detector.update_history(h)
                            logger.debug(
                                f"[CRYPTO SCAN] BTC seed {btc_symbol}: "
                                f"{len(btc_bars)} bars"
                            )
                            break
            except Exception as e:
                logger.debug(f"[CRYPTO SCAN] BTC seed failed: {e}")
            for pd in movers:
                # Use the broker tag set by get_top_movers, fall back to primary
                broker = getattr(pd, "_exchange_name",
                                 self.crypto_fetcher.exchange_name)
                # Find the correct exchange for this symbol.
                # CoinGecko is not a CCXT exchange — fall back to primary for history.
                if broker.upper() == "COINGECKO":
                    exc = self.crypto_fetcher.exchange   # primary exchange for history
                else:
                    exc = exchanges.get(broker) or self.crypto_fetcher.exchange
                if not exc:
                    continue
                # Check the resolved exchange has this symbol; skip if not
                if pd.symbol not in getattr(exc, "symbols", {}):
                    # For CoinGecko-sourced symbols, try anyway — some pairs differ in naming
                    if broker.upper() != "COINGECKO":
                        continue
                for h in self.crypto_fetcher.get_historical_data(pd.symbol, exchange=exc):
                    self.detector.update_history(h)
                logger.info(
                    f"[CRYPTO SCAN] processing {broker} {pd.symbol} "
                    f"price={pd.price} change={pd.daily_change_pct:+.2f}%"
                )
                self._process_symbol(pd, broker)
        except Exception as e:
            logger.error(f"Crypto scan error: {e}", exc_info=True)

    def _scan_stocks(self):
        try:
            logger.info("[STOCK SCAN] fetching movers")
            # Seed SPY into price_history for stock market_pct_change proxy
            try:
                spy_bars = self.stock_fetcher.get_historical_data("SPY")
                for h in spy_bars:
                    self.detector.update_history(h)
                logger.debug(f"[STOCK SCAN] SPY seed: {len(spy_bars)} bars")
            except Exception as e:
                logger.debug(f"[STOCK SCAN] SPY seed failed: {e}")
            movers = self.stock_fetcher.get_top_movers(
                self.config.get("top_stocks_count", 100))
            self.latest_movers = movers
            logger.info(f"Stock scan: {len(movers)} movers | watching={self.watchlist.size()}")
            for pd in movers:
                sym = pd.symbol
                # Skip warrants, rights, units, indexes — not tradeable breakouts
                if (sym.startswith("$") or sym.startswith("^")
                        or len(sym) > 5
                        or any(c in sym for c in ["+", "=", "/"])):
                    continue
                for h in self.stock_fetcher.get_historical_data(sym):
                    self.detector.update_history(h)
                logger.info(
                    f"[STOCK SCAN] processing ALPACA {sym} "
                    f"price={pd.price} change={pd.daily_change_pct:+.2f}%"
                )
                self._process_symbol(pd, "ALPACA")
        except Exception as e:
            logger.error(f"Stock scan error: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Gap detection -- called from scan loop at market open windows
    # ------------------------------------------------------------------

    def _gap_detect_stocks(self) -> None:
        """Register overnight stock gaps at 9:25-9:35 ET. Fires once per day."""
        try:
            import yfinance as yf
        except ImportError:
            return

        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        in_window = (9, 25) <= (now_et.hour, now_et.minute) < (9, 35)
        today = now_et.strftime("%Y-%m-%d")
        if not in_window or self._gap_stock_session == today:
            return
        self._gap_stock_session = today

        from scanners.gap_watchlist import GapSetup
        symbols = []
        try:
            from Scripts.config_manager import config as cfg
            symbols = getattr(cfg, "STOCK_WATCHLIST", [])
        except Exception:
            pass

        logger.info("[GAP DETECT] Stock open window -- scanning %d symbols", len(symbols))

        # First pass: find all qualifying gaps
        candidates = []
        for symbol in symbols:
            if self.gap_watchlist.is_pending(symbol):
                continue
            try:
                ticker = yf.Ticker(symbol)
                hist   = ticker.history(period="5d", interval="1d", auto_adjust=True)
                if hist is None or len(hist) < 2:
                    continue
                prev_close = float(hist["Close"].iloc[-2])
                today_open = float(hist["Open"].iloc[-1])
                if prev_close <= 0 or today_open <= 0:
                    continue
                gap_pct = (today_open - prev_close) / prev_close * 100.0
                if abs(gap_pct) < 2.0:
                    continue
                avg_vol   = float(hist["Volume"].iloc[:-1].mean())
                today_vol = float(hist["Volume"].iloc[-1])
                vol_spike = today_vol / avg_vol if avg_vol > 0 else 1.0
                gap_type  = self._classify_gap(abs(gap_pct), vol_spike)
                if gap_type is None:
                    continue
                candidates.append({
                    "symbol": symbol, "gap_pct": gap_pct,
                    "prev_close": prev_close, "today_open": today_open,
                    "vol_spike": vol_spike, "gap_type": gap_type,
                })
                time.sleep(0.2)
            except Exception as e:
                logger.debug("[GAP DETECT] %s error: %s", symbol, e)

        if not candidates:
            return

        # Batch catalyst check -- one API call for all candidates
        syms = [c["symbol"] for c in candidates]
        logger.info("[GAP DETECT] Checking catalysts for %d candidates: %s", len(syms), syms)
        catalyst_results = self.catalyst_checker.check_batch(syms)

        # Second pass: register only those with a catalyst
        for c in candidates:
            symbol   = c["symbol"]
            cat      = catalyst_results.get(symbol)
            gap_pct  = c["gap_pct"]

            if cat and not cat.tradeable:
                logger.info("[GAP DETECT] %s skipped -- %s", symbol, cat.skip_reason)
                continue

            # Sentiment direction should loosely match gap direction
            if cat and cat.has_catalyst:
                if gap_pct > 0 and cat.sentiment_score < -0.3:
                    logger.info("[GAP DETECT] %s skipped -- gap up but bearish sentiment (%.2f)",
                                symbol, cat.sentiment_score)
                    continue
                if gap_pct < 0 and cat.sentiment_score > 0.3:
                    logger.info("[GAP DETECT] %s skipped -- gap down but bullish sentiment (%.2f)",
                                symbol, cat.sentiment_score)
                    continue

            direction = (
                ("long" if gap_pct > 0 else "short") if c["gap_type"] == "gap_and_go"
                else ("short" if gap_pct > 0 else "long")
            )

            catalyst_tag = cat.catalyst_type if cat else "unknown"
            strength_tag = cat.strength if cat else "unknown"
            logger.info("[GAP DETECT] %s ACCEPTED  gap=%.1f%%  catalyst=%s(%s)",
                        symbol, gap_pct, catalyst_tag, strength_tag)

            self.gap_watchlist.register(GapSetup(
                symbol=symbol, asset_class="stock", gap_type=c["gap_type"],
                direction=direction, gap_pct=gap_pct, prev_close=c["prev_close"],
                gap_open=c["today_open"], vol_spike=c["vol_spike"],
                registered_at=datetime.now(timezone.utc),
            ))

    def _gap_detect_crypto(self) -> None:
        """Register overnight crypto gaps at 00:00-00:15 UTC. Fires once per day."""
        try:
            import yfinance as yf
        except ImportError:
            return

        now_utc = datetime.now(timezone.utc)
        in_window = (0, 0) <= (now_utc.hour, now_utc.minute) < (0, 15)
        today = now_utc.strftime("%Y-%m-%d")
        if not in_window or self._gap_crypto_session == today:
            return
        self._gap_crypto_session = today

        from scanners.gap_watchlist import GapSetup
        symbols = []
        try:
            from Scripts.config_manager import config as cfg
            symbols = getattr(cfg, "CRYPTO_WATCHLIST", [])
        except Exception:
            pass

        logger.info("[GAP DETECT] Crypto open window -- scanning %d symbols", len(symbols))
        for symbol in symbols:
            if self.gap_watchlist.is_pending(symbol):
                continue
            try:
                yf_sym = symbol.replace("/", "-")
                ticker = yf.Ticker(yf_sym)
                hist   = ticker.history(period="5d", interval="1d", auto_adjust=True)
                if hist is None or len(hist) < 2:
                    continue
                prev_close = float(hist["Close"].iloc[-2])
                today_open = float(hist["Open"].iloc[-1])
                if prev_close <= 0 or today_open <= 0:
                    continue
                gap_pct = (today_open - prev_close) / prev_close * 100.0
                if abs(gap_pct) < 1.5:
                    continue
                avg_vol   = float(hist["Volume"].iloc[:-1].mean())
                today_vol = float(hist["Volume"].iloc[-1])
                vol_spike = today_vol / avg_vol if avg_vol > 0 else 1.0
                gap_type  = self._classify_gap(abs(gap_pct), vol_spike)
                if gap_type is None:
                    continue
                direction = (
                    ("long" if gap_pct > 0 else "short") if gap_type == "gap_and_go"
                    else ("short" if gap_pct > 0 else "long")
                )
                self.gap_watchlist.register(GapSetup(
                    symbol=symbol, asset_class="crypto", gap_type=gap_type,
                    direction=direction, gap_pct=gap_pct, prev_close=prev_close,
                    gap_open=today_open, vol_spike=vol_spike,
                    registered_at=datetime.now(timezone.utc),
                ))
                time.sleep(0.2)
            except Exception as e:
                logger.debug("[GAP DETECT] %s error: %s", symbol, e)

    def _classify_gap(self, abs_gap_pct: float, vol_spike: float) -> Optional[str]:
        if abs_gap_pct >= 3.0 and vol_spike >= 1.4:
            return "gap_and_go"
        if abs_gap_pct >= 2.0 and abs_gap_pct < 8.0 and vol_spike < 1.8:
            return "gap_fill"
        return None

    def _check_gap_pending(self, pd) -> None:
        """
        Called from _process_symbol for any symbol in the gap watchlist.
        Uses already-fetched PriceData -- no extra fetches needed.
        """
        symbol = pd.symbol
        if not self.gap_watchlist.is_pending(symbol):
            return

        # Estimate avg volume from detector history
        vol_hist = list(self.detector.volume_history.get(symbol, []))
        avg_vol  = sum(vol_hist[:-1]) / max(len(vol_hist) - 1, 1) if len(vol_hist) > 1 else pd.volume

        confirmed, payload = self.gap_watchlist.check(
            symbol     = symbol,
            live_price = pd.price,
            open_price = pd.open_price,
            high       = pd.high,
            low        = pd.low,
            volume     = pd.volume,
            avg_volume = avg_vol,
        )

        if confirmed and payload:
            self.bot_sender.send_raw_payload(payload)

    def run_gui(self):
        self.gui = ScannerGUI(self)
        self.alert_system.gui = self.gui
        self.gui.start()

    def run_console(self):
        self.start()
        print("\n" + "="*60)
        print("  RON'S MARKET BREAKOUT SCANNER")
        print("  Press Ctrl+C to stop")
        print("="*60 + "\n")
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping..."); self.stop()

# =============================================================================
# GUI
# =============================================================================

class ScannerGUI:
    COLS   = ("Time","Broker","Symbol","Dir","Score","Conf%",
               "Entry$","Now$","Move%","5m%","15m%","1h%","Status")
    WIDTHS = {"Time":68,"Broker":72,"Symbol":90,"Dir":45,
               "Score":50,"Conf%":52,"Entry$":78,"Now$":78,
               "Move%":65,"5m%":55,"15m%":55,"1h%":55,"Status":72}

    def __init__(self, scanner: MarketScanner):
        self.scanner        = scanner
        self.root           = None
        self.running        = False
        self._stock_store:  Dict[str, dict] = {}
        self._crypto_store: Dict[str, dict] = {}
        self._gap_store:    Dict[str, dict] = {}
        self._sort_col      = "Move%"
        self._sort_desc     = True

    def start(self):
        if not TKINTER_AVAILABLE:
            self.scanner.run_console()
            return
        if ALERT_SOUND_AVAILABLE:
            _init_audio()
        self.root = tk.Tk()
        self.root.title("Ron's Market Breakout Scanner")
        self.root.geometry("1460x1160")
        self.root.state("zoomed")
        self.root.configure(bg="#1a1a2e")
        self._setup_style()
        self._setup_ui()
        self.running = True
        self._update_loop()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(500, self._start_scanner)  # auto-start scanning on launch
        self.root.mainloop()

    def _setup_style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("Dark.Treeview",
            background="#0f0f23", foreground="white",
            fieldbackground="#0f0f23", rowheight=36,
            font=("Consolas", 13))
        s.configure("Dark.Treeview.Heading",
            background="#16213e", foreground="#ffaa00",
            font=("Helvetica", 13, "bold"))
        s.map("Dark.Treeview",
            background=[("selected","#347083")],
            foreground=[("selected","#ffffff")])
        s.layout("Dark.Treeview", s.layout("Treeview"))

    def _make_tree(self, parent, height=8) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=self.COLS,
                            show="headings", style="Dark.Treeview", height=height)
        for c in self.COLS:
            tree.heading(c, text=c, command=lambda x=c: self._sort_all(x))
            tree.column(c, width=self.WIDTHS.get(c,70), anchor="center")
        _sym_font = ("Consolas", 14, "bold")
        tree.tag_configure("new",       background="#0d1a2e", foreground="#4fc3f7", font=_sym_font)
        tree.tag_configure("esc1",      background="#2a1a00", foreground="#ffaa00", font=_sym_font)
        tree.tag_configure("esc2",      background="#0d2e0d", foreground="#00ff44", font=_sym_font)
        tree.tag_configure("esc3",      background="#2e0020", foreground="#ffffff", font=_sym_font)
        tree.tag_configure("watchlist", background="#151520", foreground="#888888", font=_sym_font)
        tree.tag_configure("fail",      background="#2e0d0d", foreground="#ff4444", font=_sym_font)
        return tree

    def _setup_ui(self):
        tk.Label(self.root, text="BREAKOUT SCANNER",
                 font=("Helvetica", 22, "bold"),
                 bg="#1a1a2e", fg="#00ff88").pack(pady=6)

        sf = tk.Frame(self.root, bg="#16213e")
        sf.pack(fill="x", padx=20, pady=2)
        self.status_label = tk.Label(sf, text="Status: Stopped",
                                     font=("Helvetica", 13),
                                     bg="#16213e", fg="#ffffff")
        self.status_label.pack(side="left", padx=10)
        self.watch_label = tk.Label(sf, text="Watchlist: 0",
                                    font=("Helvetica", 13),
                                    bg="#16213e", fg="#aaaaaa")
        self.watch_label.pack(side="left", padx=20)
        src = "Massive.com" if self.scanner.stock_fetcher.massive else "yfinance"
        tk.Label(sf, text=f"Stock bars: {src}",
                 font=("Helvetica",12), bg="#16213e", fg="#666666").pack(side="right", padx=10)

        bf = tk.Frame(self.root, bg="#1a1a2e")
        bf.pack(fill="x", padx=20, pady=2)
        self.start_btn = tk.Button(bf, text="▶ Start", command=self._start_scanner,
                                   bg="#00ff88", fg="#000000",
                                   font=("Helvetica", 13, "bold"), padx=15)
        self.start_btn.pack(side="left", padx=4)

        self.stop_btn = tk.Button(bf, text="■ Stop", command=self._stop_scanner,
                                  bg="#ff4444", fg="#ffffff",
                                  font=("Helvetica", 13, "bold"), padx=15,
                                  state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        tk.Button(bf, text="🗄 DB Panel", command=self._open_db_panel,
                  bg="#0f3460", fg="#e0e0e0",
                  font=("Helvetica", 12, "bold"), padx=12
                  ).pack(side="left", padx=8)
        self.volume_var = tk.DoubleVar(value=0.5)
        tk.Scale(bf, from_=0, to=1, resolution=0.01, orient="horizontal",
                 label="Volume", variable=self.volume_var,
                 bg="#1a1a2e", fg="#ffffff", length=120).pack(side="left", padx=10)
        self.auto_trade_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bf, text="Auto-Trade", variable=self.auto_trade_var,
                       command=self._toggle_auto_trade,
                       bg="#1a1a2e", fg="#ffffff", selectcolor="#16213e",
                       font=("Helvetica",13)).pack(side="left", padx=15)

        lf = tk.Frame(self.root, bg="#1a1a2e")
        lf.pack(fill="x", padx=20, pady=2)
        for txt, fg, bg in [
            ("● NEW ALERT",  "#4fc3f7","#0d1a2e"),
            ("● MOVING >2%", "#ffaa00","#2a1a00"),
            ("● HOT >5%",    "#00ff44","#0d2e0d"),
            ("● ROCKET >10%","#ffffff","#2e0020"),
            ("● WATCHLIST",  "#888888","#151520"),
            ("● FAIL",       "#ff4444","#2e0d0d"),
        ]:
            tk.Label(lf, text=txt, fg=fg, bg=bg,
                     font=("Helvetica",12,"bold"), padx=6, pady=2).pack(side="left", padx=3)

        sf2 = tk.LabelFrame(self.root,
                             text="  📈  STOCKS  [ALPACA]  —  click headers to sort",
                             font=("Helvetica",13,"bold"),
                             bg="#16213e", fg="#00ccff", padx=6, pady=6)
        sf2.pack(fill="both", expand=True, padx=20, pady=(6,3))
        self.stock_tree = self._make_tree(sf2, height=7)
        vsb_s = ttk.Scrollbar(sf2, orient="vertical", command=self.stock_tree.yview)
        self.stock_tree.configure(yscrollcommand=vsb_s.set)
        self.stock_tree.pack(side="left", fill="both", expand=True)
        vsb_s.pack(side="right", fill="y")

        cx = self.scanner.crypto_fetcher.exchange_name
        cf = tk.LabelFrame(self.root,
                            text=f"  🔶  CRYPTO  [{cx}]  —  click headers to sort",
                            font=("Helvetica",13,"bold"),
                            bg="#16213e", fg="#ffaa00", padx=6, pady=6)
        cf.pack(fill="both", expand=True, padx=20, pady=(3,3))
        self.crypto_tree = self._make_tree(cf, height=7)
        vsb_c = ttk.Scrollbar(cf, orient="vertical", command=self.crypto_tree.yview)
        self.crypto_tree.configure(yscrollcommand=vsb_c.set)
        self.crypto_tree.pack(side="left", fill="both", expand=True)
        vsb_c.pack(side="right", fill="y")

        # --- Gap Watchlist pane ---
        GAP_COLS   = ("Symbol","Type","Dir","Gap%","Fill%","Open$","Now$","Age","Checks","Status")
        GAP_WIDTHS = {"Symbol":90,"Type":65,"Dir":50,"Gap%":65,"Fill%":65,
                      "Open$":85,"Now$":85,"Age":55,"Checks":60,"Status":85}
        gf = tk.LabelFrame(self.root,
                            text="  🎯  GAP WATCHLIST  —  pending intraday confirmation",
                            font=("Helvetica",13,"bold"),
                            bg="#16213e", fg="#ffff44", padx=6, pady=6)
        gf.pack(fill="both", expand=False, padx=20, pady=(3,3))
        self.gap_tree = ttk.Treeview(gf, columns=GAP_COLS,
                                     show="headings", style="Dark.Treeview", height=5)
        for c in GAP_COLS:
            self.gap_tree.heading(c, text=c)
            self.gap_tree.column(c, width=GAP_WIDTHS.get(c, 70), anchor="center")
        _gf = ("Consolas", 13, "bold")
        self.gap_tree.tag_configure("watching",   background="#1a1a00", foreground="#ffff44", font=_gf)
        self.gap_tree.tag_configure("confirmed",  background="#002200", foreground="#00ff88", font=_gf)
        self.gap_tree.tag_configure("expired",    background="#1a0d00", foreground="#ff8800", font=_gf)
        self.gap_tree.tag_configure("failed",     background="#2e0d0d", foreground="#ff4444", font=_gf)
        vsb_g = ttk.Scrollbar(gf, orient="vertical", command=self.gap_tree.yview)
        self.gap_tree.configure(yscrollcommand=vsb_g.set)
        self.gap_tree.pack(side="left", fill="both", expand=True)
        vsb_g.pack(side="right", fill="y")

        mf = tk.LabelFrame(self.root, text="Stock Top Movers",
                            font=("Helvetica",10,"bold"),
                            bg="#16213e", fg="#aaaaaa", padx=6, pady=4)
        mf.pack(fill="x", padx=20, pady=(3,6))
        self.movers_tree = ttk.Treeview(mf,
                                        columns=("Symbol","Price","Change%"),
                                        show="headings", style="Dark.Treeview", height=3)
        for c, w in [("Symbol",100),("Price",110),("Change%",100)]:
            self.movers_tree.heading(c, text=c)
            self.movers_tree.column(c, width=w, anchor="center")
        self.movers_tree.pack(fill="x")

    def add_alert(self, message: str, signal: BreakoutSignal, escalation: int = 0):
        sym      = signal.symbol
        store    = self._crypto_store if signal.asset_type == AssetType.CRYPTO \
                   else self._stock_store
        existing = store.get(sym, {})
        mpct    = move_pct(signal.entry_price, signal.current_price)
        dir_txt = signal_direction(signal).upper()   # explicit direction; no momentum re-inference
        store[sym] = {
            "time":          signal.timestamp.strftime("%H:%M:%S"),
            "broker":        signal.broker,
            "symbol":        sym,
            "direction":     dir_txt,
            "score":         round(signal.predicted_move_pct, 1),
            "confidence":    round(signal.confidence * 100, 0),
            "entry_price":   existing.get("entry_price", signal.entry_price or signal.current_price),
            "entry_time":    existing.get("entry_time",  signal.timestamp),
            "current_price": signal.current_price,
            "move_pct":      mpct,
            "best_move":     max(
                favorable_move_pct(dir_txt, mpct),   # positive = good for either direction
                existing.get("best_move", 0.0)
            ),
            "t5m":  existing.get("t5m",  ""),
            "t15m": existing.get("t15m", ""),
            "t1h":  existing.get("t1h",  ""),
            "asset_type":    signal.asset_type,
            "escalation":    escalation,
            "phase":         signal.phase.value,
        }
        if not existing:
            store[sym]["entry_price"] = signal.entry_price or signal.current_price
            store[sym]["entry_time"]  = signal.timestamp
        tree = self.crypto_tree if signal.asset_type == AssetType.CRYPTO \
               else self.stock_tree
        self._refresh_tree(tree, store)
        self._beep(signal, escalation)

    def _tag_for(self, r: dict) -> str:
        esc   = r.get("escalation", 0)
        phase = r.get("phase","")
        mpct  = r.get("move_pct", 0.0)
        fav   = favorable_move_pct(r.get("direction", "long"), mpct)
        if phase == "watchlist": return "watchlist"
        if fav < 0:              return "fail"
        if esc >= 3:             return "esc3"
        if esc >= 2:             return "esc2"
        if esc >= 1:             return "esc1"
        return "new"

    def _status_for(self, r: dict) -> str:
        esc  = r.get("escalation", 0)
        mpct = r.get("move_pct", 0.0)
        fav  = favorable_move_pct(r.get("direction", "long"), mpct)
        if r.get("phase") == "watchlist": return "watching"
        if fav < 0:    return "FAIL"
        if esc >= 3:   return "ROCKET"
        if esc >= 2:   return "HOT"
        if esc >= 1:   return "MOVING"
        return "ALERT"

    def _refresh_tree(self, tree: ttk.Treeview, store: dict):
        if not tree: return
        STATUS_RANK = {"ROCKET": 4, "HOT": 3, "MOVING": 2, "ALERT": 1, "watching": 0, "FAIL": -1}
        key_map = {
            "Time":"time","Broker":"broker","Symbol":"symbol","Dir":"direction",
            "Score":"score","Conf%":"confidence","Entry$":"entry_price",
            "Now$":"current_price","Move%":"move_pct",
            "5m%":"t5m","15m%":"t15m","1h%":"t1h",
        }
        sk   = key_map.get(self._sort_col, "move_pct")
        rows = list(store.values())
        try:
            if self._sort_col == "Status":
                rows.sort(
                    key=lambda r: STATUS_RANK.get(self._status_for(r), 0),
                    reverse=self._sort_desc
                )
            elif sk == "move_pct":
                # Sort by favorable move so short winners (negative raw) sort correctly
                rows.sort(
                    key=lambda r: favorable_move_pct(
                        r.get("direction", "long"),
                        float(str(r.get("move_pct", 0)).replace("%","").replace("+","") or 0)
                    ),
                    reverse=self._sort_desc
                )
            else:
                rows.sort(
                    key=lambda r: float(str(r.get(sk,0)).replace("%","").replace("$","").replace("+","") or 0),
                    reverse=self._sort_desc
                )
        except Exception:
            rows.sort(key=lambda r: str(r.get(sk,"")), reverse=self._sort_desc)
        for item in tree.get_children():
            tree.delete(item)
        for r in rows:
            ep  = r.get("entry_price", 0) or 0
            cp  = r.get("current_price", ep) or ep
            mp  = r.get("move_pct", 0.0)
            def _pct(val):
                if not val or not ep or ep == 0: return ""
                try:   return f"{((float(val)-ep)/ep*100):+.1f}%"
                except: return ""
            tag    = self._tag_for(r)
            status = self._status_for(r)
            sym_display = f" {r.get('symbol','')} "   # pad symbol for readability
            tree.insert("", tk.END, values=(
                r.get("time",""), r.get("broker",""), sym_display,
                r.get("direction",""),
                f"{r.get('score',0):.0f}",
                f"{r.get('confidence',0):.0f}%",
                f"${ep:.4f}", f"${cp:.4f}",
                f"{mp:+.2f}%",
                _pct(r.get("t5m","")),
                _pct(r.get("t15m","")),
                _pct(r.get("t1h","")),
                status,
            ), tags=(tag,))

    def _refresh_gap_tree(self):
        """Refresh the gap watchlist pane from scanner.gap_watchlist.summary()."""
        if not hasattr(self, "gap_tree") or not self.gap_tree:
            return
        rows = self.scanner.gap_watchlist.summary()
        # Update gap_store keyed by symbol
        for r in rows:
            self._gap_store[r["symbol"]] = r
        # Remove entries no longer in queue
        active = {r["symbol"] for r in rows}
        for s in list(self._gap_store.keys()):
            if s not in active:
                del self._gap_store[s]

        for item in self.gap_tree.get_children():
            self.gap_tree.delete(item)

        for r in sorted(self._gap_store.values(), key=lambda x: x.get("age_min", 0)):
            status  = r.get("status", "WATCHING")
            tag     = {"WATCHING": "watching", "CONFIRMED": "confirmed",
                       "EXPIRED": "expired", "FAILED": "failed"}.get(status, "watching")
            gap_pct = r.get("gap_pct", 0.0)
            op      = r.get("gap_open", 0.0)
            # Fill% only meaningful for gap_fill; show -- for gap_and_go
            fill_str = f"{r.get('fill_pct', 0.0):.0f}%" if r.get("gap_type") == "FILL" else "--"
            # Now$ not available in summary (no live price stored), show --
            self.gap_tree.insert("", "end", values=(
                r.get("symbol", ""),
                r.get("gap_type", ""),
                r.get("direction", ""),
                f"{gap_pct:+.1f}%",
                fill_str,
                f"${op:.3f}",
                "--",
                f"{r.get('age_min', 0)}m",
                r.get("attempts", 0),
                status,
            ), tags=(tag,))

    def _sort_all(self, col: str):
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col  = col
            self._sort_desc = True
        self._refresh_tree(self.stock_tree,  self._stock_store)
        self._refresh_tree(self.crypto_tree, self._crypto_store)

    def _beep(self, signal: BreakoutSignal, escalation: int = 0):
        if not WINSOUND_AVAILABLE: return
        if not self.scanner.config.get("enable_sound_alert", True): return
        try:
            if self.volume_var.get() < 0.05: return
            patterns = {
                0: [(650, 220)],
                1: [(900, 220),(900, 220)],
                2: [(1100,200),(1100,200),(1100,200)],
                3: [(1400,150),(1400,150),(1400,150),(1400,150)],
            }
            for freq, dur in patterns.get(escalation, [(650, 220)]):
                winsound.Beep(freq, dur)
                time.sleep(0.05)
        except Exception:
            pass

    def _update_loop(self):
        """
        GUI update loop — runs on main tkinter thread every 30s.
        All network calls are offloaded to a background thread to
        prevent GUI freezing on slow/timeout Kraken responses.
        """
        # ── Update watchlist count and movers (no network) ────────────────
        try:
            self.watch_label.config(text=f"Watchlist: {self.scanner.watchlist.size()}")
        except Exception:
            pass

        # ── Refresh gap watchlist pane ────────────────────────────────────
        try:
            self._refresh_gap_tree()
        except Exception:
            pass
        try:
            for row in self.movers_tree.get_children():
                self.movers_tree.delete(row)
            for m in self.scanner.latest_movers[:12]:
                self.movers_tree.insert("", tk.END, values=(
                    m.symbol, f"${m.price:.4f}", f"{m.daily_change_pct:+.2f}%"
                ))
        except Exception:
            pass

        # ── Fetch prices in background thread, update GUI when done ──────
        import threading as _threading

        def _fetch_prices():
            """Runs in background — fetches current prices, no GUI calls here."""
            now     = datetime.now()
            updates = {}   # sym -> updated row dict
            for store_key, store in [("stock", self._stock_store),
                                      ("crypto", self._crypto_store)]:
                for sym, r in list(store.items()):
                    if r.get("phase") == "watchlist":
                        continue
                    try:
                        et      = r.get("entry_time", now)
                        elapsed = (now - et).total_seconds()
                        price   = None
                        if r.get("asset_type") == AssetType.CRYPTO:
                            exc = getattr(self.scanner.crypto_fetcher, "exchange", None)
                            if exc:
                                try:
                                    price = exc.fetch_ticker(sym)["last"]
                                except Exception:
                                    pass
                        else:
                            try:
                                h     = self.scanner.stock_fetcher.get_historical_data(sym)
                                price = h[-1].price if h else None
                            except Exception:
                                pass
                        if not price:
                            continue
                        ep       = r.get("entry_price", price)
                        new_r    = dict(r)
                        new_r["current_price"] = price
                        new_r["move_pct"]      = move_pct(ep, price)
                        new_r["best_move"]     = max(
                            favorable_move_pct(r.get("direction", "long"), new_r["move_pct"]),
                            r.get("best_move", 0.0)
                        )
                        if elapsed >= 300  and not r.get("t5m"):
                            new_r["t5m"]  = price
                        if elapsed >= 900  and not r.get("t15m"):
                            new_r["t15m"] = price
                        if elapsed >= 3600 and not r.get("t1h"):
                            new_r["t1h"]  = price
                        updates[(store_key, sym)] = new_r
                    except Exception:
                        continue

            # ── Post results back to GUI thread safely ────────────────────
            def _apply_updates():
                try:
                    for (store_key, sym), new_r in updates.items():
                        store = (self._stock_store if store_key == "stock"
                                 else self._crypto_store)
                        if sym in store:
                            store[sym] = new_r
                    self._refresh_tree(self.stock_tree,  self._stock_store)
                    self._refresh_tree(self.crypto_tree, self._crypto_store)
                except Exception:
                    pass
            try:
                self.root.after(0, _apply_updates)
            except Exception:
                pass

        t = _threading.Thread(target=_fetch_prices, daemon=True)
        t.start()

        if self.running:
            self.root.after(30000, self._update_loop)

    def _start_scanner(self):
        if not self.scanner.running:
            self.scanner.alert_system.gui = self
            self.scanner.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_label.config(text="Status: Running ●", fg="#00ff88")

    def _stop_scanner(self):
        self.scanner.stop()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text="Status: Stopped", fg="#ffffff")

    def _toggle_auto_trade(self):
        self.scanner.config.set("enable_auto_trade", self.auto_trade_var.get())

    def _open_db_panel(self):
        import subprocess, sys
        subprocess.Popen(
            [sys.executable, str(
                __import__("pathlib").Path(__file__).parent / "db_panel.py"
            )],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _on_close(self):
        self.running = False
        self.scanner.stop()
        self.root.destroy()

# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ron's Market Breakout Scanner")
    parser.add_argument("--console", action="store_true", help="Force console mode")
    args    = parser.parse_args()
    scanner = MarketScanner()
    if args.console or not TKINTER_AVAILABLE:
        scanner.run_console()
    else:
        scanner.run_gui()
