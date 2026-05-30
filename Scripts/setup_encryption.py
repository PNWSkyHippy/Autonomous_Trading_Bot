"""
Scripts/setup_encryption.py
============================
First-run encryption setup for Trading Bot v2.

What this does
--------------
1. Generates a unique Fernet encryption key for this machine.
2. Stores it securely in the OS credential store:
     Windows  → Windows Credential Manager
     macOS    → Keychain
     Linux    → Secret Service (libsecret) or encrypted file fallback
3. Prints step-by-step instructions for encrypting your API keys.

Run once per machine before starting the bot for the first time:
    python Scripts/setup_encryption.py

If a key already exists it will NOT be overwritten unless you pass --rotate.
Rotating the key invalidates all previously encrypted values — you will need
to re-encrypt all API keys and reset all dashboard passwords.

    python Scripts/setup_encryption.py --rotate    # generate new key (danger)
    python Scripts/setup_encryption.py --verify    # confirm the key works
    python Scripts/setup_encryption.py --show      # print the key (keep private)
"""

import argparse
import sys
from pathlib import Path

try:
    import keyring
except ImportError:
    print("ERROR: 'keyring' package not installed.")
    print("Run:  pip install keyring")
    sys.exit(1)

try:
    from cryptography.fernet import Fernet
except ImportError:
    print("ERROR: 'cryptography' package not installed.")
    print("Run:  pip install cryptography")
    sys.exit(1)

SERVICE  = "trading_bot_v2"
CRED_KEY = "fernet_key"
ROOT     = Path(__file__).parent.parent


def _load_key() -> str | None:
    return keyring.get_password(SERVICE, CRED_KEY)


def _store_key(key: str) -> None:
    keyring.set_password(SERVICE, CRED_KEY, key)


def _generate_and_store() -> str:
    key = Fernet.generate_key().decode()
    _store_key(key)
    return key


def cmd_setup(rotate: bool = False) -> None:
    existing = _load_key()

    if existing and not rotate:
        print()
        print("=" * 60)
        print("  Encryption key already exists.")
        print("=" * 60)
        print()
        print("  The Fernet key is already stored in your OS credential store.")
        print("  Nothing to do — your setup is complete.")
        print()
        print("  To verify the key works:")
        print("    python Scripts/setup_encryption.py --verify")
        print()
        print("  To REPLACE the key (invalidates all encrypted values):")
        print("    python Scripts/setup_encryption.py --rotate")
        print()
        return

    if rotate and existing:
        print()
        print("  ⚠️  WARNING: Rotating the key will invalidate ALL previously")
        print("  encrypted API keys and dashboard passwords.")
        print("  You will need to re-encrypt every key and reset all passwords.")
        print()
        confirm = input("  Type ROTATE to confirm: ").strip()
        if confirm != "ROTATE":
            print("  Cancelled.")
            return

    key = _generate_and_store()
    action = "rotated" if (rotate and existing) else "generated"

    print()
    print("=" * 60)
    print(f"  Encryption key {action} and stored successfully.")
    print("=" * 60)
    print()
    print("  Stored in:  OS credential store")
    print(f"  Service:    {SERVICE}")
    print(f"  Entry:      {CRED_KEY}")
    print()
    _print_next_steps()


def cmd_verify() -> None:
    key_str = _load_key()
    if not key_str:
        print()
        print("  ✗  No key found in credential store.")
        print("  Run:  python Scripts/setup_encryption.py")
        print()
        sys.exit(1)

    try:
        f = Fernet(key_str.encode())
        test = b"trading_bot_v2_verify"
        assert f.decrypt(f.encrypt(test)) == test
        print()
        print("  ✓  Fernet key found and working.")
        print()
    except Exception as e:
        print(f"\n  ✗  Key found but failed verification: {e}\n")
        sys.exit(1)


def cmd_show() -> None:
    key_str = _load_key()
    if not key_str:
        print("\n  No key found. Run:  python Scripts/setup_encryption.py\n")
        sys.exit(1)
    print()
    print("  ⚠️  Keep this private — anyone with this key can decrypt your API keys.")
    print()
    print(f"  Fernet key:  {key_str}")
    print()
    print("  To store it on another machine:")
    print("    python Scripts/setup_encryption.py --import <key>")
    print()


def cmd_import(key_str: str) -> None:
    """Store an existing key (for moving to a new machine)."""
    try:
        Fernet(key_str.encode()).encrypt(b"test")   # validate it's a real key
    except Exception:
        print("\n  ERROR: That doesn't look like a valid Fernet key.\n")
        sys.exit(1)

    existing = _load_key()
    if existing:
        confirm = input("  A key already exists. Type YES to overwrite: ").strip()
        if confirm != "YES":
            print("  Cancelled.")
            return

    _store_key(key_str)
    print("\n  ✓  Key imported and stored successfully.\n")


def _print_next_steps() -> None:
    print("  NEXT STEPS")
    print("  " + "─" * 50)
    print()
    print("  1. Encrypt each API key:")
    print()
    print("       python Scripts/encrypt_key.py")
    print()
    print("     Paste each plaintext key when prompted.")
    print("     You will get back a token starting with 'gAAAAA'.")
    print()
    print("  2. Open .env and replace plaintext values:")
    print()
    print("       KRAKEN_API_KEY=gAAAAA...your_encrypted_token...")
    print("       KRAKEN_SECRET_KEY=gAAAAA...your_encrypted_token...")
    print("       ALPACA_API_KEY=gAAAAA...your_encrypted_token...")
    print("       ALPACA_SECRET_KEY=gAAAAA...your_encrypted_token...")
    print()
    print("     Keys NOT starting with 'gAAAAA' are treated as plaintext")
    print("     and work unchanged — migrate one at a time if preferred.")
    print()
    print("  3. Start the dashboard and complete first-time setup:")
    print()
    print("       python web_dashboard.py")
    print()
    print("     Visit http://localhost:8125 — you'll be prompted to change")
    print("     the default admin password and create your personal account.")
    print()
    print("  If you need to move to a new machine later:")
    print("    python Scripts/setup_encryption.py --show     # get the key")
    print("    python Scripts/setup_encryption.py --import <key>  # store on new machine")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trading Bot encryption setup")
    parser.add_argument("--rotate",       action="store_true", help="Generate a new key (invalidates all encrypted values)")
    parser.add_argument("--verify",       action="store_true", help="Check the stored key works")
    parser.add_argument("--show",         action="store_true", help="Print the stored key (keep private)")
    parser.add_argument("--import",       dest="import_key", metavar="KEY", help="Import an existing key from another machine")
    args = parser.parse_args()

    if args.verify:
        cmd_verify()
    elif args.show:
        cmd_show()
    elif args.import_key:
        cmd_import(args.import_key)
    else:
        cmd_setup(rotate=args.rotate)
