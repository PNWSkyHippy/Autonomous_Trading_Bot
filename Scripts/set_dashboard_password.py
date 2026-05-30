"""
set_dashboard_password.py
=========================
CLI helper to manage dashboard user accounts.

Commands
--------
  python Scripts/set_dashboard_password.py                 # change/add a user
  python Scripts/set_dashboard_password.py --list          # list users
  python Scripts/set_dashboard_password.py --delete <user> # remove a user
  python Scripts/set_dashboard_password.py --reset         # delete ALL users
                                                           # (triggers first-run wizard on next start)

Users are stored in  data/dashboard_users.json
(sha256-salted hashes — plaintext passwords are never stored).
"""

import argparse
import getpass
import hashlib
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT     = Path(__file__).parent.parent
DB_FILE  = ROOT / "data" / "trading_bot.db"   # adjust if DB_PATH differs in config


def _get_fernet():
    try:
        import keyring as _kr
        from cryptography.fernet import Fernet as _F
        raw = _kr.get_password("trading_bot_v2", "fernet_key")
        return _F(raw.encode()) if raw else None
    except Exception:
        return None


def _enc_hash(h: str) -> str:
    f = _get_fernet()
    return f.encrypt(h.encode()).decode() if f else "plain:" + h


def _dec_hash(stored: str) -> str:
    if stored.startswith("plain:"):
        return stored[6:]
    f = _get_fernet()
    if not f:
        raise RuntimeError("Fernet key not set up. Run: python Scripts/setup_encryption.py")
    return f.decrypt(stored.encode()).decode()


def _load() -> dict:
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM dashboard_users").fetchall()
        conn.close()
        result = {}
        for r in rows:
            try:
                h = _dec_hash(r["hash_enc"])
            except Exception:
                h = ""
            result[r["username"]] = {"hash": h, "role": r["role"], "force_change": bool(r["force_change"])}
        return result
    except Exception:
        return {}


def _save(users: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(str(DB_FILE))
        for username, rec in users.items():
            enc   = _enc_hash(rec.get("hash", ""))
            role  = rec.get("role", "user")
            force = 1 if rec.get("force_change") else 0
            conn.execute("""
                INSERT INTO dashboard_users (username, hash_enc, role, force_change, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    hash_enc=excluded.hash_enc, role=excluded.role,
                    force_change=excluded.force_change, updated_at=excluded.updated_at
            """, (username, enc, role, force, now, now))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ERROR saving to DB: {e}")


def _hash(password: str) -> str:
    salt   = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"sha256:{salt}:{digest}"


def cmd_set_password(username: str | None = None) -> None:
    users = _load()

    # Username
    if not username:
        default = "admin" if not users else next(iter(users))
        username = input(f"  Username [{default}]: ").strip() or default

    exists = username in users

    # Password
    while True:
        pw1 = getpass.getpass("  New password: ")
        if len(pw1) < 8:
            print("  Password must be at least 8 characters. Try again.")
            continue
        pw2 = getpass.getpass("  Confirm password: ")
        if pw1 != pw2:
            print("  Passwords do not match. Try again.")
            continue
        break

    role = "admin" if username == "admin" else "user"
    if exists and isinstance(users[username], dict):
        role = users[username].get("role", role)

    users[username] = {"hash": _hash(pw1), "role": role, "force_change": False}
    _save(users)
    action = "updated" if exists else "created"
    print(f"\n  [OK] Account '{username}' {action} (role: {role}).")
    print("  Restart the dashboard to apply:  python web_dashboard.py\n")


def cmd_list() -> None:
    users = _load()
    if not users:
        print("\n  No accounts found (first-run wizard will appear on next start).\n")
        return
    print(f"\n  Dashboard accounts ({len(users)}):")
    for u in users:
        print(f"    • {u}")
    print()


def cmd_delete(username: str) -> None:
    users = _load()
    if username not in users:
        print(f"\n  User '{username}' not found.\n")
        return
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("DELETE FROM dashboard_users WHERE username=?", (username,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"\n  ERROR: {e}\n")
        return
    users.pop(username)
    print(f"\n  [OK] User '{username}' deleted.")
    if not users:
        print("  No accounts remain — first-run wizard will appear on next dashboard start.")
    print()


def cmd_reset() -> None:
    answer = input("  Type YES to delete ALL accounts (triggers first-run wizard): ").strip()
    if answer != "YES":
        print("  Cancelled.\n")
        return
    try:
        conn = sqlite3.connect(str(DB_FILE))
        conn.execute("DELETE FROM dashboard_users")
        conn.commit()
        conn.close()
        print("\n  [OK] All accounts deleted. Restart the dashboard to run first-run setup.\n")
    except Exception as e:
        print(f"\n  ERROR: {e}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage dashboard user accounts")
    parser.add_argument("--list",   action="store_true",  help="List all users")
    parser.add_argument("--delete", metavar="USER",       help="Delete a user")
    parser.add_argument("--reset",  action="store_true",  help="Delete ALL users (re-triggers setup wizard)")
    parser.add_argument("--user",   metavar="USER",       help="Username for password change (skips prompt)")
    args = parser.parse_args()

    print()
    print("=" * 50)
    print("  Dashboard Account Manager")
    print("=" * 50)

    if args.list:
        cmd_list()
    elif args.delete:
        cmd_delete(args.delete)
    elif args.reset:
        cmd_reset()
    else:
        cmd_set_password(args.user)
