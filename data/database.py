"""
=============================================================
  DATABASE LAYER
  Manages all persistent storage: trades, capital, tax records,
  daily summaries, withdrawal history, strategy results,
  fund events, broker balance tracking, and Claude chat actions.

  TIMESTAMP POLICY:
  All timestamps stored in local machine time (Pacific Time).
  This matches the log files and makes cross-referencing trivial.
  Migration script: Scripts/migrate_timestamps_to_pt.py
=============================================================
"""

import sqlite3
import json
import logging
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

import config

logger = logging.getLogger(__name__)


def _now_local() -> datetime:
    """
    Return current local time (Pacific Time on Ron's machine).
    Used for all DB timestamp writes so they match the log files.
    Previously used datetime.utcnow() which was 7-8 hours ahead.
    """
    return datetime.now()


class Database:
    """Central database manager using SQLite (local, no server needed)."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"DB error: {e}")
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Create all tables if they don't exist."""
        with self._conn() as conn:
            conn.executescript("""
                -- Capital tracking
                CREATE TABLE IF NOT EXISTS capital (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT NOT NULL,
                    total_capital   REAL NOT NULL,
                    available_cash  REAL NOT NULL,
                    invested_value  REAL NOT NULL,
                    daily_pnl       REAL DEFAULT 0,
                    total_pnl       REAL DEFAULT 0,
                    note            TEXT
                );

                -- Every trade ever taken
                CREATE TABLE IF NOT EXISTS trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id        TEXT UNIQUE NOT NULL,
                    symbol          TEXT NOT NULL,
                    asset_class     TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    entry_time      TEXT,
                    exit_time       TEXT,
                    entry_price     REAL,
                    exit_price      REAL,
                    quantity        REAL NOT NULL,
                    position_value  REAL NOT NULL,
                    stop_loss       REAL,
                    take_profit     REAL,
                    pnl             REAL DEFAULT 0,
                    pnl_pct         REAL DEFAULT 0,
                    fees_paid       REAL DEFAULT 0,
                    exit_reason     TEXT,
                    signal_score    REAL,
                    indicators_json TEXT,
                    broker          TEXT,
                    strategy_name   TEXT DEFAULT 'original',
                    is_overnight    INTEGER DEFAULT 0,
                    broker_order_id  TEXT,
                    tp_hit_count     INTEGER DEFAULT 0,
                    entry_timeframe  TEXT DEFAULT '5Min'
                );

                -- Advisory AI signal reviews.
                -- These are written separately from trades.indicators_json because
                -- advisory review runs after execution so it cannot delay entries.
                CREATE TABLE IF NOT EXISTS ai_signal_reviews (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp           TEXT NOT NULL,
                    trade_id            TEXT,
                    symbol              TEXT NOT NULL,
                    asset_class          TEXT,
                    direction           TEXT,
                    strategy_name        TEXT,
                    signal_score         REAL,
                    reviewer            TEXT DEFAULT 'claude',
                    mode                TEXT DEFAULT 'advisory',
                    decision            TEXT,
                    confidence          INTEGER,
                    reasoning           TEXT,
                    suggested_size_pct   REAL,
                    warnings_json        TEXT,
                    elapsed_ms           INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_ai_signal_reviews_trade_id
                    ON ai_signal_reviews(trade_id);
                CREATE INDEX IF NOT EXISTS idx_ai_signal_reviews_symbol_time
                    ON ai_signal_reviews(symbol, timestamp);

                -- Daily session summaries
                CREATE TABLE IF NOT EXISTS daily_summaries (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date          TEXT UNIQUE NOT NULL,
                    starting_capital    REAL NOT NULL,
                    ending_capital      REAL NOT NULL,
                    daily_pnl           REAL NOT NULL,
                    daily_pnl_pct       REAL NOT NULL,
                    total_trades        INTEGER DEFAULT 0,
                    winning_trades      INTEGER DEFAULT 0,
                    losing_trades       INTEGER DEFAULT 0,
                    win_rate            REAL DEFAULT 0,
                    largest_win         REAL DEFAULT 0,
                    largest_loss        REAL DEFAULT 0,
                    trading_halted      INTEGER DEFAULT 0,
                    halt_reason         TEXT,
                    goal_met            INTEGER DEFAULT 0
                );

                -- Tax ledger (IRS Form 8949 compatible)
                CREATE TABLE IF NOT EXISTS tax_ledger (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id        TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    open_date       TEXT NOT NULL,
                    close_date      TEXT NOT NULL,
                    proceeds        REAL NOT NULL,
                    cost_basis      REAL NOT NULL,
                    fees_paid       REAL NOT NULL DEFAULT 0,
                    gain_loss       REAL NOT NULL,
                    hold_days       INTEGER NOT NULL,
                    term            TEXT NOT NULL,
                    tax_year        INTEGER NOT NULL
                );

                -- Withdrawal history
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT NOT NULL,
                    amount          REAL NOT NULL,
                    reason          TEXT,
                    capital_before  REAL NOT NULL,
                    capital_after   REAL NOT NULL
                );

                -- Bot state & settings (key-value store)
                CREATE TABLE IF NOT EXISTS bot_state (
                    key     TEXT PRIMARY KEY,
                    value   TEXT NOT NULL,
                    updated TEXT NOT NULL
                );

                -- Daily session tracking
                CREATE TABLE IF NOT EXISTS session_state (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date            TEXT UNIQUE NOT NULL,
                    consecutive_losses      INTEGER DEFAULT 0,
                    trades_today            INTEGER DEFAULT 0,
                    pnl_today               REAL DEFAULT 0,
                    trading_halted          INTEGER DEFAULT 0,
                    halt_reason             TEXT,
                    starting_capital_today  REAL DEFAULT 0
                );

                -- Strategy performance tracking
                CREATE TABLE IF NOT EXISTS strategy_results (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name   TEXT NOT NULL,
                    trade_date      TEXT NOT NULL,
                    pnl             REAL NOT NULL,
                    won             INTEGER NOT NULL,
                    recorded_at     TEXT DEFAULT CURRENT_TIMESTAMP
                );

                -- Fund injection / withdrawal tracking per broker
                CREATE TABLE IF NOT EXISTS fund_events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT NOT NULL,
                    broker_name     TEXT NOT NULL,
                    event_type      TEXT NOT NULL,
                    amount          REAL NOT NULL,
                    reason          TEXT,
                    balance_before  REAL NOT NULL,
                    balance_after   REAL NOT NULL
                );

                -- Claude AI chat action log
                CREATE TABLE IF NOT EXISTS chat_actions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT NOT NULL,
                    action_date     TEXT NOT NULL,
                    user_message    TEXT,
                    action_type     TEXT NOT NULL,
                    action_detail   TEXT,
                    result          TEXT
                );

                -- Encrypted API key store
                -- value_enc: Fernet-encrypted plaintext value (same key as passwords).
                -- Falls back to config.py / .env if the row is missing.
                CREATE TABLE IF NOT EXISTS api_keys (
                    service     TEXT NOT NULL,
                    key_name    TEXT NOT NULL,
                    value_enc   TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    PRIMARY KEY (service, key_name)
                );

                -- Dashboard user accounts
                -- hash_enc: Fernet-encrypted "sha256:<salt>:<hex>" string.
                -- If the Fernet key is not yet set up, hash_enc stores the
                -- sha256 hash in plaintext prefixed with "plain:" so the bot
                -- can still start; it is re-encrypted on next successful login.
                CREATE TABLE IF NOT EXISTS dashboard_users (
                    username        TEXT PRIMARY KEY,
                    hash_enc        TEXT NOT NULL,
                    role            TEXT NOT NULL DEFAULT 'user',
                    force_change    INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
            """)
            # Column migrations for existing databases (ALTER TABLE ignores if already present)
            for sql in [
                "ALTER TABLE trades    ADD COLUMN fees_paid REAL DEFAULT 0",
                "ALTER TABLE tax_ledger ADD COLUMN fees_paid REAL NOT NULL DEFAULT 0",
            ]:
                try:
                    conn.execute(sql)
                except Exception:
                    pass  # column already exists

            # Safe migrations
            migrations = [
                "ALTER TABLE trades ADD COLUMN strategy_name    TEXT    DEFAULT 'original'",
                "ALTER TABLE trades ADD COLUMN is_overnight     INTEGER DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN broker_order_id  TEXT",
                "ALTER TABLE trades ADD COLUMN tp_hit_count     INTEGER DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN entry_timeframe  TEXT    DEFAULT '5Min'",
                "ALTER TABLE session_state ADD COLUMN starting_capital_today REAL DEFAULT 0",
            ]
            for sql in migrations:
                try:
                    conn.execute(sql)
                except Exception:
                    pass

        logger.info("Database initialized successfully.")

    # --- CAPITAL -------------------------------------------------------

    def log_capital(self, total: float, available: float, invested: float,
                    daily_pnl: float = 0, total_pnl: float = 0, note: str = ""):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO capital
                    (timestamp, total_capital, available_cash,
                     invested_value, daily_pnl, total_pnl, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (_now_local().isoformat(), total, available,
                  invested, daily_pnl, total_pnl, note))

    def record_capital_snapshot(self, total: float):
        self.log_capital(total=total, available=total, invested=0)

    def get_latest_capital(self) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM capital ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    # --- TRADES --------------------------------------------------------

    def open_trade(self, trade: Dict[str, Any]):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO trades
                    (trade_id, symbol, asset_class, direction, status,
                     entry_time, entry_price, quantity, position_value,
                     stop_loss, take_profit, signal_score,
                     indicators_json, broker, strategy_name, entry_timeframe)
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade["trade_id"], trade["symbol"], trade["asset_class"],
                trade["direction"], trade["entry_time"], trade["entry_price"],
                trade["quantity"], trade["position_value"],
                trade["stop_loss"], trade["take_profit"],
                trade.get("signal_score"),
                json.dumps(trade.get("indicators", {})),
                trade.get("broker", "alpaca"),
                trade.get("strategy_name", "original"),
                trade.get("entry_timeframe", "5Min"),
            ))
        logger.info(f"Trade opened: {trade['symbol']} @ {trade['entry_price']}")

    def close_trade(self, trade_id: str, exit_price: float,
                    exit_reason: str, pnl: float, pnl_pct: float,
                    fees_paid: float = 0.0):
        with self._conn() as conn:
            conn.execute("""
                UPDATE trades SET
                    status      = 'closed',
                    exit_time   = ?,
                    exit_price  = ?,
                    exit_reason = ?,
                    pnl         = ?,
                    pnl_pct     = ?,
                    fees_paid   = ?
                WHERE trade_id = ?
            """, (_now_local().isoformat(), exit_price, exit_reason,
                  pnl, pnl_pct, fees_paid, trade_id))

    def update_trade_order_ids(self, trade_id: str,
                               stop_order_id: str = None,
                               tp_order_id:   str = None):
        """Store broker order IDs for stop and TP orders."""
        sets, vals = [], []
        if stop_order_id is not None:
            sets.append("stop_order_id = ?"); vals.append(stop_order_id)
        if tp_order_id is not None:
            sets.append("tp_order_id = ?");   vals.append(tp_order_id)
        if not sets:
            return
        vals.append(trade_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE trades SET {', '.join(sets)} WHERE trade_id = ?", vals
            )

    def update_trade_broker(self, trade_id: str, broker: str):
        """Correct the broker field when a fallback broker actually filled/closed."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET broker = ? WHERE trade_id = ?",
                (broker, trade_id)
            )

    def record_ai_signal_review(self, review: Dict[str, Any]):
        """Persist an advisory/strict AI review for later trade-outcome analysis."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO ai_signal_reviews
                    (timestamp, trade_id, symbol, asset_class, direction,
                     strategy_name, signal_score, reviewer, mode, decision,
                     confidence, reasoning, suggested_size_pct, warnings_json,
                     elapsed_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                review.get("timestamp") or _now_local().isoformat(),
                review.get("trade_id"),
                review["symbol"],
                review.get("asset_class"),
                review.get("direction"),
                review.get("strategy_name"),
                review.get("signal_score"),
                review.get("reviewer", "claude"),
                review.get("mode", "advisory"),
                review.get("decision"),
                review.get("confidence"),
                review.get("reasoning"),
                review.get("suggested_size_pct"),
                json.dumps(review.get("warnings", [])),
                review.get("elapsed_ms"),
            ))

    def increment_tp_hit_count(self, trade_id: str) -> int:
        """Increment tp_hit_count for a trade and return the new value."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET tp_hit_count = tp_hit_count + 1 WHERE trade_id = ?",
                (trade_id,)
            )
            row = conn.execute(
                "SELECT tp_hit_count FROM trades WHERE trade_id = ?",
                (trade_id,)
            ).fetchone()
            return int(row["tp_hit_count"]) if row else 0

    def update_trade_levels(self, trade_id: str,
                             stop_loss: float = None,
                             take_profit: float = None):
        with self._conn() as conn:
            if stop_loss and take_profit:
                conn.execute(
                    "UPDATE trades SET stop_loss=?, take_profit=? WHERE trade_id=?",
                    (stop_loss, take_profit, trade_id)
                )
            elif stop_loss:
                conn.execute(
                    "UPDATE trades SET stop_loss=? WHERE trade_id=?",
                    (stop_loss, trade_id)
                )
            elif take_profit:
                conn.execute(
                    "UPDATE trades SET take_profit=? WHERE trade_id=?",
                    (take_profit, trade_id)
                )

    def get_open_trades(self) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='open'"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_trades_for_date(self, trade_date: str) -> List[Dict]:
        """Return closed trades whose exit_time falls on trade_date.
        Using exit_time means trades that opened yesterday but closed today
        appear in today's log — which is the correct behaviour for a trade log."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='closed' AND date(exit_time)=? ORDER BY exit_time",
                (trade_date,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_closed_trades(self, limit: int = 500) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='closed' "
                "ORDER BY exit_time DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # --- SESSION STATE -------------------------------------------------

    def get_session(self, session_date: str = None) -> Dict:
        today = session_date or date.today().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM session_state WHERE session_date=?", (today,)
            ).fetchone()
            if row:
                return dict(row)
            conn.execute(
                "INSERT INTO session_state (session_date) VALUES (?)", (today,)
            )
            return {
                "session_date":          today,
                "consecutive_losses":    0,
                "trades_today":          0,
                "pnl_today":             0.0,
                "trading_halted":        0,
                "halt_reason":           None,
                "starting_capital_today": 0.0,
            }

    def update_session(self, session_date: str, **kwargs):
        set_clause = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values()) + [session_date]
        with self._conn() as conn:
            conn.execute(
                f"UPDATE session_state SET {set_clause} WHERE session_date=?",
                values
            )

    def set_starting_capital_today(self, capital: float):
        today = date.today().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT starting_capital_today FROM session_state WHERE session_date=?",
                (today,)
            ).fetchone()
            if row and float(row["starting_capital_today"] or 0) > 0:
                return
            conn.execute(
                "UPDATE session_state SET starting_capital_today=? WHERE session_date=?",
                (capital, today)
            )
            logger.info(f"Starting capital locked for today: ${capital:,.2f}")

    def get_starting_capital_today(self) -> float:
        today = date.today().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT starting_capital_today FROM session_state WHERE session_date=?",
                (today,)
            ).fetchone()
            return float(row["starting_capital_today"] or 0.0) if row else 0.0

    def reset_session_state(self):
        today = date.today().isoformat()
        self.update_session(today,
            trading_halted         = 0,
            halt_reason            = None,
            consecutive_losses     = 0,
            pnl_today              = 0.0,
            trades_today           = 0,
            starting_capital_today = 0.0
        )

    # --- DAILY SUMMARY -------------------------------------------------

    def save_daily_summary(self, summary: Dict[str, Any]):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_summaries
                    (trade_date, starting_capital, ending_capital, daily_pnl,
                     daily_pnl_pct, total_trades, winning_trades, losing_trades,
                     win_rate, largest_win, largest_loss, trading_halted,
                     halt_reason, goal_met)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                summary["trade_date"], summary["starting_capital"],
                summary["ending_capital"], summary["daily_pnl"],
                summary["daily_pnl_pct"], summary["total_trades"],
                summary["winning_trades"], summary["losing_trades"],
                summary["win_rate"], summary["largest_win"],
                summary["largest_loss"], summary.get("trading_halted", 0),
                summary.get("halt_reason"), summary.get("goal_met", 0)
            ))

    def get_daily_summaries(self, days: int = 30) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_summaries ORDER BY trade_date DESC LIMIT ?",
                (days,)
            ).fetchall()
            return [dict(r) if row else None for row in rows]

    def get_daily_summaries(self, days: int = 30) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_summaries ORDER BY trade_date DESC LIMIT ?",
                (days,)
            ).fetchall()
            return [dict(r) for r in rows]

    # --- TAX LEDGER ----------------------------------------------------

    def record_tax_event(self, trade: Dict[str, Any]):
        if trade["status"] != "closed" or not trade.get("exit_time"):
            return
        open_dt   = datetime.fromisoformat(trade["entry_time"])
        close_dt  = datetime.fromisoformat(trade["exit_time"])
        hold_days = (close_dt - open_dt).days
        proceeds  = trade["exit_price"] * trade["quantity"]
        cost_basis= trade["entry_price"] * trade["quantity"]
        fees_paid = float(trade.get("fees_paid") or 0.0)
        # gain_loss net of fees — correct for tax purposes (fees reduce taxable gain)
        gain_loss = proceeds - cost_basis - fees_paid
        term      = "long" if hold_days >= 365 else "short"
        tax_year  = close_dt.year
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO tax_ledger
                    (trade_id, symbol, open_date, close_date, proceeds,
                     cost_basis, fees_paid, gain_loss, hold_days, term, tax_year)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade["trade_id"], trade["symbol"],
                open_dt.date().isoformat(), close_dt.date().isoformat(),
                round(proceeds, 4), round(cost_basis, 4),
                round(fees_paid, 4), round(gain_loss, 4),
                hold_days, term, tax_year
            ))

    def get_tax_year_summary(self, year: int) -> Dict:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*)                                               AS total_trades,
                    SUM(CASE WHEN term='short' THEN gain_loss ELSE 0 END) AS short_term_gains,
                    SUM(CASE WHEN term='long'  THEN gain_loss ELSE 0 END) AS long_term_gains,
                    SUM(gain_loss)                                         AS total_gains,
                    SUM(CASE WHEN gain_loss > 0 THEN gain_loss ELSE 0 END) AS gross_profits,
                    SUM(CASE WHEN gain_loss < 0 THEN gain_loss ELSE 0 END) AS gross_losses
                FROM tax_ledger WHERE tax_year=?
            """, (year,)).fetchone()
            return dict(row) if row else {}

    def export_8949_csv(self, year: int, filepath: str):
        import csv
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT symbol, open_date, close_date, proceeds,
                       cost_basis, gain_loss, term
                FROM tax_ledger WHERE tax_year=?
                ORDER BY close_date
            """, (year,)).fetchall()
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Description", "Date Acquired", "Date Sold",
                             "Proceeds", "Cost Basis", "Gain/Loss", "Term"])
            for r in rows:
                writer.writerow(list(r))
        logger.info(f"Form 8949 CSV exported: {filepath}")

    # --- WITHDRAWALS ---------------------------------------------------

    def record_withdrawal(self, amount: float, reason: str,
                           capital_before: float, capital_after: float):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO withdrawals
                    (timestamp, amount, reason, capital_before, capital_after)
                VALUES (?, ?, ?, ?, ?)
            """, (_now_local().isoformat(), amount, reason,
                  capital_before, capital_after))
        logger.info(f"Withdrawal recorded: ${amount:.2f}")

    # --- BOT STATE -----------------------------------------------------

    def set_state(self, key: str, value: Any):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO bot_state (key, value, updated)
                VALUES (?, ?, ?)
            """, (key, json.dumps(value), _now_local().isoformat()))

    def get_state(self, key: str, default=None) -> Any:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row["value"]) if row else default

    # --- STRATEGY TRACKING ---------------------------------------------

    def record_strategy_result(self, strategy_name: str, trade_date: str,
                                pnl: float, won: bool):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO strategy_results "
                "(strategy_name, trade_date, pnl, won) VALUES (?, ?, ?, ?)",
                (strategy_name, trade_date, pnl, int(won))
            )

    def get_strategy_results(self, strategy_name: str,
                              limit: int = 500) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT strategy_name, trade_date, pnl, won "
                "FROM strategy_results "
                "WHERE strategy_name = ? ORDER BY id DESC LIMIT ?",
                (strategy_name, limit)
            ).fetchall()
            return [
                {"strategy_name": r[0], "trade_date": r[1],
                 "pnl": r[2], "won": bool(r[3])}
                for r in rows
            ]

    # --- FUND EVENTS ---------------------------------------------------

    def record_fund_event(self, broker_name: str, event_type: str,
                           amount: float, reason: str,
                           balance_before: float, balance_after: float):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO fund_events
                    (timestamp, broker_name, event_type, amount, reason,
                     balance_before, balance_after)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (_now_local().isoformat(), broker_name, event_type,
                  amount, reason, balance_before, balance_after))
        logger.info(f"Fund event: {event_type} ${amount:.2f} @ {broker_name}")

    def get_fund_history(self, broker_name: str = None,
                          limit: int = 100) -> List[Dict]:
        with self._conn() as conn:
            if broker_name:
                rows = conn.execute(
                    "SELECT * FROM fund_events WHERE broker_name=? "
                    "ORDER BY id DESC LIMIT ?",
                    (broker_name, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fund_events ORDER BY id DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    # --- CLAUDE CHAT ACTION TRACKING -----------------------------------

    def log_chat_action(self, user_message: str, action_type: str,
                         action_detail: str = "", result: str = ""):
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO chat_actions
                    (timestamp, action_date, user_message, action_type,
                     action_detail, result)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                _now_local().isoformat(), today,
                user_message[:200] if user_message else "",
                action_type, action_detail[:500] if action_detail else "",
                result[:200] if result else ""
            ))

    def get_chat_actions_for_date(self, action_date: str = None) -> List[Dict]:
        today = action_date or date.today().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_actions WHERE action_date=? ORDER BY id",
                (today,)
            ).fetchall()
            return [dict(r) for r in rows]


# Singleton instance
db = Database()
