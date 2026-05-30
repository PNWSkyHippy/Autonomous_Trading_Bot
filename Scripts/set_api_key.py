"""
Scripts/set_api_key.py
======================
Add, update, or remove encrypted API keys in the trading bot database.

Keys stored here take priority over .env / config.py.
Values are Fernet-encrypted using the same key as dashboard passwords.

Usage
-----
  python Scripts/set_api_key.py                            # interactive wizard
  python Scripts/set_api_key.py --service kraken --key api_key
  python Scripts/set_api_key.py --service kraken --key secret_key
  python Scripts/set_api_key.py --list                     # show stored services
  python Scripts/set_api_key.py --delete --service kraken --key api_key
  python Scripts/set_api_key.py --migrate                  # import all keys from .env → DB

Known service / key_name pairs
-------------------------------
  alpaca      api_key, secret_key, base_url
  coinbase    api_key, secret_key
  kraken      api_key, secret_key
  ibkr        account
  anthropic   api_key
  massive     api_key
  email       sender, password, recipient
"""

import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT    = Path(__file__).parent.parent
DB_FILE = ROOT / "data" / "trading_bot.db"

# Known keys grouped by service (for the wizard and --migrate)
KNOWN_KEYS = {
    "alpaca":        ["api_key", "secret_key", "base_url"],
    "coinbase":      ["api_key", "secret_key"],
    "kraken":        ["api_key", "secret_key"],
    "ibkr":          ["account"],
    "anthropic":     ["api_key"],
    "massive":       ["api_key"],
    "alphavantage":  ["api_key"],
    "trading_bot":   ["bot_api_key"],
    "email":         ["sender", "password", "recipient"],
}

# .env variable names that map to (service, key_name)
ENV_MAP = {
    "ALPACA_API_KEY":      ("alpaca",    "api_key"),
    "ALPACA_SECRET_KEY":   ("alpaca",    "secret_key"),
    "ALPACA_BASE_URL":     ("alpaca",    "base_url"),
    "COINBASE_API_KEY":    ("coinbase",  "api_key"),
    "COINBASE_SECRET_KEY": ("coinbase",  "secret_key"),
    "KRAKEN_API_KEY":      ("kraken",    "api_key"),
    "KRAKEN_SECRET_KEY":   ("kraken",    "secret_key"),
    "IBKR_ACCOUNT":        ("ibkr",      "account"),
    "ANTHROPIC_API_KEY":   ("anthropic", "api_key"),
    "MASSIVE_API_KEY":     ("massive",   "api_key"),
    "EMAIL_SENDER":        ("email",     "sender"),
    "EMAIL_PASSWORD":      ("email",     "password"),
    "EMAIL_RECIPIENT":     ("email",     "recipient"),
}

SENSITIVE = {"secret_key", "api_key", "password"}   # hide input while typing


def _get_fernet():
    try:
        import keyring as _kr
        from cryptography.fernet import Fernet as _F
        raw = _kr.get_password("trading_bot_v2", "fernet_key")
        if not raw:
            print("ERROR: Fernet key not found.")
            print("Run:  python Scripts/setup_encryption.py")
            sys.exit(1)
        return _F(raw.encode())
    except ImportError as e:
        print(f"ERROR: Missing package — {e}")
        print("Run:  pip install cryptography keyring")
        sys.exit(1)


def _encrypt(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt_stored(enc: str) -> str:
    if enc.startswith("plain:"):
        return enc[6:]
    return _get_fernet().decrypt(enc.encode()).decode()


def _conn():
    if not DB_FILE.exists():
        print(f"ERROR: Database not found at {DB_FILE}")
        print("Start the bot or dashboard once to initialise the DB.")
        sys.exit(1)
    c = sqlite3.connect(str(DB_FILE))
    c.row_factory = sqlite3.Row
    return c


def _upsert(service: str, key_name: str, value: str) -> None:
    enc = _encrypt(value)
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    conn.execute("""
        INSERT INTO api_keys (service, key_name, value_enc, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(service, key_name) DO UPDATE SET
            value_enc  = excluded.value_enc,
            updated_at = excluded.updated_at
    """, (service, key_name, enc, now))
    conn.commit()
    conn.close()


def cmd_set(service: str, key_name: str) -> None:
    prompt = f"  Value for {service}/{key_name}: "
    if key_name in SENSITIVE:
        value = getpass.getpass(prompt)
    else:
        value = input(prompt).strip()
    if not value:
        print("  Empty value — nothing saved.")
        return
    _upsert(service, key_name)  # will fail — fix below
    _upsert_fix(service, key_name, value)


def _upsert_fix(service: str, key_name: str, value: str) -> None:
    _upsert(service, key_name, value)
    print(f"  [OK] {service}/{key_name} saved (encrypted).")


def cmd_list() -> None:
    conn = _conn()
    rows = conn.execute(
        "SELECT service, key_name, updated_at FROM api_keys ORDER BY service, key_name"
    ).fetchall()
    conn.close()
    if not rows:
        print("\n  No API keys stored in DB yet.\n")
        return
    print(f"\n  {'SERVICE':<14} {'KEY NAME':<16} UPDATED")
    print("  " + "─" * 52)
    for r in rows:
        print(f"  {r['service']:<14} {r['key_name']:<16} {r['updated_at'][:19]}")
    print()


def cmd_delete(service: str, key_name: str) -> None:
    conn = _conn()
    cur = conn.execute(
        "DELETE FROM api_keys WHERE service=? AND key_name=?", (service, key_name)
    )
    conn.commit()
    conn.close()
    if cur.rowcount:
        print(f"\n  [OK] {service}/{key_name} deleted. Will fall back to .env on next start.\n")
    else:
        print(f"\n  Not found: {service}/{key_name}\n")


def cmd_wizard() -> None:
    """Interactive wizard — lets you pick a service and set all its keys."""
    print()
    services = list(KNOWN_KEYS.keys())
    print("  Which service do you want to configure?")
    for i, s in enumerate(services, 1):
        print(f"    {i}. {s}")
    print(f"    {len(services)+1}. Custom (enter service/key manually)")
    print()
    choice = input("  Choice: ").strip()
    try:
        idx = int(choice) - 1
    except ValueError:
        print("  Invalid choice.\n")
        return

    if idx == len(services):
        service  = input("  Service name: ").strip().lower()
        key_name = input("  Key name:     ").strip().lower()
        if not service or not key_name:
            print("  Cancelled.\n")
            return
        _upsert_fix(service, key_name, getpass.getpass(f"  Value: ") if key_name in SENSITIVE else input(f"  Value: ").strip())
        return

    if idx < 0 or idx >= len(services):
        print("  Invalid choice.\n")
        return

    service  = services[idx]
    key_names = KNOWN_KEYS[service]
    print(f"\n  Configuring: {service}")
    print(f"  (press Enter to skip a key)\n")
    for kn in key_names:
        prompt = f"  {kn}: "
        if kn in SENSITIVE:
            value = getpass.getpass(prompt)
        else:
            value = input(prompt).strip()
        if value:
            _upsert_fix(service, kn, value)
        else:
            print(f"  Skipped {kn}.")
    print()


def cmd_migrate() -> None:
    """Read keys from .env and store them in the DB (encrypted)."""
    try:
        from dotenv import dotenv_values
        env_path = ROOT / ".env"
        if not env_path.exists():
            print("\n  .env not found — nothing to migrate.\n")
            return
        vals = dotenv_values(str(env_path))
    except ImportError:
        # fallback: parse .env manually
        vals = {}
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    vals[k.strip()] = v.strip()

    migrated = 0
    skipped  = 0
    for env_var, (service, key_name) in ENV_MAP.items():
        raw = vals.get(env_var, "").strip()
        if not raw:
            skipped += 1
            continue
        # Decrypt Fernet token from .env before re-encrypting for DB
        if raw.startswith("gAAAAA"):
            try:
                raw = _get_fernet().decrypt(raw.encode()).decode()
            except Exception:
                print(f"  WARN: Could not decrypt {env_var} — skipping")
                skipped += 1
                continue
        _upsert_fix(service, key_name, raw)
        migrated += 1

    print(f"\n  Migration complete: {migrated} key(s) stored, {skipped} skipped (blank).")
    print("  Keys are now in DB. You can leave .env as fallback or clear it.\n")


# ── Fix the broken cmd_set above ──────────────────────────────────────────────
def cmd_set(service: str, key_name: str) -> None:  # noqa: F811
    prompt = f"  Value for {service}/{key_name}: "
    if key_name in SENSITIVE:
        value = getpass.getpass(prompt)
    else:
        value = input(prompt).strip()
    if not value:
        print("  Empty value — nothing saved.")
        return
    _upsert_fix(service, key_name, value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage encrypted API keys in the bot DB")
    parser.add_argument("--service",  help="Service name  (e.g. kraken)")
    parser.add_argument("--key",      help="Key name      (e.g. api_key)")
    parser.add_argument("--list",     action="store_true", help="List all stored keys")
    parser.add_argument("--delete",   action="store_true", help="Delete a key from DB")
    parser.add_argument("--migrate",  action="store_true", help="Import all keys from .env → DB")
    args = parser.parse_args()

    print()
    print("=" * 50)
    print("  API Key Manager")
    print("=" * 50)

    if args.list:
        cmd_list()
    elif args.delete:
        if not args.service or not args.key:
            print("  --delete requires --service and --key\n")
            sys.exit(1)
        cmd_delete(args.service, args.key)
    elif args.migrate:
        cmd_migrate()
    elif args.service and args.key:
        cmd_set(args.service, args.key)
    else:
        cmd_wizard()
