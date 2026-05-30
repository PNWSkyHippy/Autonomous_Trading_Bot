"""HTML dashboard bridge.

Run: python web_dashboard.py
"""
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
HTML_ROOT = ROOT / "Html-Files"
if not HTML_ROOT.exists():
    HTML_ROOT = ROOT / "HTML-Files"

_config = None
try:
    import config as _config
    _default_db_path = Path(_config.DB_PATH)
    if not _default_db_path.is_absolute():
        _default_db_path = ROOT / _default_db_path
except Exception:
    _default_db_path = ROOT / "data" / "trading_bot.db"

DB_PATH = Path(os.environ.get("WEB_DASHBOARD_DB_PATH", str(_default_db_path)))
BOT_SCRIPT = ROOT / "bot_engine.py"
BOT_PYTHON = ROOT / "venv312" / "Scripts" / "python.exe"
WEB_DASHBOARD_PORT = 8125

risk_manager = None
db = None
monitor = None
_close_lock = threading.Lock()
_close_requests = {}

try:
    from data.database import db as _db
    db = _db
except Exception:
    db = None

try:
    from core.risk_manager import risk_manager as _risk_manager
    risk_manager = _risk_manager
except Exception:
    risk_manager = None

try:
    from core.position_monitor import monitor as _monitor
    monitor = _monitor
except Exception:
    monitor = None

# ── Tradovate real-time feed (optional) ──────────────────────────────────────
# Activated when a JWT token is posted to /api/tradovate_token.
# Falls back to yfinance silently when not active.
_tradovate_feed = None
try:
    from core.tradovate_feed import TradovateFeed, init_feed as _tv_init_feed
    import logging as _logging
    _logging.getLogger("core.tradovate_feed").setLevel(_logging.WARNING)
    _tradovate_available = True
except Exception:
    _tradovate_available = False

# Map dashboard symbol → Tradovate front-month contract name
# Format: ROOT → ROOT + MONTH_CODE + YEAR_CODE  (M6 = June 2026)
# Update month code each quarterly rollover: M6→U6 (Sep), U6→Z6 (Dec), Z6→H7 (Mar)
TRADOVATE_SYMBOL_MAP = {
    # ── CME Equity Index Micros & Minis ──────────────────────────────────
    "MES":  "MESM6",   "ES":  "ESM6",
    "MNQ":  "MNQM6",   "NQ":  "NQM6",
    "M2K":  "M2KM6",   "RTY": "RTYM6",
    # ── CBOT Equity ──────────────────────────────────────────────────────
    "MYM":  "MYMM6",   "YM":  "YMM6",
    # ── NYMEX Energy ─────────────────────────────────────────────────────
    "MCL":  "MCLM6",   "CL":  "CLN6",   # CL front-month is July (N6) in late May
    "NG":   "NGN6",    "MNG": "MNGN6",
    # ── COMEX Metals ─────────────────────────────────────────────────────
    "MGC":  "MGCM6",   "GC":  "GCM6",
    "SI":   "SIU6",    "SIL": "SILQ6",  # Silver rolls to Sep (U6) by May
    # ── CME Crypto Micros ────────────────────────────────────────────────
    "MBT":  "MBTM6",   "MET": "METM6",
}

# Known Tradovate numeric contract IDs (stable per contract, not per session)
# IDs discovered via: GET /v1/contract/find?name=<NAME>  (auto-populated at runtime)
TRADOVATE_SYMBOL_IDS: dict = {
    "MESM6":  3961353,   # confirmed
    # All others are auto-looked up via REST on first chart request and cached here
}


# ── Dashboard Auth ────────────────────────────────────────────────────────────
# User store: data/dashboard_users.json
# Schema: { username: { "hash": "sha256:salt:hex", "role": "admin"|"user",
#                        "force_change": bool } }
#
# On first start the file is seeded with admin / admin (force_change=True).
# Admin is redirected to /setup to change the password and create a personal
# account before the dashboard becomes accessible.
#
# Recovery if locked out:
#   • A one-time recovery token is printed to the console every server start.
#     Visit /recover, paste the token, and reset the admin password.
#   • CLI: python Scripts/set_dashboard_password.py
#   • Nuclear: delete data/dashboard_users.json → re-seeds admin/admin on next start.

_ADMIN_DEFAULT  = "admin"
SESSION_TTL     = 86400           # 24 hours
_sessions: dict[str, tuple[float, str]] = {}   # token → (expiry, username)
_sessions_lock  = threading.Lock()
_RECOVERY_TOKEN = secrets.token_hex(24)        # regenerated each start

# Legacy JSON path — only used during one-time migration
_USERS_FILE_LEGACY = ROOT / "data" / "dashboard_users.json"


# ── Fernet helper (shared key with API key encryption) ────────────────────────

def _get_fernet():
    """Return a Fernet instance using the key from the OS credential store.
    Returns None if the key hasn’t been set up yet (new install pre-setup)."""
    try:
        import keyring as _kr
        from cryptography.fernet import Fernet as _F
        raw = _kr.get_password("trading_bot_v2", "fernet_key")
        return _F(raw.encode()) if raw else None
    except Exception:
        return None


def _enc_hash(hash_str: str) -> str:
    """Encrypt a password hash string for DB storage.
    Falls back to plain: prefix if the Fernet key isn’t set up yet."""
    f = _get_fernet()
    if f:
        return f.encrypt(hash_str.encode()).decode()
    return "plain:" + hash_str


def _dec_hash(stored: str) -> str:
    """Decrypt a stored hash_enc value back to the raw sha256 hash string."""
    if stored.startswith("plain:"):
        return stored[6:]
    f = _get_fernet()
    if not f:
        raise RuntimeError(
            "Fernet key missing — run: python Scripts/setup_encryption.py"
        )
    from cryptography.fernet import Fernet as _F
    return f.decrypt(stored.encode()).decode()


# ── User store (DB-backed) ────────────────────────────────────────────────────

def _db_path() -> str:
    return str(DB_PATH)


def _load_users() -> dict:
    """Return {username: {hash, role, force_change}} from DB."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM dashboard_users").fetchall()
        conn.close()
        result = {}
        for r in rows:
            try:
                h = _dec_hash(r["hash_enc"])
            except Exception:
                h = ""   # decrypt failed — key mismatch, treat as locked
            result[r["username"]] = {
                "hash":         h,
                "role":         r["role"],
                "force_change": bool(r["force_change"]),
            }
        return result
    except Exception:
        return {}


def _save_users(users: dict) -> None:
    """Write the users dict back to DB, encrypting hashes."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = sqlite3.connect(_db_path())
        for username, rec in users.items():
            raw_hash  = rec.get("hash", "")
            enc       = _enc_hash(raw_hash) if raw_hash else ""
            role      = rec.get("role", "user")
            force     = 1 if rec.get("force_change") else 0
            conn.execute("""
                INSERT INTO dashboard_users (username, hash_enc, role, force_change, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    hash_enc     = excluded.hash_enc,
                    role         = excluded.role,
                    force_change = excluded.force_change,
                    updated_at   = excluded.updated_at
            """, (username, enc, role, force, now, now))
        conn.commit()
        conn.close()
    except Exception as e:
        import logging as _lg
        _lg.getLogger("dashboard.auth").error(f"[AUTH] _save_users error: {e}")


def _delete_user_db(username: str) -> None:
    try:
        conn = sqlite3.connect(_db_path())
        conn.execute("DELETE FROM dashboard_users WHERE username=?", (username,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _ensure_admin_seeded() -> None:
    """Seed admin/admin (force_change=True) if no users exist in DB yet.
    Also migrates the legacy JSON file if present."""
    try:
        # One-time migration from dashboard_users.json → DB
        if _USERS_FILE_LEGACY.exists():
            try:
                old = json.loads(_USERS_FILE_LEGACY.read_text(encoding="utf-8"))
                users_to_migrate = {}
                for u, v in old.items():
                    if isinstance(v, str):
                        users_to_migrate[u] = {"hash": v, "role": "admin" if u == "admin" else "user", "force_change": False}
                    else:
                        users_to_migrate[u] = v
                if users_to_migrate:
                    _save_users(users_to_migrate)
                    import logging as _lg
                    _lg.getLogger("dashboard.auth").info(
                        f"[AUTH] Migrated {len(users_to_migrate)} user(s) from JSON to DB")
                _USERS_FILE_LEGACY.rename(_USERS_FILE_LEGACY.with_suffix(".json.migrated"))
            except Exception as me:
                import logging as _lg
                _lg.getLogger("dashboard.auth").warning(f"[AUTH] JSON migration failed: {me}")

        conn = sqlite3.connect(_db_path())
        count = conn.execute("SELECT COUNT(*) FROM dashboard_users").fetchone()[0]
        conn.close()
        if count == 0:
            _save_users({_ADMIN_DEFAULT: {
                "hash":         _hash_password(_ADMIN_DEFAULT),
                "role":         "admin",
                "force_change": True,
            }})
    except Exception as e:
        import logging as _lg
        _lg.getLogger("dashboard.auth").error(f"[AUTH] _ensure_admin_seeded error: {e}")


def _needs_setup() -> bool:
    """True when the only account is admin with force_change=True (fresh install)."""
    users = _load_users()
    if len(users) != 1:
        return False
    rec = users.get(_ADMIN_DEFAULT, {})
    return bool(rec.get("force_change"))


def _hash_password(password: str) -> str:
    salt   = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"sha256:{salt}:{digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, expected = stored.split(":", 2)
        actual = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def _auth_check(username: str, password: str) -> bool:
    users = _load_users()
    rec   = users.get(username)
    return bool(rec and _verify_password(password, rec.get("hash", "")))


def _is_admin(username: str) -> bool:
    users = _load_users()
    return users.get(username, {}).get("role") == "admin"


def _user_force_change(username: str) -> bool:
    users = _load_users()
    return bool(users.get(username, {}).get("force_change"))


# ── Session management ────────────────────────────────────────────────────────

def _new_session(username: str) -> str:
    token = secrets.token_hex(32)
    with _sessions_lock:
        _sessions[token] = (time.time() + SESSION_TTL, username)
    return token


def _valid_session(token: str) -> bool:
    if not token:
        return False
    with _sessions_lock:
        rec = _sessions.get(token)
        if not rec:
            return False
        if time.time() > rec[0]:
            del _sessions[token]
            return False
        return True


def _session_user(token: str) -> str:
    with _sessions_lock:
        rec = _sessions.get(token)
        return rec[1] if rec else ""


def _revoke_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)


# ── Auth HTML pages (all inline — zero external dependencies) ─────────────────

def _auth_page(title: str, heading: str, sub: str, form_html: str,
               error_msg: str = "Something went wrong.", extra_js: str = "") -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Bot &mdash; {title}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0f172a;display:flex;align-items:center;justify-content:center;
       min-height:100vh;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
  .card{{background:#1e293b;border:1px solid #334155;border-radius:12px;
        padding:40px 36px;width:400px;box-shadow:0 20px 60px rgba(0,0,0,.5)}}
  h1{{color:#f1f5f9;font-size:1.3rem;font-weight:600;margin-bottom:6px}}
  p.sub{{color:#64748b;font-size:.85rem;margin-bottom:28px}}
  label{{display:block;color:#94a3b8;font-size:.78rem;font-weight:500;
        margin-bottom:5px;letter-spacing:.04em;text-transform:uppercase}}
  input{{width:100%;background:#0f172a;border:1px solid #334155;border-radius:6px;
        color:#f1f5f9;font-size:.95rem;padding:10px 12px;outline:none;transition:border-color .15s}}
  input:focus{{border-color:#6366f1}}
  .field{{margin-bottom:16px}}
  button{{width:100%;background:#6366f1;border:none;border-radius:6px;color:#fff;
         cursor:pointer;font-size:.95rem;font-weight:600;padding:11px;margin-top:4px;
         transition:background .15s}}
  button:hover{{background:#4f46e5}}
  .err{{background:#450a0a;border:1px solid #7f1d1d;border-radius:6px;
       color:#fca5a5;font-size:.83rem;padding:10px 12px;margin-bottom:16px;display:none}}
  .err.show{{display:block}}
  .notice{{background:#1c3048;border:1px solid #1e40af;border-radius:6px;
           color:#93c5fd;font-size:.82rem;padding:10px 12px;margin-bottom:16px;line-height:1.5}}
  hr{{border:none;border-top:1px solid #334155;margin:20px 0}}
  .sec{{color:#94a3b8;font-size:.78rem;font-weight:600;text-transform:uppercase;
        letter-spacing:.06em;margin-bottom:14px}}
</style>
</head>
<body><div class="card">
  <h1>{heading}</h1><p class="sub">{sub}</p>
  <div class="err" id="err">{error_msg}</div>
  {form_html}
</div><script>{extra_js}</script></body></html>"""
    return html.encode("utf-8")


# Login page
_LOGIN_PAGE = lambda: _auth_page("Login", "Trading Bot Dashboard", "Sign in to continue", """
<form id="form">
  <div class="field"><label>Username</label>
    <input type="text" id="u" autocomplete="username" autofocus></div>
  <div class="field"><label>Password</label>
    <input type="password" id="p" autocomplete="current-password"></div>
  <button type="submit">Sign in</button>
</form>""", error_msg="Invalid username or password.", extra_js="""
document.getElementById("form").addEventListener("submit",async e=>{
  e.preventDefault();
  const r=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({username:document.getElementById("u").value,
                         password:document.getElementById("p").value})});
  if(r.ok){location.href=new URLSearchParams(location.search).get("next")||"/";}
  else{document.getElementById("err").classList.add("show");}
});""")

# First-run setup: change admin password + create personal account
_SETUP_PAGE = lambda: _auth_page("Setup", "First-Time Setup",
  "The default admin/admin credentials are active. Change the admin password and create your personal account to continue.", """
<div class="notice">&#x26A0;&#xFE0F; <strong>Default credentials are in use.</strong><br>
Complete this form before the dashboard becomes accessible.</div>
<p class="sec">&#x1F512; Change Admin Password</p>
<form id="form">
  <div class="field"><label>New Admin Password (min 8 chars)</label>
    <input type="password" id="ap" autocomplete="new-password" autofocus></div>
  <div class="field"><label>Confirm Admin Password</label>
    <input type="password" id="ap2" autocomplete="new-password"></div>
  <hr>
  <p class="sec">&#x1F464; Create Your Personal Account</p>
  <div class="field"><label>Your Username</label>
    <input type="text" id="u" autocomplete="username"></div>
  <div class="field"><label>Your Password (min 8 chars)</label>
    <input type="password" id="p" autocomplete="new-password"></div>
  <div class="field"><label>Confirm Your Password</label>
    <input type="password" id="p2" autocomplete="new-password"></div>
  <button type="submit">Save &amp; Enter Dashboard</button>
</form>""", extra_js="""
document.getElementById("form").addEventListener("submit",async e=>{
  e.preventDefault();
  const ap=document.getElementById("ap").value,ap2=document.getElementById("ap2").value,
        u=document.getElementById("u").value.trim(),
        p=document.getElementById("p").value,p2=document.getElementById("p2").value;
  const err=document.getElementById("err");err.classList.remove("show");
  if(ap.length<8){err.textContent="Admin password must be at least 8 characters.";err.classList.add("show");return;}
  if(ap!==ap2){err.textContent="Admin passwords do not match.";err.classList.add("show");return;}
  if(!u){err.textContent="Your username cannot be empty.";err.classList.add("show");return;}
  if(u.toLowerCase()==="admin"){err.textContent="Choose a username other than ‘admin’.";err.classList.add("show");return;}
  if(p.length<8){err.textContent="Your password must be at least 8 characters.";err.classList.add("show");return;}
  if(p!==p2){err.textContent="Your passwords do not match.";err.classList.add("show");return;}
  const r=await fetch("/api/auth/setup",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({admin_password:ap,username:u,password:p})});
  const d=await r.json();
  if(r.ok){location.href="/";}
  else{err.textContent=d.message||"Setup failed.";err.classList.add("show");}
});""")

# Recovery page (console token required)
_RECOVER_PAGE = lambda: _auth_page("Recovery", "Account Recovery",
  "Paste the recovery token from your server console to reset the admin password.", """
<div class="notice">&#x1F511; Find the recovery token in the server console output<br>
(printed each time the dashboard starts).</div>
<form id="form">
  <div class="field"><label>Recovery Token</label>
    <input type="text" id="tok" autocomplete="off" spellcheck="false"></div>
  <div class="field"><label>New Admin Password (min 8 chars)</label>
    <input type="password" id="p" autocomplete="new-password"></div>
  <div class="field"><label>Confirm Password</label>
    <input type="password" id="p2" autocomplete="new-password"></div>
  <button type="submit">Reset Admin Password</button>
</form>""", extra_js="""
document.getElementById("form").addEventListener("submit",async e=>{
  e.preventDefault();
  const p=document.getElementById("p").value,p2=document.getElementById("p2").value;
  const err=document.getElementById("err");err.classList.remove("show");
  if(p.length<8){err.textContent="Password must be at least 8 characters.";err.classList.add("show");return;}
  if(p!==p2){err.textContent="Passwords do not match.";err.classList.add("show");return;}
  const r=await fetch("/api/auth/recover",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({token:document.getElementById("tok").value.trim(),password:p})});
  const d=await r.json();
  if(r.ok){location.href="/login";}
  else{err.textContent=d.message||"Recovery failed.";err.classList.add("show");}
});""")


def _api_keys_page() -> bytes:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Bot — API Keys</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0f172a;color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:32px 24px}
  h1{font-size:1.25rem;font-weight:600;margin-bottom:4px}
  p.sub{color:#64748b;font-size:.85rem;margin-bottom:24px}
  .card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:24px;margin-bottom:24px}
  h2{font-size:.85rem;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em;margin-bottom:16px}
  table{width:100%;border-collapse:collapse;font-size:.88rem}
  th{text-align:left;color:#64748b;font-weight:500;padding:6px 10px;border-bottom:1px solid #334155;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
  td{padding:9px 10px;border-bottom:1px solid #1a2740;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:#1a2740}
  .svc{color:#818cf8;font-weight:600}
  .kn{color:#94a3b8}
  .val{font-family:ui-monospace,monospace;font-size:.82rem;color:#34d399;word-break:break-all;max-width:340px}
  .val.hidden{color:#334155;letter-spacing:.12em;user-select:none}
  .ts{color:#475569;font-size:.75rem;white-space:nowrap}
  .btn{border:none;border-radius:5px;cursor:pointer;font-size:.78rem;font-weight:600;padding:5px 11px;transition:background .15s;white-space:nowrap}
  .eye{background:#172033;color:#93c5fd}.eye:hover{background:#1e3a5f}
  .edit{background:#172a1e;color:#6ee7b7}.edit:hover{background:#065f46}
  .del{background:#2d0a0a;color:#fca5a5}.del:hover{background:#7f1d1d}
  .add-btn{background:#6366f1;color:#fff;padding:9px 18px;font-size:.88rem;border-radius:7px;border:none;cursor:pointer;font-weight:600;margin-top:16px;transition:background .15s}
  .add-btn:hover{background:#4f46e5}
  .save-btn{background:#059669;color:#fff}.save-btn:hover{background:#047857}
  .cancel-btn{background:#334155;color:#94a3b8}.cancel-btn:hover{background:#475569}
  .actions{display:flex;gap:6px}
  .add-form{display:none;margin-top:20px;padding-top:20px;border-top:1px solid #334155}
  .add-form.open{display:block}
  .frow{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
  .frow input{flex:1;min-width:150px;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#f1f5f9;font-size:.88rem;padding:8px 11px;outline:none}
  .frow input:focus{border-color:#6366f1}
  .notice{background:#1c2f4a;border:1px solid #1e40af;border-radius:6px;color:#93c5fd;font-size:.82rem;padding:10px 14px;margin-bottom:20px;line-height:1.6}
  .back{color:#6366f1;font-size:.85rem;text-decoration:none;display:inline-flex;align-items:center;gap:4px;margin-bottom:22px}
  .back:hover{color:#818cf8}
  .empty{color:#475569;font-size:.85rem;padding:8px 0}
  .toast{position:fixed;bottom:24px;right:24px;background:#059669;color:#fff;padding:10px 18px;border-radius:8px;font-size:.88rem;font-weight:500;opacity:0;transition:opacity .2s;pointer-events:none}
  .toast.show{opacity:1}
  .toast.err{background:#7f1d1d}
</style>
</head>
<body>
<a class="back" href="/">← Back to Dashboard</a>
<h1>\U0001f511 API Key Manager</h1>
<p class="sub">Keys stored encrypted in the database — admin eyes only.</p>
<div class="notice">
  Values are decrypted for display here. Changes take effect on the next bot restart
  (config.py reloads at import time).<br>
  <strong>Fallback chain:</strong> DB (encrypted) → .env → empty string.
</div>
<div class="card">
  <h2>Stored Keys</h2>
  <div id="tbl"></div>
  <button class="add-btn" onclick="openAdd()">+ Add / Update Key</button>
  <div class="add-form" id="add-form">
    <div class="frow">
      <input type="text"     id="f-svc" placeholder="Service  (e.g. kraken)" autocomplete="off">
      <input type="text"     id="f-key" placeholder="Key name (e.g. api_key)" autocomplete="off">
    </div>
    <div class="frow">
      <input type="password" id="f-val" placeholder="Value — will be Fernet-encrypted on save" autocomplete="new-password">
    </div>
    <div class="actions">
      <button class="btn save-btn"   onclick="saveKey()">Save Encrypted</button>
      <button class="btn cancel-btn" onclick="closeAdd()">Cancel</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let _keys=[],_shown={};
async function load(){
  const r=await fetch('/api/admin/api_keys');
  if(!r.ok){document.getElementById('tbl').innerHTML='<p class="empty">Error loading keys.</p>';return;}
  const d=await r.json();_keys=d.keys||[];render();
}
function render(){
  const w=document.getElementById('tbl');
  if(!_keys.length){w.innerHTML='<p class="empty">No keys in DB yet.</p>';return;}
  const rows=_keys.map((k,i)=>{
    const shown=_shown[i];
    const val=shown?`<span class="val">${esc(k.value)}</span>`:`<span class="val hidden">••••••••••••</span>`;
    return `<tr><td><span class="svc">${esc(k.service)}</span></td><td><span class="kn">${esc(k.key_name)}</span></td><td>${val}</td><td><span class="ts">${esc(k.updated_at)}</span></td><td><div class="actions"><button class="btn eye" onclick="toggleVal(${i})">${shown?'Hide':'Show'}</button><button class="btn edit" onclick="prefill('${esc(k.service)}','${esc(k.key_name)}')">Edit</button><button class="btn del" onclick="delKey('${esc(k.service)}','${esc(k.key_name)}')">Delete</button></div></td></tr>`;
  }).join('');
  w.innerHTML=`<table><thead><tr><th>Service</th><th>Key Name</th><th>Value</th><th>Updated</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function toggleVal(i){_shown[i]=!_shown[i];render();}
function toast(msg,err=false){const t=document.getElementById('toast');t.textContent=msg;t.className='toast'+(err?' err':'');requestAnimationFrame(()=>{t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2800);});}
function openAdd(){document.getElementById('add-form').classList.add('open');document.getElementById('f-svc').focus();}
function closeAdd(){document.getElementById('add-form').classList.remove('open');}
function prefill(svc,kn){document.getElementById('f-svc').value=svc;document.getElementById('f-key').value=kn;document.getElementById('f-val').value='';openAdd();document.getElementById('f-val').focus();}
async function saveKey(){
  const svc=document.getElementById('f-svc').value.trim().toLowerCase();
  const kn=document.getElementById('f-key').value.trim().toLowerCase();
  const val=document.getElementById('f-val').value;
  if(!svc||!kn||!val){toast('All three fields required.',true);return;}
  const r=await fetch('/api/admin/api_keys/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service:svc,key_name:kn,value:val})});
  const d=await r.json();
  if(r.ok){closeAdd();document.getElementById('f-svc').value='';document.getElementById('f-key').value='';document.getElementById('f-val').value='';_shown={};toast('✓ '+svc+'/'+kn+' saved');load();}
  else toast(d.message||'Save failed.',true);
}
async function delKey(svc,kn){
  if(!confirm('Delete '+svc+'/'+kn+'?\\n\\nBot will fall back to .env on next restart.')) return;
  const r=await fetch('/api/admin/api_keys/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service:svc,key_name:kn})});
  const d=await r.json();
  if(r.ok){_shown={};toast('✓ '+svc+'/'+kn+' deleted');load();}
  else toast(d.message||'Delete failed.',true);
}
load();
</script>
</body>
</html>"""
    return html.encode("utf-8")


def is_bot_running():
    """Best-effort check for bot process."""
    try:
        if sys.platform == "win32":
            pids = find_bot_pids_windows()
            return len(pids) > 0
        out = subprocess.check_output(["ps", "-ef"], text=True)
        return "bot_engine.py" in out
    except Exception:
        return False


def find_bot_pids_windows():
    """Find PIDs for processes whose command line contains bot_engine.py."""
    try:
        import psutil

        bot_name = BOT_SCRIPT.name.lower()
        current_pid = os.getpid()
        pids = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                info = proc.info
                pid = int(info.get("pid") or 0)
                if pid == current_pid:
                    continue
                name = (info.get("name") or "").lower()
                if name not in ("python.exe", "pythonw.exe", "python"):
                    continue
                cmdline = [str(part) for part in (info.get("cmdline") or [])]
                if any(Path(part).name.lower() == bot_name for part in cmdline):
                    pids.append(pid)
            except Exception:
                continue
        if pids:
            return pids
    except Exception:
        pass

    try:
        bot_path = str(BOT_SCRIPT.resolve()).replace("'", "''").lower()
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "$target = '"
                + bot_path
                + "'; "
                + "$current = "
                + str(os.getpid())
                + "; "
                + "Get-CimInstance Win32_Process | "
                + "Where-Object { "
                + "$_.ProcessId -ne $current -and "
                + "$_.Name -match '^(python|pythonw)\\.exe$' -and "
                + "$_.CommandLine -and "
                + "$_.CommandLine.ToLower().Contains($target) "
                + "} | "
                + "Select-Object -ExpandProperty ProcessId"
            ),
        ]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=5)
        return [int(x.strip()) for x in out.splitlines() if x.strip().isdigit()]
    except Exception:
        return []


def start_bot_process():
    """Start bot_engine.py if not already running."""
    if is_bot_running():
        return False, "Bot is already running."
    try:
        python_exe = str(BOT_PYTHON if BOT_PYTHON.exists() else Path(sys.executable))
        if sys.platform == "win32":
            # Launch through cmd /k so the bot has a visible console with live output.
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NEW_CONSOLE = 0x00000010
            flags = CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP
            launcher = ROOT / "StartBotEngine.bat"
            ps_launcher = ROOT / "StartBotEngine.ps1"
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoExit",
                    "-File",
                    str(ps_launcher if ps_launcher.exists() else launcher),
                ],
                cwd=str(ROOT),
                creationflags=flags,
            )
        else:
            subprocess.Popen(
                [python_exe, str(BOT_SCRIPT)],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        return True, "Bot start requested."
    except Exception as exc:
        return False, f"Failed to start bot: {exc}"


def open_project_terminal():
    """Open an interactive PowerShell rooted in the trading bot workspace."""
    try:
        if sys.platform == "win32":
            CREATE_NEW_CONSOLE = 0x00000010
            root = str(ROOT.resolve()).replace("'", "''")
            command = (
                f"Set-Location -LiteralPath '{root}'; "
                "$Host.UI.RawUI.WindowTitle = 'Trading Bot Workspace Shell'; "
                "Write-Host 'Trading bot workspace shell ready.' -ForegroundColor Cyan; "
                "Write-Host 'Root: ' -NoNewline -ForegroundColor DarkGray; "
                "Write-Host (Get-Location).Path; "
                "Write-Host 'Try: Get-Content .\\logs\\bot.log -Tail 80 -Wait' -ForegroundColor DarkGray"
            )
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-NoExit",
                    "-Command",
                    command,
                ],
                cwd=str(ROOT),
                creationflags=CREATE_NEW_CONSOLE,
            )
            return True, "PowerShell opened in the trading bot workspace."

        shell = os.environ.get("SHELL", "/bin/sh")
        subprocess.Popen([shell], cwd=str(ROOT))
        return True, "Shell opened in the trading bot workspace."
    except Exception as exc:
        return False, f"Failed to open PowerShell: {exc}"


def stop_bot_process():
    """Stop bot_engine.py process(es)."""
    try:
        if sys.platform == "win32":
            pids = find_bot_pids_windows()
            if not pids:
                return False, "No bot_engine.py process found."
            killed = 0
            for pid in pids:
                result = subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    killed += 1
            if killed > 0:
                return True, f"Bot stop requested for {killed} process(es)."
            return False, "Matched bot process IDs but taskkill failed."
        else:
            result = subprocess.run(["pkill", "-f", "bot_engine.py"], check=False)
            if result.returncode == 0:
                return True, "Bot stop requested."
            return False, "No bot_engine.py process found."
    except Exception as exc:
        return False, f"Failed to stop bot: {exc}"


def sort_open_positions(rows, sort_by=None, sort_dir="desc"):
    key_map = {
        "opened": "entry_time",
        "symbol": "symbol",
        "type": "asset_class",
        "dir": "direction",
        "entry": "entry_price",
        "current": "current_price",
        "sl": "stop_loss",
        "tp": "take_profit",
        "pnlD": "unrealized_pnl",
        "pnlP": "pnl_pct",
        "toSL": "distance_to_sl",
        "toTP": "distance_to_tp",
    }
    field = key_map.get(sort_by or "opened", "entry_time")
    reverse = str(sort_dir or "desc").lower() != "asc"

    def value(row):
        raw = row.get(field)
        if field in ("asset_class", "direction", "entry_time", "symbol"):
            return str(raw or "").lower()
        try:
            return float(raw or 0)
        except Exception:
            return 0.0

    return sorted(rows, key=value, reverse=reverse)


def get_open_positions(sort_by=None, sort_dir="desc", enrich=True):
    """Return open positions using DB adapter when available, else SQLite fallback."""
    if enrich and monitor is not None:
        try:
            rows = monitor.get_positions_summary() or []
            positions = [
                {
                    "id": t.get("id"),
                    "trade_id": t.get("trade_id"),
                    "symbol": t.get("symbol") or t.get("ticker") or "?",
                    "asset_class": t.get("asset_class") or "crypto",
                    "side": t.get("direction") or t.get("side") or "long",
                    "direction": t.get("direction") or t.get("side") or "long",
                    "qty": float(t.get("quantity") or t.get("shares") or 0),
                    "entry_price": float(t.get("entry_price") or 0),
                    "current_price": float(t.get("current_price") or t.get("entry_price") or 0),
                    "stop_loss": float(t.get("stop_loss") or t.get("entry_price") or 0),
                    "take_profit": float(t.get("take_profit") or t.get("entry_price") or 0),
                    "unrealized_pnl": float(t.get("unrealized_pnl") or 0),
                    "pnl_pct": float(t.get("pnl_pct") or 0),
                    "distance_to_sl": float(t.get("distance_to_sl") or 0),
                    "distance_to_tp": float(t.get("distance_to_tp") or 0),
                    "entry_time": t.get("entry_time") or "",
                    "age_hours": float(t.get("age_hours") or 0),
                    "is_stuck": bool(t.get("is_stuck")),
                    "close_attempts": int(t.get("close_attempts") or 0),
                    "tp_hit_count": int(t.get("tp_hit_count") or 0),
                }
                for t in rows
            ]
            return sort_open_positions(positions, sort_by, sort_dir)
        except Exception:
            pass

    if db is not None:
        try:
            rows = db.get_open_trades() or []
            positions = []
            for t in rows:
                entry_price = float(t.get("entry_price") or 0)
                if enrich:
                    raw_price = get_current_price(
                        t.get("symbol") or t.get("ticker") or "?",
                        t.get("asset_class") or "crypto",
                    )
                    # Price sanity gate: reject prices that deviate >80% from entry.
                    # Bad price feeds (e.g. Kraken returning a stale $0.05 for a
                    # $55 token) produce phantom -$1900 P&L that flips wildly.
                    # Mirror the same gate used in position_monitor._process_position.
                    if raw_price and entry_price > 0:
                        _dev = abs(raw_price - entry_price) / entry_price
                        current_price = raw_price if _dev <= 0.80 else entry_price
                    else:
                        current_price = raw_price or entry_price
                else:
                    current_price = float(t.get("current_price") or entry_price or 0)
                qty = float(t.get("quantity") or t.get("shares") or 0)
                direction = (t.get("direction") or t.get("side") or "long").lower()
                pnl = (current_price - entry_price) * qty
                if direction == "short":
                    pnl = -pnl
                pnl_pct = 0.0
                if entry_price:
                    raw_pct = ((current_price - entry_price) / entry_price) * 100
                    pnl_pct = raw_pct if direction == "long" else -raw_pct
                stop_loss = float(t.get("stop_loss") or entry_price or 0)
                take_profit = float(t.get("take_profit") or entry_price or 0)
                distance_to_sl = abs((current_price - stop_loss) / current_price * 100) if current_price else 0
                distance_to_tp = abs((take_profit - current_price) / current_price * 100) if current_price else 0
                positions.append({
                    "id": t.get("id"),
                    "trade_id": t.get("trade_id"),
                    "symbol": t.get("symbol") or t.get("ticker") or "?",
                    "asset_class": t.get("asset_class") or "crypto",
                    "side": direction,
                    "direction": direction,
                    "qty": qty,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "unrealized_pnl": round(pnl, 4),
                    "pnl_pct": round(pnl_pct, 4),
                    "distance_to_sl": distance_to_sl,
                    "distance_to_tp": distance_to_tp,
                    "entry_time": t.get("entry_time") or "",
                })
            return sort_open_positions(positions, sort_by, sort_dir)
        except Exception:
            pass

    try:
        conn = sqlite3.connect(DB_PATH)
 #       conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, symbol, side, COALESCE(shares, quantity, 0), entry_price, entry_time
            FROM trades
            WHERE status='open'
            ORDER BY id DESC
            """
        )
        rows = cur.fetchall()
        conn.close()
        positions = [
            {
                "id": r[0],
                "symbol": r[1],
                "side": r[2] or "",
                "qty": float(r[3] or 0),
                "entry_price": float(r[4] or 0),
                "entry_time": r[5] or "",
            }
            for r in rows
        ]
        return sort_open_positions(positions, sort_by, sort_dir)
    except Exception:
        return []


def close_position(position_id):
    """Close a position through the same executor path used by the bot."""
    try:
        trade_record = None
        if db is not None:
            for t in db.get_open_trades() or []:
                if str(t.get("id")) == str(position_id) or str(t.get("trade_id")) == str(position_id):
                    trade_record = t
                    break

        if trade_record is None:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM trades WHERE status='open' AND (id=? OR trade_id=?)",
                (position_id, str(position_id)),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                trade_record = dict(row)

        if not trade_record:
            return False, f"Position id {position_id} not found or already closed."

        symbol = trade_record["symbol"]
        asset_class = trade_record.get("asset_class", "crypto")
        current_price = get_current_price(symbol, asset_class) or trade_record.get("entry_price")
        if not current_price:
            return False, f"Could not get current price for {symbol}."

        from core.trade_executor import executor

        entry_price = float(trade_record.get("entry_price") or 0)
        qty = float(trade_record.get("quantity") or 0)
        direction = str(trade_record.get("direction") or "long").lower()
        strategy_name = str(trade_record.get("strategy_name") or "original").lower()
        current_price = float(current_price)
        if direction == "short":
            estimated_pnl = (entry_price - current_price) * qty
        else:
            estimated_pnl = (current_price - entry_price) * qty
        exit_reason = (
            "operator_profit_harvest"
            if estimated_pnl > 0 and strategy_name not in ("original", "manual")
            else "manual_close"
        )

        success = executor.close_trade(trade_record, current_price, exit_reason)
        if not success:
            return False, f"Executor returned failure for {symbol}."

        # ── Write [TRADE CLOSED] to bot.log (web_dashboard is a separate process;
        # executor's own logger writes to the dashboard context, not bot.log).
        try:
            import config as _cfg
            _bot_log = Path(_cfg.LOG_FILE) if hasattr(_cfg, "LOG_FILE") else ROOT / "logs" / "bot.log"
            _result  = "WIN" if estimated_pnl > 0 else "LOSS"
            _broker  = trade_record.get("broker", "kraken")
            _pnl_pct = ((current_price - entry_price) / entry_price * 100
                        if direction == "long" else
                        (entry_price - current_price) / entry_price * 100)
            _line = (
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] "
                f"[TRADE CLOSED] [{_result}] {symbol} | {_broker} | {exit_reason} | "
                f"PnL: {'+' if estimated_pnl > 0 else ''}{estimated_pnl:.4f} ({_pnl_pct:+.2f}%)\n"
            )
            with open(_bot_log, "a", encoding="utf-8") as _f:
                _f.write(_line)
        except Exception:
            pass  # never block a successful close over a logging failure

        return True, f"Closed {symbol} @ ${float(current_price):.4f}."
    except Exception as exc:
        return False, f"Close failed: {exc}"


def request_close_position(position_id):
    """Start a manual close in the background so the dashboard stays usable."""
    key = str(position_id)
    with _close_lock:
        if key in _close_requests:
            return True, f"Close already in progress for position {position_id}."
        _close_requests[key] = datetime.now()

    def worker():
        try:
            ok, msg = close_position(position_id)
            status = "OK" if ok else "FAILED"
            print(f"[manual close {status}] position={position_id}: {msg}", flush=True)
        except Exception as exc:
            print(f"[manual close ERROR] position={position_id}: {exc}", flush=True)
        finally:
            with _close_lock:
                _close_requests.pop(key, None)

    thread = threading.Timer(0.25, worker)
    thread.name = f"manual-close-{key}"
    thread.daemon = True
    thread.start()
    return True, f"Close requested for position {position_id}; broker close is running in the background."


def get_current_price(symbol, asset_class):
    try:
        from scanners.market_scanner import scanner
        return scanner.get_current_price(symbol, asset_class)
    except Exception:
        try:
            from scanners.market_scanner import MarketScanner
            market_scanner = MarketScanner()
            return market_scanner.get_current_price(symbol, asset_class)
        except Exception:
            return None


def get_manual_trade_config():
    brokers = ["alpaca", "coinbase", "kraken", "paper"]
    if getattr(_config, "IBKR_ENABLED", False):
        brokers.insert(0, "ibkr")
    return {
        "brokers": brokers,
        "default_broker": brokers[0],
        "asset_classes": ["stock", "crypto"],
        "directions": ["long", "short"],
        "default_size": 2000.0,
        "default_stop_loss_pct": 1.5,
        "default_take_profit_pct": 3.0,
        "default_strategy": "manual",
    }


# Cancel flag — set by POST /api/backtest/cancel, checked in the inner run loop.
# Cleared at the start of every new run so stale signals don't bleed through.
_bt_cancel = threading.Event()
_bt_running = threading.Event()

BACKTEST_STRATEGIES = [
    # Original 11
    "adaptive_regime", "rsi_momentum", "bollinger_breakout", "ema_crossover",
    "mean_reversion", "scalp_master", "swing_trader", "grid_bot",
    "dca_accumulator", "vwap_momentum", "hammer_reversal", "orb_breakout",
    # Strategies 12-21
    "ecb_strategy", "vdmr_strategy", "rsi_dip_spike_v4",
    "vwap_confirmed_orb", "bollinger_squeeze",
    "mr_02_vef", "mr_03_fbs", "mr_04_fvg",
    # Strategies 22-27 (incubate / new)
    "btc_v6_chandelier", "rsi_dip_simple", "pll_cycle",
    "kds_mean_reversion", "ema_ribbon_breakout", "rcr_mean_reversion",
]


def _parse_backtest_days(raw):
    text = str(raw or "90").strip().lower()
    if text in ("1y", "1yr", "1 year"):
        return 365
    if text in ("2y", "2yr", "2 years"):
        return 730
    digits = "".join(ch if ch.isdigit() else " " for ch in text).split()
    return max(1, int(digits[0])) if digits else 90


def _load_watchlist_file(path):
    try:
        p = ROOT / path
        if not p.exists():
            return []
        return [
            line.strip() for line in p.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except Exception:
        return []


def _unique_symbols(symbols, crypto=False):
    seen = set()
    out = []
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol.startswith("#"):
            continue
        if crypto and "/" not in symbol:
            symbol = f"{symbol}/USD"
        if not crypto and ("/" in symbol or "-" in symbol):
            continue
        if symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def get_strategy_optimizer_params(strategy_name: str) -> list:
    """
    Introspect a strategy's params dataclass and return a list of optimizer-ready
    param descriptors: {name, default, min, max, step, is_int}.

    Skips string/bool fields (not optimizable numerically).
    Generates sensible default ranges from the dataclass default values.
    """
    STRATEGY_MAP = {
        "rsi_momentum":       ("strategies.rsi_momentum",       "RSIMomentum"),
        "bollinger_breakout": ("strategies.bollinger_breakout",  "BollingerBreakout"),
        "bollinger_squeeze":  ("strategies.bollinger_squeeze",   "BollingerSqueeze"),
        "ema_crossover":      ("strategies.ema_crossover",       "EMACrossover"),
        "mean_reversion":     ("strategies.mean_reversion",      "MeanReversion"),
        "scalp_master":       ("strategies.scalp_master",        "ScalpMaster"),
        "swing_trader":       ("strategies.swing_trader",        "SwingTrader"),
        "grid_bot":           ("strategies.grid_bot",            "GridBot"),
        "dca_accumulator":    ("strategies.dca_accumulator",     "DCAAccumulator"),
        "vwap_momentum":      ("strategies.vwap_momentum",       "VWAPMomentum"),
        "vwap_confirmed_orb": ("strategies.vwap_confirmed_orb",  "VwapConfirmedOrb"),
        "hammer_reversal":    ("strategies.hammer_reversal",     "HammerReversal"),
        "orb_breakout":       ("strategies.orb_breakout",        "ORBBreakout"),
        "adaptive_regime":    ("strategies.adaptive_regime",     "AdaptiveRegime"),
        "ecb_strategy":       ("strategies.ecb_strategy",        "ECBStrategy"),
        "vdmr_strategy":      ("strategies.vdmr_strategy",       "VDMRStrategy"),
        "rsi_dip_spike_v4":   ("strategies.rsi_dip_spike_v4",   "RSIDipSpikeV4Strategy"),
        "mr_02_vef":          ("strategies.mr_02_vef_strategy",  "MR02VEFStrategy"),
        "mr_03_fbs":          ("strategies.mr_03_fbs_strategy",  "MR03FBSStrategy"),
        "mr_04_fvg":          ("strategies.mr_04_fvg_strategy",  "MR04FVGStrategy"),
    }
    if strategy_name not in STRATEGY_MAP:
        return []
    try:
        import importlib, dataclasses, math
        module_path, class_name = STRATEGY_MAP[strategy_name]
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        obj = cls()
        params = getattr(obj, 'params', None)
        if params is None or not dataclasses.is_dataclass(params):
            return []

        result = []
        for f in dataclasses.fields(params):
            val = getattr(params, f.name)
            if isinstance(val, bool) or isinstance(val, str):
                continue  # not numerically optimizable
            is_int = isinstance(val, int)
            is_float = isinstance(val, float)
            if not (is_int or is_float):
                continue

            if is_int:
                # For period/length-type params, range around default
                if val <= 0:
                    continue
                step  = max(1, val // 10)
                lo    = max(1, val // 2)
                hi    = val * 3
            else:
                # Float — pick step as ~10% of value, round nicely
                if val <= 0:
                    lo, hi, step = 0.5, 5.0, 0.5
                else:
                    magnitude = 10 ** math.floor(math.log10(val))
                    step = round(magnitude * 0.5, 4)
                    lo   = round(max(0.1, val * 0.4), 4)
                    hi   = round(val * 2.5, 4)

            result.append({
                "name":    f.name,
                "default": val,
                "min":     lo,
                "max":     hi,
                "step":    step,
                "is_int":  is_int,
            })
        return result
    except Exception as e:
        logger.warning(f"get_strategy_optimizer_params({strategy_name}): {e}")
        return []


def get_backtest_config():
    try:
        import config as cfg
    except Exception:
        cfg = None

    stock_symbols = []
    crypto_symbols = []
    if cfg is not None:
        stock_symbols.extend(getattr(cfg, "STOCK_WATCHLIST", []) or [])
        crypto_symbols.extend(getattr(cfg, "CRYPTO_WATCHLIST", []) or [])

    stock_symbols.extend(_load_watchlist_file("watchlists/stocks.txt"))
    stock_symbols.extend(_load_watchlist_file("watchlist/scanned_stocks.txt"))
    crypto_symbols.extend(_load_watchlist_file("watchlists/crypto.txt"))
    crypto_symbols.extend(_load_watchlist_file("watchlist/scanned_crypto.txt"))

    stocks = _unique_symbols(stock_symbols, crypto=False)
    crypto = _unique_symbols(crypto_symbols, crypto=True)
    return {
        "asset_classes": ["Stocks", "Crypto"],
        "strategies": ["ALL strategies", *BACKTEST_STRATEGIES],
        "timeframes": ["5m", "1h", "1d"],
        "durations": ["30d", "60d", "90d (3mo)", "180d (6mo)", "365d (1y)", "730d (2y)"],
        "symbols": {
            "Stocks": ["ALL", *stocks],
            "Crypto": ["ALL", *crypto],
        },
        "counts": {"stocks": len(stocks), "crypto": len(crypto)},
    }


def _backtest_result_payload(result):
    trades = list(getattr(result, "trades", []) or [])
    wins = [t for t in trades if getattr(t, "pnl", 0) > 0]
    losses = [t for t in trades if getattr(t, "pnl", 0) <= 0]
    avg_win = sum(float(getattr(t, "pnl", 0) or 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(float(getattr(t, "pnl", 0) or 0) for t in losses) / len(losses) if losses else 0
    start_cap = float(getattr(result, "starting_capital", 0) or 0)
    end_cap = float(getattr(result, "ending_capital", start_cap) or start_cap)
    start_date = getattr(result, "start_date", "")
    end_date = getattr(result, "end_date", "")
    stop_mode = getattr(result, "stop_mode", "") or (getattr(trades[0], "stop_mode", "") if trades else "") or "standard"
    win_rate = float(getattr(result, "win_rate", 0) or 0)
    profit_factor = float(getattr(result, "profit_factor", 0) or 0)
    max_drawdown = float(getattr(result, "max_drawdown_pct", 0) or 0)
    verdict = "GO" if win_rate >= 50 and profit_factor >= 1.2 and max_drawdown > -25 else "TUNE"
    equity_curve = list(getattr(result, "equity_curve", []) or [])
    sample_step = max(1, len(equity_curve) // 500)
    sampled_equity = [
        {
            "t": str(getattr(ep, "timestamp", ""))[:16],
            "e": float(getattr(ep, "equity", 0) or 0),
            "d": float(getattr(ep, "drawdown", 0) or 0),
            "r": float(getattr(ep, "daily_ret", 0) or 0),
        }
        for ep in equity_curve[::sample_step]
    ]
    return {
        "symbol": getattr(result, "symbol", ""),
        "strategy": getattr(result, "strategy_name", ""),
        "timeframe": getattr(result, "timeframe", ""),
        "stop_mode": stop_mode,
        "stop": "2-Bar" if str(stop_mode).lower() in ("two_bar", "trailing") else "Standard",
        "start_date": start_date,
        "end_date": end_date,
        "period": f"{str(start_date)[:10]} -> {str(end_date)[:10]}",
        "starting_capital": start_cap,
        "ending_capital": end_cap,
        "wins": len(wins),
        "losses": len(losses),
        "wl": f"{len(wins)}W/{len(losses)}L",
        "verdict": verdict,
        "totalTrades": int(getattr(result, "total_trades", 0) or 0),
        "winRate": win_rate,
        "pnlD": round(end_cap - start_cap, 2),
        "pnlP": float(getattr(result, "total_return_pct", 0) or 0),
        "sharpe": float(getattr(result, "sharpe_ratio", 0) or 0),
        "sortino": float(getattr(result, "sortino_ratio", 0) or 0),
        "maxDD": max_drawdown,
        "profitFactor": profit_factor,
        "avgWin": round(avg_win, 2),
        "avgLoss": round(avg_loss, 2),
        "equity_curve": sampled_equity,
    }


# ---------------------------------------------------------------------------
# Optimizer subprocess state
# ---------------------------------------------------------------------------
_optimizer_progress = {
    "running":    False,
    "iteration":  0,
    "total":      0,
    "best_score": None,
    "strategy":   "",
    "method":     "",
    "cancelled":  False,
    "results":    None,
    "error":      None,
}
_opt_cancel  = threading.Event()
_opt_running = threading.Event()
_opt_proc    = None          # current subprocess.Popen handle
_opt_prog_file = None        # path to progress JSON file

def _opt_monitor(proc, progress_path: str):
    """
    Background thread: polls progress_path every 0.5s and merges into
    _optimizer_progress, then cleans up when the subprocess exits.
    """
    global _opt_proc, _optimizer_progress
    import time as _t, json as _j

    while proc.poll() is None:
        _t.sleep(0.5)
        try:
            with open(progress_path) as _f:
                data = _j.load(_f)
            # Merge into shared dict (preserve keys the worker doesn't write)
            for k in ('running','iteration','total','best_score','cancelled','error','results'):
                if k in data:
                    _optimizer_progress[k] = data[k]
        except Exception:
            pass

    # Process exited — do one final read
    try:
        with open(progress_path) as _f:
            data = _j.load(_f)
        for k in ('running','iteration','total','best_score','cancelled','error','results'):
            if k in data:
                _optimizer_progress[k] = data[k]
        if data.get('results') is None and not data.get('error'):
            import logging as _lg
            _lg.getLogger("dashboard.opt_monitor").warning(
                f"Final progress has no results and no error — worker may have been killed. "
                f"phase={data.get('phase')} iter={data.get('iteration')}/{data.get('total')}"
            )
    except Exception as _fe:
        import logging as _lg
        _lg.getLogger("dashboard.opt_monitor").error(
            f"Final progress read FAILED ({_fe}) — results lost, zombie state likely"
        )

    _optimizer_progress["running"] = False
    _opt_running.clear()
    _opt_proc = None
    try:
        os.unlink(progress_path)
    except Exception:
        pass

def run_optimize_api(payload):
    """
    Launch the optimizer in a subprocess (opt_worker.py) so the heavy
    simulation loop runs in its own Python interpreter with its own GIL,
    keeping the web server fully responsive during optimization.
    """
    global _opt_proc, _opt_prog_file

    import tempfile, json as _j, subprocess as _sp

    strategy   = str(payload.get("strategy") or "").strip()
    if not strategy:
        _optimizer_progress.update({"error": "strategy is required", "results": [], "running": False})
        _opt_running.clear(); return

    asset       = str(payload.get("asset") or "Crypto").lower()
    asset_class = "crypto" if asset.startswith("crypto") else "stock"
    timeframe   = str(payload.get("timeframe") or payload.get("tf") or "1h").strip()
    days        = int(payload.get("days") or 365)
    metric      = str(payload.get("metric")     or "profit_factor").strip()
    minimize    = bool(payload.get("minimize",  False))
    method      = str(payload.get("method")     or "annealing").strip()
    iterations  = int(payload.get("iterations") or 40)

    # Resolve symbols
    raw_syms = payload.get("symbols")
    if not raw_syms or raw_syms == "ALL":
        cfg_syms = get_backtest_config()["symbols"]
        key = "Crypto" if asset_class == "crypto" else "Stocks"
        import config as _cfg_mod
        symbols = [s for s in cfg_syms.get(key, []) if s != "ALL"][:20]
    elif isinstance(raw_syms, list):
        symbols = [s.strip().upper() for s in raw_syms if s.strip()]
    else:
        symbols = [s.strip().upper() for s in str(raw_syms).split(",") if s.strip()]
    if not symbols:
        _optimizer_progress.update({"error": "No symbols resolved", "results": [], "running": False})
        _opt_running.clear(); return

    raw_params = payload.get("params") or []
    if not raw_params:
        _optimizer_progress.update({"error": "params list is required", "results": [], "running": False})
        _opt_running.clear(); return

    # Write config file for worker
    cfg_data = {
        "strategy":    strategy,
        "symbols":     symbols,
        "params":      raw_params,
        "method":      method,
        "iterations":  iterations,
        "days":        days,
        "timeframe":   timeframe,
        "asset_class": asset_class,
        "metric":      metric,
        "minimize":    minimize,
    }
    try:
        cfg_fd, cfg_path = tempfile.mkstemp(suffix='.json', prefix='opt_cfg_')
        with os.fdopen(cfg_fd, 'w') as f:
            _j.dump(cfg_data, f)

        _fd, prog_path = tempfile.mkstemp(suffix='.json', prefix='opt_prog_')
        os.close(_fd)
        _opt_prog_file = prog_path
        # Seed the progress file so the monitor has something to read immediately
        import json as _jj
        with open(prog_path, 'w') as _pf:
            _jj.dump({"running": False, "iteration": 0, "total": iterations,
                      "best_score": None, "cancelled": False, "error": None,
                      "results": None, "phase": "starting"}, _pf)
    except Exception as e:
        _optimizer_progress.update({"error": f"Temp file failed: {e}", "results": [], "running": False})
        _opt_running.clear(); return

    # Initial progress state
    _optimizer_progress.update({
        "running":    False,
        "iteration":  0,
        "total":      iterations,
        "best_score": None,
        "strategy":   strategy,
        "method":     method,
        "cancelled":  False,
        "error":      None,
        "results":    None,
    })

    # Find the python executable (same venv as web_dashboard)
    python_exe = sys.executable
    worker_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'intelligence', 'opt_worker.py')

    try:
        proc = _sp.Popen(
            [python_exe, '-u', worker_script, cfg_path, prog_path],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=_sp.PIPE, stderr=_sp.STDOUT,
        )
        _opt_proc = proc
        _opt_running.set()   # mark optimizer as active — cleared by monitor when done
        # Relay subprocess output directly to terminal
        def _relay_output(p):
            for line in iter(p.stdout.readline, b''):
                text = line.decode('utf-8', errors='replace').rstrip()
                if text:
                    print(f'[worker] {text}', flush=True)
            p.stdout.close()
        threading.Thread(target=_relay_output, args=(proc,), daemon=True).start()
    except Exception as e:
        _optimizer_progress.update({"error": f"Subprocess launch failed: {e}", "results": [], "running": False})
        _opt_running.clear()
        try: os.unlink(cfg_path)
        except: pass
        return

    # Monitor thread — reads progress file and relays to _optimizer_progress
    mon = threading.Thread(target=_opt_monitor, args=(proc, prog_path), daemon=True)
    mon.start()

    # Clean up config file after worker exits (monitor thread handles this)
    def _del_cfg():
        proc.wait()   # block until subprocess exits
        import time as _t; _t.sleep(2)
        try: os.unlink(cfg_path)
        except: pass
    threading.Thread(target=_del_cfg, daemon=True).start()


def run_backtest_api(payload):
    try:
        from intelligence.backtester import Backtester
        import config as cfg
    except Exception as exc:
        return False, f"Backtester unavailable: {exc}", {}

    asset = str(payload.get("asset") or "Stocks")
    symbol = str(payload.get("symbol") or "ALL").strip()
    strategy = str(payload.get("strategy") or "ALL strategies").strip()
    timeframe = str(payload.get("tf") or payload.get("timeframe") or "1h").strip()
    days = _parse_backtest_days(payload.get("duration") or payload.get("days"))
    capital = float(payload.get("cap") or payload.get("capital") or getattr(cfg, "STARTING_CAPITAL", 100000))
    stop_mode = "two_bar" if str(payload.get("mode") or "").lower() in ("trailing", "two_bar") else "standard"
    lookback = max(1, int(float(payload.get("lookback") or 2)))
    entry_side_filter = str(payload.get("side")         or "all").strip().lower()
    entry_mode_filter = str(payload.get("entry_mode")   or "all").strip().lower()
    initial_stop_mode = str(payload.get("initial_stop") or "auto").strip().lower()
    trail_mode        = str(payload.get("trail")        or "auto").strip().lower()
    _commission_raw   = payload.get("commission")
    _slippage_raw     = payload.get("slippage")
    commission_pct    = float(_commission_raw) if _commission_raw not in (None, "", "default") else None
    slippage_pct      = float(_slippage_raw)   if _slippage_raw   not in (None, "", "default") else None

    asset_class = "crypto" if asset.lower().startswith("crypto") or "/" in symbol else "stock"
    if asset_class == "crypto":
        if symbol.upper() == "ALL":
            symbols = [s for s in get_backtest_config()["symbols"]["Crypto"] if s != "ALL"]
        else:
            symbols = _unique_symbols([symbol], crypto=True)
    else:
        if symbol.upper() == "ALL":
            symbols = [s for s in get_backtest_config()["symbols"]["Stocks"] if s != "ALL"]
        else:
            symbols = _unique_symbols([symbol.upper()], crypto=False)
    strategies = BACKTEST_STRATEGIES if strategy == "ALL strategies" else [strategy]

    if not symbols:
        return False, "No symbols selected for backtest.", {}

    # Clear any stale cancel signals from a previous run
    _bt_cancel.clear()
    _bt_running.set()
    try:
        from intelligence.backtester import clear_cancel
        clear_cancel()
    except Exception:
        pass

    results = []
    skipped = 0
    was_cancelled = False
    try:
        bt = Backtester(starting_capital=capital)
        for strat in strategies:
            if _bt_cancel.is_set():
                break
            for sym in symbols:
                if _bt_cancel.is_set():
                    break
                try:
                    if strat == "original_scorer":
                        result = bt.run(
                            sym, days, capital, timeframe, asset_class=asset_class,
                            stop_mode=stop_mode, two_bar_lookback=lookback,
                            commission_pct=commission_pct, slippage_pct=slippage_pct,
                            initial_stop_mode=initial_stop_mode, trail_mode=trail_mode,
                            entry_mode_filter=entry_mode_filter,
                            entry_side_filter=entry_side_filter,
                        )
                    else:
                        result = bt.run_strategy(
                            sym, strat, days, capital, timeframe, asset_class=asset_class,
                            stop_mode=stop_mode, two_bar_lookback=lookback,
                            commission_pct=commission_pct, slippage_pct=slippage_pct,
                            initial_stop_mode=initial_stop_mode, trail_mode=trail_mode,
                            entry_mode_filter=entry_mode_filter,
                            entry_side_filter=entry_side_filter,
                        )
                    if result and getattr(result, "total_trades", 0) > 0:
                        results.append(_backtest_result_payload(result))
                    else:
                        skipped += 1
                except Exception as _exc:
                    if type(_exc).__name__ == "_BacktestCancelled":
                        # Inner-loop cancel checkpoint fired — treat as cancelled
                        _bt_cancel.set()
                        break
                    skipped += 1

        was_cancelled = _bt_cancel.is_set()
    finally:
        _bt_running.clear()
        _bt_cancel.clear()

    if not results:
        if was_cancelled:
            return True, f"Cancelled — no runs completed.", {
                "skipped": skipped,
                "requested_runs": len(symbols) * len(strategies),
                "cancelled": True,
            }
        return False, f"No results; {skipped} run(s) had insufficient data or no trades.", {
            "skipped": skipped,
            "requested_runs": len(symbols) * len(strategies),
            "cancelled": False,
        }

    total_trades = sum(r["totalTrades"] for r in results)
    total_wins = sum(round(r["totalTrades"] * r["winRate"] / 100) for r in results)
    summary = {
        "totalTrades": total_trades,
        "winRate": round((total_wins / total_trades * 100) if total_trades else 0, 1),
        "pnlD": round(sum(r["pnlD"] for r in results), 2),
        "pnlP": round(sum(r["pnlP"] for r in results) / len(results), 2),
        "sharpe": round(sum(r["sharpe"] for r in results) / len(results), 2),
        "maxDD": round(sum(r["maxDD"] for r in results) / len(results), 2),
        "profitFactor": round(sum(r["profitFactor"] for r in results) / len(results), 2),
        "avgWin": round(sum(r["avgWin"] for r in results) / len(results), 2),
        "avgLoss": round(sum(r["avgLoss"] for r in results) / len(results), 2),
    }
    complete_msg = (
        f"Cancelled — partial results: {len(results)} run(s) completed, {skipped} skipped."
        if was_cancelled
        else f"Backtest complete: {len(results)} result(s), {skipped} skipped."
    )
    return True, complete_msg, {
        **summary,
        "results": results,
        "skipped": skipped,
        "requested_runs": len(symbols) * len(strategies),
        "cancelled": was_cancelled,
    }


def get_manual_trade_preview(params):
    symbol = (params.get("symbol") or [""])[0].strip().upper()
    asset_class = (params.get("asset") or params.get("asset_class") or ["stock"])[0].lower()
    direction = (params.get("dir") or params.get("direction") or ["long"])[0].lower()
    if not symbol:
        return False, "Symbol is required.", {}
    try:
        position_size = float((params.get("size") or params.get("position_size") or ["0"])[0] or 0)
        sl_pct = float((params.get("sl") or params.get("stop_loss_pct") or ["1.5"])[0] or 1.5)
        tp_pct = float((params.get("tp") or params.get("take_profit_pct") or ["3.0"])[0] or 3.0)
    except Exception:
        return False, "Position size, stop loss, and take profit must be numeric.", {}
    if asset_class not in ("stock", "crypto"):
        return False, f"Unsupported asset class: {asset_class}", {}
    if direction not in ("long", "short"):
        return False, f"Unsupported direction: {direction}", {}
    if position_size <= 0:
        return False, "Position size must be greater than 0.", {}

    entry = get_current_price(symbol, asset_class)
    if not entry:
        return False, f"Could not fetch live price for {symbol}.", {}
    if direction == "long":
        sl = entry * (1 - sl_pct / 100)
        tp = entry * (1 + tp_pct / 100)
    else:
        sl = entry * (1 + sl_pct / 100)
        tp = entry * (1 - tp_pct / 100)
    return True, "Preview ready.", {
        "symbol": symbol,
        "asset_class": asset_class,
        "direction": direction,
        "entry_price": float(entry),
        "stop_loss": float(sl),
        "take_profit": float(tp),
        "quantity": float(position_size / entry),
        "position_size": position_size,
    }


def get_candles(symbol, asset_class, timeframe="5 Min", limit=100):
    """Return OHLC candles for the HTML chart — via CandleManager cache."""
    try:
        limit = max(10, min(int(limit), 300))
    except Exception:
        limit = 100

    # Normalise dashboard timeframe strings → CandleManager format
    _tf_map = {
        "1 Min": "1m",  "5 Min": "5m",  "15 Min": "15m",
        "1 H":   "1h",  "4 H":   "4h",  "1 D":    "1d",
    }
    tf = _tf_map.get(timeframe, "5m")

    # Kraken native symbol mapping
    _kraken_sym_map = {
        "XBT/USD": "BTC/USD", "XBT/EUR": "BTC/EUR",
        "XDG/USD": "DOGE/USD", "XDG/EUR": "DOGE/EUR",
    }
    lookup_symbol = _kraken_sym_map.get(symbol, symbol)

    try:
        from core.candle_manager import candle_manager
        df = candle_manager.get(lookup_symbol, tf, limit=limit)
        if df is None or df.empty:
            return []
        return [
            {
                "time": str(idx) if asset_class == "stock" else int(idx.timestamp() * 1000),
                "open":  float(row["open"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
            }
            for idx, row in df.iterrows()
        ]
    except Exception as e:
        print(f"[get_candles] CandleManager error for {symbol}: {e}")
        return []


# ---------------------------------------------------------------------------
# Chart panel data — OHLCV bars + bot trade markers
# ---------------------------------------------------------------------------

_CHART_TF_ALPACA = {
    '1m': '1Min', '2m': '2Min', '3m': '3Min', '5m': '5Min',
    '15m': '15Min', '30m': '30Min',
    '1h': '1Hour', '4h': '4Hour', '1D': '1Day', '1W': '1Week',
}
_CHART_TF_CCXT = {
    '1m': '1m', '2m': '2m', '3m': '3m', '5m': '5m',
    '15m': '15m', '30m': '30m',
    '1h': '1h', '4h': '4h', '1D': '1d', '1W': '1w',
}
# IBKR bar size strings for historical data requests
_CHART_TF_IBKR = {
    '1m': '1 min', '2m': '2 mins', '3m': '3 mins', '5m': '5 mins',
    '15m': '15 mins', '30m': '30 mins',
    '1h': '1 hour', '4h': '4 hours', '1D': '1 day', '1W': '1 week',
}


def _chart_bars_alpaca(symbol, timeframe, limit):
    try:
        import alpaca_trade_api as tradeapi
        if not _config:
            return []
        api = tradeapi.REST(_config.ALPACA_API_KEY, _config.ALPACA_SECRET_KEY, _config.ALPACA_BASE_URL)
        tf  = _CHART_TF_ALPACA.get(timeframe, '5Min')
        raw = api.get_bars(symbol, tf, limit=limit, adjustment='raw').df
        if raw.empty:
            return []
        # Flatten MultiIndex columns (newer alpaca-trade-api versions)
        if hasattr(raw.columns, 'levels'):
            raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
        bars = []
        for ts, r in raw.iterrows():
            try:
                bars.append({
                    'time': int(ts.timestamp()),
                    'open': float(r['open']), 'high': float(r['high']),
                    'low':  float(r['low']),  'close': float(r['close']),
                    'volume': float(r.get('volume', 0)),
                })
            except Exception:
                continue
        return bars
    except Exception as e:
        import logging; logging.getLogger(__name__).warning(f"[chart/alpaca] {symbol}: {e}")
        return []


def _chart_bars_ibkr(symbol, timeframe, limit, sec_type='STK', exchange='SMART'):
    # Reuse the bot's live executor singleton — don't create a new connection
    # per chart request (wastes clientIds and triggers event-loop errors).
    try:
        from core.trade_executor import executor as _tex
        ibkr = getattr(_tex, '_ibkr', None)
        if ibkr and ibkr.is_available():
            return ibkr.get_historical_bars(symbol, timeframe, limit,
                                            sec_type=sec_type, exchange=exchange)
    except Exception:
        pass
    # Fallback: yfinance (no connection needed).
    # For futures, caller handles symbol→yf mapping; for stocks/crypto pass as-is.
    return _chart_bars_yfinance(symbol, timeframe, limit)


def _resample_bars(bars_1m, target_minutes):
    """
    Resample a list of 1-minute OHLCV bars into N-minute bars.
    Used for timeframes brokers don't natively support (e.g. Kraken 2m/3m).
    bars_1m must be sorted ascending by time.
    """
    if not bars_1m or target_minutes <= 1:
        return bars_1m
    out = []
    bucket = None
    for b in bars_1m:
        # Snap timestamp to the start of the N-minute bucket
        ts      = int(b['time'])
        bucket_ts = ts - (ts % (target_minutes * 60))
        if bucket is None or bucket['time'] != bucket_ts:
            if bucket:
                out.append(bucket)
            bucket = {
                'time':   bucket_ts,
                'open':   b['open'],
                'high':   b['high'],
                'low':    b['low'],
                'close':  b['close'],
                'volume': b.get('volume', 0),
            }
        else:
            bucket['high']   = max(bucket['high'],  b['high'])
            bucket['low']    = min(bucket['low'],   b['low'])
            bucket['close']  = b['close']
            bucket['volume'] = bucket['volume'] + b.get('volume', 0)
    if bucket:
        out.append(bucket)
    return out


def _chart_bars_kraken(symbol, timeframe, limit):
    try:
        import json as _json
        import logging
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        # Kraken natively supports: 1,5,15,30,60,240,1440,10080 minutes
        # For 2m/3m we fetch 1m and resample in-house
        _resample_to = None
        if timeframe in ('2m', '3m'):
            _resample_to = 2 if timeframe == '2m' else 3
            fetch_interval = 1
            fetch_limit = limit * _resample_to + 10   # extra for clean boundaries
        else:
            fetch_interval = {
                '1m': 1, '5m': 5, '15m': 15, '30m': 30,
                '1h': 60, '4h': 240, '1D': 1440, '1W': 10080,
            }.get(timeframe, 5)
            fetch_limit = limit

        pair = symbol.replace('/', '').replace('-', '').upper()
        if pair.startswith('BTC'):
            pair = pair.replace('BTC', 'XBT', 1)

        url = 'https://api.kraken.com/0/public/OHLC?' + urlencode({
            'pair': pair,
            'interval': fetch_interval,
        })
        req = Request(url, headers={'User-Agent': 'trading-bot-dashboard/1.0'})
        with urlopen(req, timeout=6) as resp:
            payload = _json.loads(resp.read().decode('utf-8'))
        if payload.get('error'):
            logging.getLogger(__name__).warning(f"[chart/kraken] {symbol}: {payload['error']}")
            return []
        result = payload.get('result') or {}
        data_key = next((k for k in result.keys() if k != 'last'), None)
        rows = result.get(data_key) or []
        rows = rows[-fetch_limit:]
        bars = [
            {'time': int(r[0]), 'open': float(r[1]), 'high': float(r[2]),
             'low': float(r[3]), 'close': float(r[4]), 'volume': float(r[6])}
            for r in rows
        ]
        if _resample_to:
            bars = _resample_bars(bars, _resample_to)
        return bars[-limit:]
    except Exception as e:
        logging.getLogger(__name__).warning(f"[chart/kraken] {symbol}: {e}")
        return []


def _chart_bars_coinbase(symbol, timeframe, limit):
    try:
        import logging
        import ccxt
        exc  = ccxt.coinbase({'enableRateLimit': True, 'timeout': 5000})
        tf   = _CHART_TF_CCXT.get(timeframe, '5m')
        rows = exc.fetch_ohlcv(symbol, tf, limit=limit)
        return [
            {'time': int(ts // 1000), 'open': float(o), 'high': float(h),
             'low': float(l), 'close': float(c), 'volume': float(v)}
            for ts, o, h, l, c, v in rows
        ]
    except Exception as e:
        logging.getLogger(__name__).warning(f"[chart/coinbase] {symbol}: {e}")
        return []


_YF_INTERVAL_MAP = {
    '1m': '1m', '2m': '2m', '3m': '5m',   # yfinance has no 3m → use 5m
    '5m': '5m', '15m': '15m', '30m': '30m',
    '1h': '1h', '4h': '1h', '1D': '1d', '1W': '1wk',
    # Sub-minute (not yet exposed in UI — framework only)
    '30s': '1m', '15s': '1m', '1s': '1m',
}

def _chart_bars_tradovate(symbol: str, timeframe: str, limit: int):
    """
    Return bars from the Tradovate real-time WebSocket cache.
    Returns [] if the feed is not running or has no data yet.
    Converts timeframe string ('1m','3m','5m'…) → element_size int.
    """
    global _tradovate_feed
    if not _tradovate_available or _tradovate_feed is None:
        return []

    # If feed stopped due to inactivity (but not auth failure), restart it automatically
    if not _tradovate_feed._running and not _tradovate_feed._auth_failed:
        _tradovate_feed._last_client_activity = __import__('time').time()
        _tradovate_feed.start()

    # Map dashboard symbol (e.g. "MES") → subscription name (e.g. "MESM6_5")
    tf_map = {'1m': 1, '2m': 2, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
              '1h': 60, '4h': 240, '1D': 1440}
    element_size = tf_map.get(timeframe, 5)

    base_name = TRADOVATE_SYMBOL_MAP.get(symbol)
    if not base_name:
        return []

    sub_name = base_name if element_size == 5 else f"{base_name}_{element_size}"

    # Dynamically subscribe if this timeframe wasn't pre-registered.
    # Prefer a known numeric contract ID; fall back to REST lookup; last resort string name.
    # Tradovate's md/getChart WS accepts both numeric IDs and string contract names.
    if sub_name not in _tradovate_feed._subscriptions:
        sym_id = TRADOVATE_SYMBOL_IDS.get(base_name)
        if sym_id is None:
            # Try REST lookup first (may fail with 401 if token has limited ACL)
            looked_up = _tradovate_feed.lookup_symbol_id(base_name)
            if looked_up:
                TRADOVATE_SYMBOL_IDS[base_name] = looked_up
                sym_id = looked_up
                import logging as _lg
                _lg.getLogger(__name__).info(
                    "Tradovate: REST lookup %s → id=%d (cached)", base_name, looked_up)
            else:
                # Fall back to string contract name — the live WS accepts both formats
                sym_id = base_name
                import logging as _lg
                _lg.getLogger(__name__).info(
                    "Tradovate: using string name %s (REST lookup unavailable)", base_name)
        _tradovate_feed.subscribe(sub_name, symbol_id=sym_id,
                                  element_size=element_size)
        # Send the subscription immediately if already authenticated
        if _tradovate_feed._authenticated and _tradovate_feed._ws:
            try:
                _tradovate_feed._send_one_subscription(
                    _tradovate_feed._ws, sub_name,
                    _tradovate_feed._subscriptions[sub_name]
                )
            except Exception:
                pass  # will be sent on next reconnect

    bars = _tradovate_feed.get_bars(sub_name)
    return bars[-limit:] if bars else []


def _chart_bars_yfinance(symbol, timeframe, limit):
    try:
        import yfinance as yf
        from datetime import timedelta
        # yfinance uses BTC-USD format for crypto
        yfSym = symbol.replace('/', '-')
        interval = _YF_INTERVAL_MAP.get(timeframe, '5m')
        # Approximate period needed
        mins_per_bar = {
            '1s':1/60,'15s':0.25,'30s':0.5,
            '1m':1,'2m':2,'3m':3,'5m':5,'15m':15,'30m':30,
            '1h':60,'4h':240,'1D':1440,'1W':10080,
        }.get(timeframe, 5)
        total_days = max(2, (mins_per_bar * limit) // 1440 + 1)
        period_str = f"{min(total_days, 729)}d" if interval not in ('1d','1wk') else f"{min(total_days*2, 1800)}d"
        raw = yf.download(
            yfSym,
            period=period_str,
            interval=interval,
            progress=False,
            auto_adjust=True,
            threads=False,
            timeout=6,
        )
        if raw.empty:
            return []
        # yfinance 0.2.x returns MultiIndex columns for single symbols: ('Open','TSLA')
        # Flatten so row['Open'] works instead of requiring row[('Open','TSLA')]
        if hasattr(raw.columns, 'levels'):
            raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
        bars = []
        for ts, row in raw.iterrows():
            try:
                t = int(ts.timestamp())
                bars.append({'time': t, 'open': float(row['Open']), 'high': float(row['High']),
                             'low': float(row['Low']), 'close': float(row['Close']),
                             'volume': float(row.get('Volume', 0))})
            except Exception:
                continue
        return bars[-limit:]
    except Exception:
        return []


def _futures_front_month_ticker(root: str) -> str:
    """
    Build the yfinance-style specific front-month contract ticker for a futures root.
    e.g.  GC  → 'GCM26'  (Gold June 2026)
          MGC → 'MGCM26'
          ES  → 'ESM26'
          CL  → 'CLM26'

    Month codes: F=Jan G=Feb H=Mar J=Apr K=May M=Jun
                 N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec

    Contract schedules (typical front-month delivery months per asset class):
      Equities / FX / MBT / MET: Mar H / Jun M / Sep U / Dec Z  (quarterly)
      Energy (CL QM MCL NG QG RB HO): every month
      Metals (GC MGC SI SIL PL): Feb G / Apr J / Jun M / Aug Q / Oct V / Dec Z
      Copper (HG MHG): every month
      Rates (ZT ZF ZN ZB UB TN): Mar H / Jun M / Sep U / Dec Z
      Agri grains (ZC ZW ZS ZM ZL): Mar H / May K / Jul N / Sep U / Dec Z
      Livestock (LE HE): Feb G / Apr J / Jun M / Aug Q / Oct V / Dec Z
    """
    from datetime import date as _date

    today = _date.today()
    y, m = today.year, today.month

    # Which delivery months are valid for this root?
    _monthly   = {'CL','MCL','QM','NG','MNG','QG','RB','HO','HG','MHG'}
    _quarterly = {'ES','MES','NQ','MNQ','YM','MYM','RTY','M2K','NKD',
                  '6A','M6A','6B','M6B','6C','6E','M6E','6J','6S','E7','6M','6N',
                  'ZT','ZF','ZN','ZB','UB','TN','MBT','MET'}
    _metals    = {'GC','MGC','SI','SIL','PL'}
    _livestock = {'LE','HE'}
    _grains    = {'ZC','ZW','ZS','ZM','ZL'}

    if root in _monthly:
        delivery_months = list(range(1, 13))
    elif root in _quarterly:
        delivery_months = [3, 6, 9, 12]
    elif root in _metals:
        delivery_months = [2, 4, 6, 8, 10, 12]
    elif root in _livestock:
        delivery_months = [2, 4, 6, 8, 10, 12]
    elif root in _grains:
        delivery_months = [3, 5, 7, 9, 12]
    else:
        delivery_months = [3, 6, 9, 12]   # default quarterly

    # Find the nearest upcoming delivery month (>= current month)
    # Give 15 days lead time — front month is usually the next out by then
    check_month = m + 1 if today.day >= 15 else m
    check_year  = y
    if check_month > 12:
        check_month = 1
        check_year += 1

    for _ in range(13):
        if check_month in delivery_months:
            break
        check_month += 1
        if check_month > 12:
            check_month = 1
            check_year += 1

    _code = {1:'F',2:'G',3:'H',4:'J',5:'K',6:'M',
             7:'N',8:'Q',9:'U',10:'V',11:'X',12:'Z'}
    month_code = _code.get(check_month, 'M')
    year_code  = str(check_year)[-2:]

    return f"{root}{month_code}{year_code}"   # e.g. 'MGCM26', 'ESU26'


def get_chart_data(symbol, timeframe='5m', limit=300, broker=None):
    symbol = symbol.upper().strip()
    is_crypto = '/' in symbol
    is_futures = symbol in FUTURES_SYMBOLS
    try:
        limit = max(50, min(int(limit), 1000))
    except Exception:
        limit = 300

    # Futures: Tradovate real-time feed (if token active) → IBKR → yfinance
    if is_futures:
        # Try Tradovate WebSocket cache first (real-time, ~0 delay).
        # Always attempted unless user explicitly chose ibkr or yfinance in the toolbar.
        force_yfinance  = broker == 'yfinance'
        force_ibkr      = broker == 'ibkr'
        force_tradovate = broker == 'tradovate'
        if not force_yfinance and not force_ibkr:
            tv_bars = _chart_bars_tradovate(symbol, timeframe, limit)
            if tv_bars:
                tv_bars.sort(key=lambda b: b['time'])
                return {'bars': tv_bars[-limit:], 'markers': [], 'open_positions': [],
                        'symbol': symbol, 'timeframe': timeframe, 'source': 'tradovate_rt'}

        # When Tradovate is explicitly selected, skip IBKR (avoids 30s timeout)
        if force_yfinance or force_tradovate:
            bars = []   # skip straight to yfinance below
        else:
            meta = FUTURES_META[symbol]
            exchange = meta[0]
            bars = _chart_bars_ibkr(symbol, timeframe, limit, sec_type='FUT', exchange=exchange)
        if not bars:
            # Build yfinance ticker candidates in priority order:
            #  1. Override mapping (e.g. MGC → GC=F, MES → ES=F)
            #  2. Specific front-month contract (e.g. MGCM26, GCM26)
            #     — yfinance supports named contracts even when =F doesn't exist
            #  3. Standard continuous-contract format (symbol + '=F')
            yf_candidates = []
            override = FUTURES_YF_OVERRIDES.get(symbol)
            if override:
                yf_candidates.append(override)

            # Tier 2: specific front-month contract name
            front = _futures_front_month_ticker(symbol)
            if front and front not in yf_candidates:
                yf_candidates.append(front)

            # Tier 3: generic continuous contract
            std_ticker = symbol + '=F'
            if std_ticker not in yf_candidates:
                yf_candidates.append(std_ticker)

            for yf_sym in yf_candidates:
                bars = _chart_bars_yfinance(yf_sym, timeframe, limit)
                if bars:
                    break

        # Sort, dedup, return early — skip stock/crypto broker logic below
        bars.sort(key=lambda b: b['time'])
        seen_t: set = set()
        deduped = []
        for b in bars:
            if b['time'] not in seen_t:
                seen_t.add(b['time'])
                deduped.append(b)
        return {'bars': deduped[-limit:], 'markers': [], 'open_positions': [],
                'symbol': symbol, 'timeframe': timeframe}

    # Default broker by asset type
    if not broker:
        broker = 'kraken' if is_crypto else 'alpaca'

    _fetchers = {
        'alpaca':   _chart_bars_alpaca,
        'ibkr':     _chart_bars_ibkr,
        'kraken':   _chart_bars_kraken,
        'coinbase': _chart_bars_coinbase,
        'yfinance': _chart_bars_yfinance,
    }
    fetch = _fetchers.get(broker, _chart_bars_alpaca if not is_crypto else _chart_bars_kraken)
    bars = fetch(symbol, timeframe, limit)

    # Fallback if chosen broker returns nothing
    if not bars:
        if is_crypto:
            fallback = _chart_bars_yfinance if fetch is not _chart_bars_yfinance else None
        else:
            # yfinance is the universal stock fallback — don't retry Alpaca on Alpaca failure
            fallback = _chart_bars_yfinance if fetch is not _chart_bars_yfinance else None
        if fallback:
            bars = fallback(symbol, timeframe, limit)

    # Sort ascending — required by Lightweight Charts
    bars.sort(key=lambda b: b['time'])

    # Deduplicate — Alpaca/yfinance can emit two bars at the same timestamp
    # (e.g. DST boundary, or bar-boundary off-by-one).  LightweightCharts v4
    # aborts setData on CandlestickSeries if any duplicate timestamp exists.
    seen_t: set = set()
    deduped = []
    for b in bars:
        if b['time'] not in seen_t:
            seen_t.add(b['time'])
            deduped.append(b)
    bars = deduped

    # Trade markers for this symbol
    markers = []
    try:
        if db:
            trades = db.get_all_closed_trades(limit=500)
            for t in trades:
                if (t.get('symbol') or '').upper() != symbol:
                    continue
                entry_time = t.get('entry_time')
                if entry_time:
                    try:
                        et = datetime.fromisoformat(entry_time)
                        markers.append({
                            'time': int(et.timestamp()), 'type': 'entry',
                            'direction': t.get('direction', 'long'),
                            'price': float(t.get('entry_price', 0)),
                            'pnl': 0,
                        })
                    except Exception:
                        pass
                exit_time = t.get('exit_time')
                exit_price = t.get('exit_price')
                if exit_time and exit_price:
                    try:
                        xt = datetime.fromisoformat(exit_time)
                        pnl = float(t.get('pnl', 0) or 0)
                        markers.append({
                            'time': int(xt.timestamp()), 'type': 'exit',
                            'direction': t.get('direction', 'long'),
                            'price': float(exit_price), 'pnl': pnl,
                        })
                    except Exception:
                        pass
    except Exception:
        pass

    markers.sort(key=lambda m: m['time'])

    open_positions = []
    try:
        if db:
            for t in db.get_open_trades() or []:
                if (t.get('symbol') or '').upper() != symbol:
                    continue
                entry_time = t.get('entry_time')
                entry_ts = None
                if entry_time:
                    try:
                        entry_ts = int(datetime.fromisoformat(entry_time).timestamp())
                    except Exception:
                        entry_ts = None
                direction = t.get('direction', 'long')
                entry_price = float(t.get('entry_price') or 0)
                stop_loss = float(t.get('stop_loss') or 0)
                take_profit = float(t.get('take_profit') or 0)
                current_price = get_current_price(
                    t.get('symbol') or symbol,
                    t.get('asset_class') or ('crypto' if '/' in symbol else 'stock')
                ) or entry_price
                open_positions.append({
                    'trade_id': t.get('trade_id') or t.get('id'),
                    'symbol': t.get('symbol') or symbol,
                    'direction': direction,
                    'entry_time': entry_time,
                    'entry_ts': entry_ts,
                    'entry_price': entry_price,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'current_price': float(current_price or 0),
                    'strategy_name': t.get('strategy_name') or '',
                })
    except Exception:
        pass

    return {
        'bars': bars,
        'markers': markers,
        'open_positions': open_positions,
        'symbol': symbol,
        'timeframe': timeframe,
    }


# ── Futures symbol registry ──────────────────────────────────────────────────
# Maps CME/NYMEX/CBOT/COMEX symbol → (exchange, description, category)
# Exchange strings match IBKR's ContFuture routing.
# yfinance continuous contract ticker = symbol + '=F'
FUTURES_META = {
    # CME Equity Indices
    'ES':  ('CME',   'E-mini S&P 500',           'Equity'),
    'MES': ('CME',   'Micro E-mini S&P 500',      'Equity'),
    'NQ':  ('CME',   'E-mini NASDAQ 100',         'Equity'),
    'MNQ': ('CME',   'Micro E-mini NASDAQ 100',   'Equity'),
    'RTY': ('CME',   'E-mini Russell 2000',       'Equity'),
    'M2K': ('CME',   'Micro E-mini Russell 2000', 'Equity'),
    'NKD': ('CME',   'Nikkei 225',                'Equity'),
    'MBT': ('CME',   'Micro E-mini Bitcoin',      'Equity'),
    'MET': ('CME',   'Micro E-mini Ether',        'Equity'),
    # CBOT Equity
    'YM':  ('CBOT',  'Mini-DOW',                  'Equity'),
    'MYM': ('CBOT',  'Micro Mini-DOW',            'Equity'),
    # NYMEX Energy
    'CL':  ('NYMEX', 'Crude Oil',                 'Energy'),
    'MCL': ('NYMEX', 'Micro Crude Oil',           'Energy'),
    'QM':  ('NYMEX', 'E-mini Crude Oil',          'Energy'),
    'NG':  ('NYMEX', 'Natural Gas',               'Energy'),
    'MNG': ('NYMEX', 'Micro Henry Hub Nat Gas',   'Energy'),
    'QG':  ('NYMEX', 'E-mini Natural Gas',        'Energy'),
    'RB':  ('NYMEX', 'RBOB Gasoline',             'Energy'),
    'HO':  ('NYMEX', 'Heating Oil',               'Energy'),
    # NYMEX Metals
    'PL':  ('NYMEX', 'Platinum',                  'Metals'),
    # COMEX Metals
    'GC':  ('COMEX', 'Gold',                      'Metals'),
    'MGC': ('COMEX', 'Micro Gold',                'Metals'),
    'SI':  ('COMEX', 'Silver',                    'Metals'),
    'SIL': ('COMEX', 'Micro Silver',              'Metals'),
    'HG':  ('COMEX', 'Copper',                    'Metals'),
    'MHG': ('COMEX', 'Micro Copper',              'Metals'),
    # CME FX
    '6A':  ('CME',   'Australian Dollar',         'FX'),
    'M6A': ('CME',   'Micro AUD/USD',             'FX'),
    '6B':  ('CME',   'British Pound',             'FX'),
    'M6B': ('CME',   'Micro GBP/USD',             'FX'),
    '6C':  ('CME',   'Canadian Dollar',           'FX'),
    '6E':  ('CME',   'Euro FX',                   'FX'),
    'M6E': ('CME',   'Micro EUR/USD',             'FX'),
    '6J':  ('CME',   'Japanese Yen',              'FX'),
    '6S':  ('CME',   'Swiss Franc',               'FX'),
    'E7':  ('CME',   'E-mini Euro FX',            'FX'),
    '6M':  ('CME',   'Mexican Peso',              'FX'),
    '6N':  ('CME',   'New Zealand Dollar',        'FX'),
    # CBOT Financial / Interest Rates
    'ZT':  ('CBOT',  '2-Year Note',               'Rates'),
    'ZF':  ('CBOT',  '5-Year Note',               'Rates'),
    'ZN':  ('CBOT',  '10-Year Note',              'Rates'),
    'ZB':  ('CBOT',  '30-Year Bond',              'Rates'),
    'UB':  ('CBOT',  'Ultra-Bond',                'Rates'),
    'TN':  ('CBOT',  'Ultra-Note',                'Rates'),
    # CBOT Agricultural
    'ZC':  ('CBOT',  'Corn',                      'Agriculture'),
    'ZW':  ('CBOT',  'Wheat',                     'Agriculture'),
    'ZS':  ('CBOT',  'Soybean',                   'Agriculture'),
    'ZM':  ('CBOT',  'Soybean Meal',              'Agriculture'),
    'ZL':  ('CBOT',  'Soybean Oil',               'Agriculture'),
    # CME Agricultural (Livestock)
    'HE':  ('CME',   'Lean Hogs',                 'Agriculture'),
    'LE':  ('CME',   'Live Cattle',               'Agriculture'),
}

FUTURES_SYMBOLS = set(FUTURES_META.keys())

# ── yfinance ticker map for futures ─────────────────────────────────────────
# Most contracts → symbol + '=F'  (ES=F, GC=F, NQ=F, CL=F …)
# Micro contracts often have no yfinance coverage → fall back to the
# parent (standard-size) contract so the chart still shows real price data.
#
# Format: symbol → yfinance_ticker_to_try_first
# If that returns empty, _chart_bars_futures_yfinance tries symbol+'=F' as a
# last resort before giving up.
FUTURES_YF_OVERRIDES = {
    # ── Equity Indices ──────────────────────────────────────────────────
    'MES': 'ES=F',   # Micro E-mini S&P 500  → use E-mini
    'MNQ': 'NQ=F',   # Micro E-mini NASDAQ   → use E-mini
    'M2K': 'RTY=F',  # Micro Russell 2000    → use E-mini
    'MYM': 'YM=F',   # Micro Mini-DOW        → use Mini-DOW
    # ── Energy ──────────────────────────────────────────────────────────
    'MCL': 'CL=F',   # Micro Crude Oil       → use CL
    # ── Metals ──────────────────────────────────────────────────────────
    'MGC': 'GC=F',   # Micro Gold            → use Gold
    'SIL': 'SI=F',   # Micro Silver          → use Silver
    'MHG': 'HG=F',   # Micro Copper          → use Copper
    # ── FX ──────────────────────────────────────────────────────────────
    'M6A': '6A=F',   # Micro AUD/USD         → use 6A
    'M6B': '6B=F',   # Micro GBP/USD         → use 6B
    'M6E': '6E=F',   # Micro EUR/USD         → use 6E
    # ── Crypto Futures ──────────────────────────────────────────────────
    'MBT': 'BTC-USD', # Micro Bitcoin        → spot BTC as proxy
    'MET': 'ETH-USD', # Micro Ether          → spot ETH as proxy
}


def get_chart_symbols():
    stocks = list(getattr(_config, 'STOCK_WATCHLIST', []) or [])
    crypto = list(getattr(_config, 'CRYPTO_WATCHLIST', []) or [])
    try:
        inj = get_injected_symbols()
        stocks = list(dict.fromkeys(stocks + inj.get('stocks', [])))
        crypto = list(dict.fromkeys(crypto + inj.get('crypto', [])))
    except Exception:
        pass
    futures = [
        {'symbol': sym, 'type': 'futures', 'name': meta[1], 'category': meta[2]}
        for sym, meta in FUTURES_META.items()
    ]
    return (
        [{'symbol': s, 'type': 'stock'} for s in stocks] +
        [{'symbol': s, 'type': 'crypto'} for s in crypto] +
        futures
    )


WATCHLIST_DIR = ROOT / "watchlist"
INJECTED_STOCKS_FILE = WATCHLIST_DIR / "scanned_stocks.txt"
INJECTED_CRYPTO_FILE = WATCHLIST_DIR / "scanned_crypto.txt"


def _read_symbol_file(path):
    try:
        if not path.exists():
            return []
        return [line.strip() for line in path.read_text().splitlines() if line.strip()]
    except Exception:
        return []


def get_injected_symbols():
    return {
        "stocks": _read_symbol_file(INJECTED_STOCKS_FILE),
        "crypto": _read_symbol_file(INJECTED_CRYPTO_FILE),
        "files": {
            "stocks": str(INJECTED_STOCKS_FILE),
            "crypto": str(INJECTED_CRYPTO_FILE),
        },
    }


def inject_symbols(kind, raw_symbols, replace=False):
    if isinstance(raw_symbols, str):
        symbols = raw_symbols.replace(",", "\n").splitlines()
    else:
        symbols = raw_symbols or []

    cleaned = []
    for symbol in symbols:
        value = str(symbol or "").strip().upper()
        if not value:
            continue
        if kind == "crypto" and "/" not in value:
            value = f"{value}/USD"
        if value not in cleaned:
            cleaned.append(value)

    if kind not in ("stocks", "stock", "crypto"):
        return False, f"Unsupported injection kind: {kind}", get_injected_symbols()
    if not cleaned:
        return False, "No valid symbols entered.", get_injected_symbols()

    path = INJECTED_CRYPTO_FILE if kind == "crypto" else INJECTED_STOCKS_FILE
    WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)
    if replace:
        # Replace mode: file becomes exactly the submitted list — anything else is removed
        path.write_text("\n".join(cleaned))
        label = "crypto pair" if kind == "crypto" else "stock"
        return True, f"Replaced with {len(cleaned)} {label}(s): {', '.join(cleaned)}", get_injected_symbols()
    else:
        # Append mode: merge new symbols in front, keep existing ones not in the new list
        existing = [s for s in _read_symbol_file(path) if s not in cleaned]
        path.write_text("\n".join(cleaned + existing))
        label = "crypto pair" if kind == "crypto" else "stock"
        return True, f"Added {len(cleaned)} {label}(s): {', '.join(cleaned)}", get_injected_symbols()


def clear_injected_symbols():
    WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)
    INJECTED_STOCKS_FILE.write_text("")
    INJECTED_CRYPTO_FILE.write_text("")
    return {"stocks": [], "crypto": []}


def _format_trade_time(raw):
    if not raw:
        return "—", None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.strftime("%m/%d %H:%M"), parsed
    except Exception:
        return str(raw)[:16], None


def _normalize_trade_datetimes(entry_dt, exit_dt):
    if not entry_dt or not exit_dt:
        return entry_dt, exit_dt
    if entry_dt <= exit_dt:
        return entry_dt, exit_dt

    hours_ahead = (entry_dt - exit_dt).total_seconds() / 3600
    if 0 < hours_ahead <= 8.5:
        adjusted = entry_dt - timedelta(hours=7)
        if adjusted <= exit_dt:
            return adjusted, exit_dt
    return entry_dt, exit_dt


def _format_duration(entry_dt, exit_dt):
    if not entry_dt or not exit_dt:
        return "—"
    if entry_dt > exit_dt:
        return "—"
    minutes = int((exit_dt - entry_dt).total_seconds() / 60)
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes}m"


def get_today_trade_log():
    try:
        trades = db.get_trades_for_date(date.today().isoformat()) if db is not None else []
    except Exception:
        trades = []

    if not trades:
        try:
            conn = sqlite3.connect(DB_PATH)
 #           conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            trades = [dict(r) for r in conn.execute(
                "SELECT * FROM trades WHERE status='closed' AND date(exit_time)=? ORDER BY exit_time",
                (date.today().isoformat(),),
            ).fetchall()]
            conn.close()
        except Exception:
            trades = []

    rows = []
    for trade in trades:
        if trade.get("status") != "closed":
            continue
        opened, entry_dt = _format_trade_time(trade.get("entry_time"))
        closed, exit_dt = _format_trade_time(trade.get("exit_time"))
        entry_dt, exit_dt = _normalize_trade_datetimes(entry_dt, exit_dt)
        if entry_dt:
            opened = entry_dt.strftime("%m/%d %H:%M")
        if exit_dt:
            closed = exit_dt.strftime("%m/%d %H:%M")
        pnl = float(trade.get("pnl") or 0)
        pnl_pct = float(trade.get("pnl_pct") or 0)
        rows.append({
            "id": trade.get("id"),
            "trade_id": trade.get("trade_id"),
            "opened": opened,
            "closed": closed,
            "duration": _format_duration(entry_dt, exit_dt),
            "symbol": trade.get("symbol") or "?",
            "direction": str(trade.get("direction") or "").upper(),
            "strategy": trade.get("strategy_name") or "original",
            "entry_price": float(trade.get("entry_price") or 0),
            "exit_price": float(trade.get("exit_price") or 0),
            "reason": str(trade.get("exit_reason") or "").replace("_", " ").title(),
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "result": "Win" if pnl > 0 else "Loss",
            "tp_hit_count": int(trade.get("tp_hit_count") or 0),
            "_sort": trade.get("exit_time") or "",
        })

    rows.sort(key=lambda row: row["_sort"], reverse=True)
    for row in rows:
        row.pop("_sort", None)
    return rows


def get_overview_data():
    if db is None:
        return {}

    try:
        status = risk_manager.get_daily_status() if risk_manager else None
    except Exception:
        status = None

    cap = db.get_latest_capital() or {}
    open_trades = db.get_open_trades() or []
    closed_today = get_today_trade_log()
    wins_today = sum(1 for trade in closed_today if float(trade.get("pnl") or 0) > 0)
    losses_today = sum(1 for trade in closed_today if float(trade.get("pnl") or 0) <= 0)
    closed_trade_count = len(closed_today)
    win_rate = (wins_today / closed_trade_count * 100) if closed_trade_count else 0
    closed_today_pnl = sum(float(trade.get("pnl") or 0) for trade in closed_today)

    daily_summaries = db.get_daily_summaries(370) or []
    daily_perf = [
        {
            "date": s.get("trade_date"),
            "trades": int(s.get("total_trades") or 0),
            "wins": int(s.get("winning_trades") or 0),
            "losses": int(s.get("losing_trades") or 0),
            "win_rate": float(s.get("win_rate") or 0),
            "pnl": float(s.get("daily_pnl") or 0),
            "pnl_pct": float(s.get("daily_pnl_pct") or 0),
            "capital": float(s.get("ending_capital") or 0),
            "goal_met": bool(s.get("goal_met")),
            "trading_halted": bool(s.get("trading_halted")),
        }
        for s in daily_summaries
    ]

    capital_history = [
        {
            "date": row["date"],
            "capital": row["capital"],
        }
        for row in reversed([
            {"date": s.get("trade_date"), "capital": float(s.get("ending_capital") or 0)}
            for s in daily_summaries
            if s.get("trade_date")
        ])
    ]
    if cap:
        today = date.today().isoformat()
        current_capital = float(cap.get("total_capital") or 0)
        if not capital_history or capital_history[-1]["date"] != today:
            capital_history.append({"date": today, "capital": current_capital})

    if status:
        trading_active = bool(status.trading_active)
        halt_reason = status.halt_reason or ""
        session_daily_pnl = float(status.pnl_today or 0)
        consecutive_losses = int(status.consecutive_losses or 0)
        status_trades_today = int(status.trades_today or 0)
        capital = float(status.capital or cap.get("total_capital") or 0)
        starting_capital_today = float(status.starting_capital_today or 0)
    else:
        session = db.get_session(date.today().isoformat()) or {}
        trading_active = not bool(session.get("trading_halted", 0))
        halt_reason = session.get("halt_reason") or ""
        session_daily_pnl = float(session.get("pnl_today") or 0)
        starting_capital_today = float(session.get("starting_capital_today") or 0)
        consecutive_losses = int(session.get("consecutive_losses") or 0)
        status_trades_today = int(session.get("trades_today") or 0)
        capital = float(cap.get("total_capital") or 0)

    daily_pnl = closed_today_pnl
    daily_pnl_pct = (daily_pnl / starting_capital_today * 100) if starting_capital_today else 0

    return {
        "trading_active": trading_active,
        "halt_reason": halt_reason,
        "bot_process_running": is_bot_running(),
        "capital": capital,
        "available_cash": float(cap.get("available_cash") or 0),
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": daily_pnl_pct,
        "session_daily_pnl": session_daily_pnl,
        "open_positions": len(open_trades),
        "trades_today": closed_trade_count,        # matches Trade Log count
        "session_trades_today": status_trades_today, # session counter (since last bot start)
        "closed_trades_today": closed_trade_count,
        "wins_today": wins_today,
        "losses_today": losses_today,
        "win_rate_today": win_rate,
        "consecutive_losses": consecutive_losses,
        "capital_history": capital_history,
        "daily_performance": daily_perf,
    }


def get_quick_status(year=None):
    year = int(year or date.today().year)
    status = {}

    try:
        from intelligence.ml_scorer import ml_scorer
        status["ml"] = ml_scorer.get_status()
    except Exception:
        status["ml"] = {"stage": "unknown", "progress_pct": 0, "message": "ML scorer initializing."}

    try:
        status["tax"] = db.get_tax_year_summary(year) if db is not None else {}
    except Exception:
        status["tax"] = {}

    try:
        from intelligence.condition_detector import condition_detector
        from scanners.market_scanner import scanner
        cond = condition_detector.get_spy_condition(scanner.stock_scanner)
        status["market_condition"] = {
            "condition": getattr(cond.condition, "value", str(cond.condition)),
            "confidence": float(cond.confidence or 0),
            "position_scalar": float(cond.position_scalar or 0),
            "should_trade": bool(cond.should_trade),
            "reason": cond.reason,
            "adx": float(cond.adx or 0),
            "atr_pct": float(cond.atr_pct or 0),
            "bb_width": float(cond.bb_width or 0),
        }
    except Exception:
        status["market_condition"] = {
            "condition": "unknown",
            "confidence": 0,
            "position_scalar": 0,
            "should_trade": True,
            "reason": "Condition detector initializing.",
            "adx": 0,
            "atr_pct": 0,
            "bb_width": 0,
        }

    status["year"] = year
    return status


def generate_daily_report():
    try:
        from reporting.report_generator import ReportGenerator

        generator = ReportGenerator()
        summary = generator.generate_daily_summary()
        if db is not None:
            db.save_daily_summary(summary)
        return True, "Daily report generated and saved.", {"summary": summary}
    except Exception as exc:
        return False, f"Report generation failed: {exc}", {}


def export_tax_csv(year):
    try:
        year = int(year or date.today().year)
        exports = ROOT / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        filepath = exports / f"tax_form_8949_{year}.csv"
        db.export_8949_csv(year, str(filepath))
        return True, f"Saved: {filepath}", {"path": str(filepath)}
    except Exception as exc:
        return False, f"Tax export failed: {exc}", {}


def process_withdrawal(amount, reason):
    try:
        result = risk_manager.process_withdrawal(float(amount or 0), str(reason or "Living expenses"))
        return bool(result.get("approved")), result.get("message", "Withdrawal processed."), result
    except Exception as exc:
        return False, f"Withdrawal failed: {exc}", {}


def retrain_ml():
    try:
        from intelligence.ml_scorer import ml_scorer

        success = bool(ml_scorer.retrain())
        msg = "Retrain complete." if success else "Retrain skipped: not enough closed trades yet."
        return True, msg, {"ml": ml_scorer.get_status()}
    except Exception as exc:
        return False, f"ML retrain failed: {exc}", {}


def chat_with_bot(message):
    text = str(message or "").strip()
    if not text:
        return False, "Message is required.", {}
    try:
        from intelligence.chat_interface import chat

        reply = chat.chat(text)
        return True, reply, {"reply": reply}
    except Exception as exc:
        return False, f"Chat failed: {exc}", {}


# Module-level Chronos pipeline cache — load once, reuse across all calls.
# Lock prevents double-load race condition under ThreadingHTTPServer.
# None = not yet attempted; pipeline object = ready; "failed" string = gave up.
import threading as _threading
_CHRONOS_PIPELINE      = None
_CHRONOS_LOAD_ATTEMPTED = False
_CHRONOS_LOCK          = _threading.Lock()


def _load_chronos_pipeline():
    """
    Load amazon/chronos-t5-base once and cache it.
    Returns the pipeline object or None if unavailable.

    Fixes vs. original:
      - device_map="cpu" removed — caused meta-tensor crash on some torch/accelerate
        combinations (Cannot copy out of meta tensor). CPU is the default anyway.
      - torch_dtype → dtype (torch_dtype deprecated in newer transformers).
      - Suppress the spurious lm_head.weight warning (known Chronos checkpoint quirk).
    """
    global _CHRONOS_PIPELINE, _CHRONOS_LOAD_ATTEMPTED
    # Fast path — no lock needed once flag is set
    if _CHRONOS_LOAD_ATTEMPTED:
        return _CHRONOS_PIPELINE

    # Slow path — acquire lock so only one thread does the load
    with _CHRONOS_LOCK:
        if _CHRONOS_LOAD_ATTEMPTED:          # re-check inside lock
            return _CHRONOS_PIPELINE
        _CHRONOS_LOAD_ATTEMPTED = True

    _log = logging.getLogger("dashboard.chronos")
    try:
        import warnings
        import torch
        from chronos import ChronosPipeline

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*torch_dtype.*deprecated.*")
            warnings.filterwarnings("ignore", message=".*lm_head.weight.*")
            warnings.filterwarnings("ignore", message=".*newly initialized.*")
            _CHRONOS_PIPELINE = ChronosPipeline.from_pretrained(
                "amazon/chronos-t5-base",
                dtype=torch.float32,   # removed device_map — CPU is default, avoids meta-tensor crash
            )
        _log.info("[Chronos] model loaded successfully (amazon/chronos-t5-base)")
    except Exception as e:
        _log.warning(f"[Chronos] model load failed, will use heuristic fallback: {e}")
        _CHRONOS_PIPELINE = None

    return _CHRONOS_PIPELINE


def _chronos_predict(payload):
    """
    POST /api/chronos_predict
    Receives: { symbol, timeframe, bars: [{time,open,high,low,close}, ...] }
    Returns:  { direction: 'up'|'down'|'flat', confidence: float, model: str }

    Uses amazon/chronos-t5-base (cached after first load).
    Falls back to a heuristic if the model isn't available.
    """
    import numpy as np
    _log = logging.getLogger("dashboard.chronos")

    bars    = payload.get("bars") or []
    symbol  = payload.get("symbol", "?")
    tf      = payload.get("timeframe", "?")

    if len(bars) < 8:
        return {"direction": "flat", "confidence": 0.5, "model": "insufficient_data"}

    closes = np.array([float(b.get("close", 0)) for b in bars], dtype=np.float32)

    # ── Try Chronos T5 (cached pipeline) ────────────────────────────────────
    try:
        import torch
        pipeline = _load_chronos_pipeline()
        if pipeline is not None:
            context    = torch.tensor(closes[-64:]).unsqueeze(0)   # (1, seq)
            # Forecast 1 step, 20 samples → get a distribution
            forecast   = pipeline.predict(context, prediction_length=1, num_samples=20)
            # forecast shape: (1, 20, 1)
            samples    = forecast[0, :, 0].cpu().numpy()           # (20,)
            last_close = float(closes[-1])
            up_pct     = float(np.mean(samples > last_close))
            down_pct   = float(np.mean(samples < last_close))
            flat_pct   = 1.0 - up_pct - down_pct

            if up_pct >= down_pct and up_pct >= flat_pct:
                direction, confidence = "up",   up_pct
            elif down_pct >= up_pct and down_pct >= flat_pct:
                direction, confidence = "down", down_pct
            else:
                direction, confidence = "flat", flat_pct

            _log.info(f"[Chronos] {symbol}/{tf}: {direction} {confidence:.2%}")
            return {"direction": direction, "confidence": round(confidence, 3), "model": "chronos-t5-base"}

    except Exception as e:
        _log.warning(f"[Chronos] prediction error: {e}")

    # ── Heuristic fallback: momentum + mean-reversion composite ────────────
    try:
        # Simple: compare last 3 closes vs 10-bar EMA; RSI(7) direction
        n       = len(closes)
        w       = np.exp(np.linspace(-1, 0, min(10, n)))
        w      /= w.sum()
        ema10   = float(np.dot(w, closes[-len(w):]))
        recent  = float(np.mean(closes[-3:]))
        mom     = recent - ema10

        # RSI(7) last value
        delta   = np.diff(closes[-8:]) if n >= 8 else np.array([0.0])
        gain    = np.mean(np.where(delta > 0, delta, 0))
        loss    = np.mean(np.where(delta < 0, -delta, 0))
        rsi     = 50.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss)

        score = 0.0
        if mom > 0: score += 0.4
        else:       score -= 0.4
        if rsi < 40: score += 0.3   # oversold → up
        if rsi > 60: score -= 0.3   # overbought → down

        if score > 0.1:   direction, confidence = "up",   min(0.55 + abs(score)*0.3, 0.75)
        elif score < -0.1: direction, confidence = "down", min(0.55 + abs(score)*0.3, 0.75)
        else:              direction, confidence = "flat", 0.50

        return {"direction": direction, "confidence": round(confidence, 3), "model": "heuristic"}
    except Exception as e2:
        _log.error(f"[Chronos] heuristic also failed: {e2}")
        return {"direction": "flat", "confidence": 0.5, "model": "error"}


def open_manual_trade(payload):
    """Open a manual trade and register it in the DB for bot management."""
    try:
        import uuid
        from datetime import datetime as dt

        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            return False, "Symbol is required.", {}

        asset_class = str(payload.get("asset") or payload.get("asset_class") or "stock").lower()
        direction = str(payload.get("dir") or payload.get("direction") or "long").lower()
        broker = str(payload.get("broker") or "ibkr").lower()
        strategy_label = str(payload.get("label") or payload.get("strategy") or "manual")
        position_size = float(payload.get("size") or payload.get("position_size") or 0)
        sl_pct = float(payload.get("sl") or payload.get("stop_loss_pct") or 1.5)
        tp_pct = float(payload.get("tp") or payload.get("take_profit_pct") or 3.0)
        overnight = bool(payload.get("overnight"))

        if asset_class not in ("stock", "crypto"):
            return False, f"Unsupported asset class: {asset_class}", {}
        if direction not in ("long", "short"):
            return False, f"Unsupported direction: {direction}", {}
        if position_size <= 0:
            return False, "Position size must be greater than 0.", {}

        entry = get_current_price(symbol, asset_class)
        if not entry:
            return False, f"Could not get live price for {symbol}.", {}

        qty = position_size / entry
        if direction == "long":
            sl = entry * (1 - sl_pct / 100)
            tp = entry * (1 + tp_pct / 100)
            side = "buy"
        else:
            sl = entry * (1 + sl_pct / 100)
            tp = entry * (1 - tp_pct / 100)
            side = "sell"

        broker_order_id = None
        actual_fill_price = entry

        if broker == "ibkr" and asset_class == "stock":
            try:
                from core.trade_executor import executor
                if not executor._ibkr or not executor._ibkr.is_available():
                    return False, "IBKR is not available. Check TWS is running on port 7497.", {}
                submit_qty = int(qty) if qty >= 1 else qty
                result = executor._ibkr.submit_order(
                    symbol=symbol,
                    qty=submit_qty,
                    side=side,
                    stop_loss=round(sl, 2),
                    take_profit=round(tp, 2),
                )
                if not result:
                    return False, f"IBKR rejected the order for {symbol}.", {}
                broker_order_id = result.get("broker_order_id")
                fill_price = result.get("filled_avg_price", 0)
                actual_fill_price = fill_price if fill_price and fill_price > 0 else entry
            except Exception as exc:
                return False, f"IBKR order error: {exc}", {}

        elif broker == "alpaca" and asset_class == "stock":
            try:
                from core.trade_executor import AlpacaExecutor
                alpaca_ex = AlpacaExecutor()
                submit_qty = int(qty) if qty >= 1 else qty
                result = alpaca_ex.submit_order(
                    symbol=symbol,
                    qty=submit_qty,
                    side=side,
                    stop_loss=round(sl, 2),
                    take_profit=round(tp, 2),
                )
                if not result:
                    return False, f"Alpaca rejected the order for {symbol}.", {}
                broker_order_id = result.get("broker_order_id")
                fill_price = result.get("filled_avg_price", 0)
                actual_fill_price = fill_price if fill_price and fill_price > 0 else entry
            except Exception as exc:
                return False, f"Alpaca order error: {exc}", {}

        if direction == "long":
            sl = actual_fill_price * (1 - sl_pct / 100)
            tp = actual_fill_price * (1 + tp_pct / 100)
        else:
            sl = actual_fill_price * (1 + sl_pct / 100)
            tp = actual_fill_price * (1 - tp_pct / 100)
        qty = position_size / actual_fill_price

        trade_id = str(uuid.uuid4())[:12]
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            INSERT INTO trades
                (trade_id, symbol, asset_class, direction, status,
                 entry_time, entry_price, quantity, position_value,
                 stop_loss, take_profit, pnl, pnl_pct,
                 signal_score, broker, strategy_name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,1.0,?,?)
            """,
            (
                trade_id,
                symbol,
                asset_class,
                direction,
                "open",
                dt.now().isoformat(),
                round(actual_fill_price, 6),
                round(qty, 6),
                round(position_size, 2),
                round(sl, 6),
                round(tp, 6),
                broker,
                strategy_label,
            ),
        )
        try:
            conn.execute(
                "UPDATE trades SET is_overnight=?, broker_order_id=? WHERE trade_id=?",
                (1 if overnight else 0, broker_order_id, trade_id),
            )
        except Exception:
            pass
        conn.commit()
        conn.close()

        return True, (
            f"{direction.upper()} {symbol} opened @ ${actual_fill_price:.6f} | "
            f"SL: ${sl:.6f} | TP: ${tp:.6f} | Qty: {qty:.4f} | Broker: {broker}"
        ), {"trade_id": trade_id, "entry_price": actual_fill_price, "quantity": qty}
    except Exception as exc:
        return False, f"Trade entry error: {exc}", {}


# =============================================================================
# Database browser helpers
# =============================================================================

_DB_TABLE_SEARCH = {
    "trades":           {"strategy":True,"status":True,"symbol":True,
                         "broker":True,"time_col":"entry_time"},
    "capital":          {"time_col":"timestamp"},
    "daily_summaries":  {"time_col":"trade_date"},
    "settlement_queue": {"time_col":"created_at"},
    "ai_signal_reviews":{"strategy":True,"symbol":True,"time_col":"timestamp"},
    "strategy_results": {"strategy":True,"time_col":"recorded_at"},
    "tax_ledger":       {"symbol":True,"time_col":"close_date"},
    "withdrawals":      {"time_col":"timestamp"},
    "chat_actions":     {"time_col":"timestamp"},
    "fund_events":      {"time_col":"timestamp"},
    "bot_state":        {},
    "session_state":    {},
}

def _db_conn():
    c = sqlite3.connect(str(DB_PATH))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=3000")
    c.row_factory = sqlite3.Row
    return c

def db_tables():
    rows = []
    try:
        with _db_conn() as c:
            tables = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            for t in tables:
                name = t[0]
                cnt  = c.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
                rows.append({"name": name, "rows": cnt})
    except Exception as e:
        return {"error": str(e), "tables": []}
    return {"tables": rows}

def db_browse(table: str, limit: int = 500, offset: int = 0):
    # Whitelist: only real table names
    try:
        with _db_conn() as c:
            valid = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except Exception as e:
        return {"error": str(e)}
    if table not in valid:
        return {"error": f"Unknown table: {table}"}
    try:
        with _db_conn() as c:
            cur  = c.execute(
                f"SELECT rowid, * FROM [{table}] ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        return {"table": table, "cols": cols, "rows": rows,
                "limit": limit, "offset": offset}
    except Exception as e:
        return {"error": str(e)}

_QUICK_SQL = {
    # rowid is first column so the frontend edit-row feature works (applyResult strips it)
    "Open Today":      """SELECT rowid,trade_id,symbol,direction,status,strategy_name,entry_time,entry_price,stop_loss,take_profit,position_value,broker FROM trades WHERE status='open' AND DATE(entry_time)=DATE('now','localtime') ORDER BY entry_time DESC""",
    "Closed Today":    """SELECT rowid,trade_id,symbol,direction,status,strategy_name,entry_time,exit_time,entry_price,exit_price,pnl,pnl_pct,exit_reason,broker FROM trades WHERE status='closed' AND DATE(exit_time)=DATE('now','localtime') ORDER BY exit_time DESC""",
    "All Open":        """SELECT rowid,trade_id,symbol,direction,status,strategy_name,entry_time,entry_price,stop_loss,take_profit,position_value,broker FROM trades WHERE status='open' ORDER BY entry_time DESC""",
    "Ghost Trades":    """SELECT rowid,trade_id,symbol,direction,status,strategy_name,entry_time,entry_price,position_value,broker,ROUND((julianday('now','localtime')-julianday(entry_time))*24,1) AS hours_open FROM trades WHERE status='open' AND entry_time<datetime('now','-8 hours','localtime') ORDER BY entry_time ASC""",
    "Capital Today":   """SELECT timestamp,total_capital,available_cash,invested_value,daily_pnl,total_pnl,note FROM capital WHERE DATE(timestamp)=DATE('now','localtime') ORDER BY timestamp DESC""",
    "Settlement Queue":"""SELECT rowid,* FROM settlement_queue ORDER BY rowid DESC LIMIT 300""",
    "By Strategy":     """SELECT rowid,trade_id,symbol,direction,status,strategy_name,entry_time,exit_time,entry_price,exit_price,pnl,broker FROM trades ORDER BY entry_time DESC LIMIT 500""",
    "Strategies":      """SELECT replace(key,'strategy_','') AS strategy, UPPER(value) AS enabled FROM bot_state WHERE key LIKE 'strategy_%_enabled' ORDER BY strategy""",
    "Daily P&L":       """SELECT trade_date,daily_pnl,daily_pnl_pct,total_trades,winning_trades,losing_trades,win_rate,largest_win,largest_loss,starting_capital,ending_capital FROM daily_summaries ORDER BY trade_date DESC LIMIT 60""",
    "Yesterday Closed":"""SELECT rowid,trade_id,symbol,direction,strategy_name,entry_time,exit_time,entry_price,exit_price,pnl,pnl_pct,exit_reason,broker FROM trades WHERE status='closed' AND DATE(exit_time)=DATE('now','-1 day','localtime') ORDER BY exit_time DESC""",
    "Signal Reviews":  """SELECT rowid,timestamp,symbol,direction,strategy_name,signal_score,decision,confidence,reasoning FROM ai_signal_reviews ORDER BY timestamp DESC LIMIT 200""",
    "This Week Trades":"""SELECT rowid,trade_id,symbol,direction,strategy_name,entry_time,exit_time,entry_price,exit_price,pnl,pnl_pct,exit_reason,broker FROM trades WHERE status='closed' AND DATE(exit_time)>=DATE('now','-7 days','localtime') ORDER BY exit_time DESC""",
    "Strategy P&L":    """SELECT strategy_name,COUNT(*) AS trades,SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS losses,ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS win_rate_pct,ROUND(SUM(pnl),2) AS total_pnl,ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END),2) AS gross_wins,ROUND(ABS(SUM(CASE WHEN pnl<=0 THEN pnl ELSE 0 END)),2) AS gross_losses,ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END)/NULLIF(ABS(SUM(CASE WHEN pnl<=0 THEN pnl ELSE 0 END)),0),3) AS profit_factor FROM trades WHERE status='closed' GROUP BY strategy_name ORDER BY total_pnl DESC""",
    "💰 Financials":   """SELECT label,value,detail FROM (SELECT 'Total Capital' AS label,'$'||printf('%.2f',total_capital) AS value,'Cash: $'||printf('%.2f',available_cash)||'   Invested: $'||printf('%.2f',invested_value) AS detail FROM capital ORDER BY timestamp DESC LIMIT 1) UNION ALL SELECT label,value,detail FROM (SELECT 'Daily P&L (tracker)' AS label,(CASE WHEN daily_pnl>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',daily_pnl) AS value,'Cumulative: '||(CASE WHEN total_pnl>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',total_pnl) AS detail FROM capital ORDER BY timestamp DESC LIMIT 1) UNION ALL SELECT 'In Open Trades',('$'||printf('%.2f',COALESCE(SUM(position_value),0))),(CAST(COUNT(*) AS TEXT)||' open positions') FROM trades WHERE status='open' UNION ALL SELECT 'In Settlement',('$'||printf('%.2f',COALESCE(SUM(amount),0))),(CAST(COUNT(*) AS TEXT)||' unsettled items') FROM settlement_queue WHERE settled=0 UNION ALL SELECT 'Today Closed P&L',((CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0))),(CAST(COUNT(*) AS TEXT)||' trades  '||CAST(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS TEXT)||'W / '||CAST(SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS TEXT)||'L') FROM trades WHERE status='closed' AND DATE(exit_time)=DATE('now','localtime') UNION ALL SELECT 'Yesterday P&L',((CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0))),(CAST(COUNT(*) AS TEXT)||' trades  '||CAST(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS TEXT)||'W / '||CAST(SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS TEXT)||'L') FROM trades WHERE status='closed' AND DATE(exit_time)=DATE('now','-1 day','localtime') UNION ALL SELECT 'This Week P&L',((CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0))),(CAST(COUNT(*) AS TEXT)||' trades') FROM trades WHERE status='closed' AND DATE(exit_time)>=DATE('now','-7 days','localtime') UNION ALL SELECT 'This Month P&L',((CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0))),(CAST(COUNT(*) AS TEXT)||' trades') FROM trades WHERE status='closed' AND strftime('%Y-%m',exit_time)=strftime('%Y-%m','now','localtime')""",
}

def db_quick(label: str):
    sql = _QUICK_SQL.get(label)
    if not sql:
        return {"error": f"Unknown quick query: {label}"}
    try:
        with _db_conn() as c:
            cur  = c.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        return {"label": label, "cols": cols, "rows": rows}
    except Exception as e:
        return {"error": str(e)}

def db_search(data: dict):
    table = data.get("table", "trades")
    try:
        with _db_conn() as c:
            valid = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except Exception as e:
        return {"error": str(e)}
    if table not in valid:
        return {"error": f"Unknown table: {table}"}

    cfg    = _DB_TABLE_SEARCH.get(table, {})
    where, params = [], []

    if cfg.get("strategy") and data.get("strategy"):
        where.append("strategy_name LIKE ?")
        params.append(f"%{data['strategy']}%")
    if cfg.get("status") and data.get("status","any") != "any":
        where.append("status=?"); params.append(data["status"])
    if cfg.get("symbol") and data.get("symbol"):
        where.append("symbol LIKE ?"); params.append(f"%{data['symbol'].upper()}%")
    if cfg.get("broker") and data.get("broker","any") != "any":
        where.append("broker=?"); params.append(data["broker"])

    tcol = cfg.get("time_col")
    tf   = data.get("time","any")
    if tcol and tf != "any":
        if tf == "last 1h":
            where.append(f"{tcol}>=datetime('now','localtime','-1 hour')")
        elif tf == "last 4h":
            where.append(f"{tcol}>=datetime('now','localtime','-4 hours')")
        elif tf == "last 8h":
            where.append(f"{tcol}>=datetime('now','localtime','-8 hours')")
        elif tf == "today":
            where.append(f"DATE({tcol})=DATE('now','localtime')")
        elif tf == "yesterday":
            where.append(f"DATE({tcol})=DATE('now','localtime','-1 day')")
        elif tf == "this week":
            where.append(f"{tcol}>=datetime('now','localtime','-7 days')")

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql    = f"SELECT rowid, * FROM [{table}] {clause} ORDER BY rowid DESC LIMIT 2000"
    try:
        with _db_conn() as c:
            cur  = c.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
        return {"table": table, "cols": cols, "rows": rows}
    except Exception as e:
        return {"error": str(e)}

def db_edit_row(data: dict):
    table    = data.get("table","")
    rowid    = data.get("rowid")
    trade_id = data.get("trade_id")
    col      = data.get("col","")
    new_val  = data.get("value","")
    if not table or not col:
        return False, "Missing table/col"
    if rowid is None and not trade_id:
        return False, "Missing rowid/trade_id"
    try:
        with _db_conn() as c:
            if rowid is not None:
                c.execute(f"UPDATE [{table}] SET [{col}]=? WHERE rowid=?",
                          (new_val, rowid))
            else:
                # Fallback: quick-query views don't return rowid, use trade_id
                c.execute(f"UPDATE [{table}] SET [{col}]=? WHERE trade_id=?",
                          (new_val, trade_id))
            c.commit()
        ref = f"rowid={rowid}" if rowid is not None else f"trade_id={trade_id}"
        return True, f"Updated {table} {ref} {col}={new_val}"
    except Exception as e:
        return False, str(e)

def db_flush_ghost(data: dict):
    trade_id    = data.get("trade_id","")
    entry_price = data.get("entry_price", 0)
    if not trade_id:
        return False, "Missing trade_id"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _db_conn() as c:
            c.execute("""UPDATE trades SET
                           status='closed', exit_time=?, exit_price=?,
                           exit_reason='manual_flush_ghost', pnl=0, pnl_pct=0
                         WHERE trade_id=?""",
                      (now_str, entry_price, trade_id))
            c.commit()
        return True, f"Flushed ghost trade {trade_id}"
    except Exception as e:
        return False, str(e)

def db_strategy_toggle(data: dict):
    key     = data.get("key","")
    enabled = data.get("enabled", True)
    if not key or not key.startswith("strategy_"):
        return False, "Invalid strategy key"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    val     = "true" if enabled else "false"
    try:
        with _db_conn() as c:
            c.execute("UPDATE bot_state SET value=?, updated=? WHERE key=?",
                      (val, now_str, key))
            c.commit()
        return True, f"{'Enabled' if enabled else 'Disabled'} {key}"
    except Exception as e:
        return False, str(e)


def build_snapshot():
    """Top-level status metrics."""
    if db is not None:
        cap = db.get_latest_capital() or {}
        open_trades = db.get_open_trades() or []
        try:
            session = db.get_session(date.today().isoformat()) or {}
        except Exception:
            session = {}
        halted = bool(session.get("trading_halted", 0))
        halt_reason = session.get("halt_reason") or ""
        return {
            "bot_status": "PAUSED" if halted else "ACTIVE",
            "trading_active": not halted,
            "halt_reason": halt_reason,
            "open_positions": len(open_trades),
            "available_cash": float(cap.get("available_cash", 0.0) or 0.0),
            "bot_process_running": is_bot_running(),
            "snapshot_source": "live_db_adapter",
            "db_path": str(DB_PATH),
        }

    try:
        conn = sqlite3.connect(DB_PATH)
 #       conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades WHERE status='open'")
        open_positions = int(cur.fetchone()[0])
        cur.execute("SELECT available_cash FROM capital ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        available_cash = float(row[0]) if row else 0.0
        conn.close()
        return {
            "bot_status": "UNKNOWN",
            "open_positions": open_positions,
            "available_cash": available_cash,
            "bot_process_running": is_bot_running(),
            "snapshot_source": "sqlite_fallback",
            "db_path": str(DB_PATH),
        }
    except Exception:
        return {
            "bot_status": "UNKNOWN",
            "open_positions": 0,
            "available_cash": 0.0,
            "bot_process_running": is_bot_running(),
            "snapshot_source": "error_fallback",
            "db_path": str(DB_PATH),
        }


def load_entry_html(prefer_modular=True):
    modular = HTML_ROOT / "Trading Dashboard.html"
    stage3 = HTML_ROOT / "index.html"
    if prefer_modular and modular.exists():
        return modular.read_bytes(), "modular"
    return stage3.read_bytes(), "stage3"


class Handler(BaseHTTPRequestHandler):
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _session_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith("session="):
                return part[8:]
        return ""

    def _authed(self) -> bool:
        return _valid_session(self._session_token())

    def _current_user(self) -> str:
        return _session_user(self._session_token())

    def _redirect(self, target: str):
        self.send_response(302)
        self.send_header("Location", target)
        self.end_headers()

    def _serve_html(self, content: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # Client disconnected mid-response — harmless

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ── Auth gate ─────────────────────────────────────────────────────────
        _static = path.startswith("/static/") or path.endswith((".css", ".js", ".ico", ".png"))

        if path == "/login":
            return self._serve_html(_LOGIN_PAGE())
        if path == "/setup":
            return self._serve_html(_SETUP_PAGE())
        if path == "/recover":
            return self._serve_html(_RECOVER_PAGE())

        if path == "/api/auth/logout":
            _revoke_session(self._session_token())
            self.send_response(302)
            self.send_header("Set-Cookie", "session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            self.send_header("Location", "/login")
            self.end_headers()
            return

        if path == "/api/auth/users" and self._authed() and _is_admin(self._current_user()):
            users = _load_users()
            return self._json(200, {"users": [
                {"username": u, "role": v.get("role", "user"), "force_change": v.get("force_change", False)}
                for u, v in users.items()
            ]})

        if path == "/api-keys":
            if not self._authed():
                return self._redirect(f"/login?next=/api-keys")
            if not _is_admin(self._current_user()):
                return self._json(403, {"error": "Admin only"})
            return self._serve_html(_api_keys_page())

        if path == "/api/admin/api_keys" and self._authed() and _is_admin(self._current_user()):
            try:
                conn = sqlite3.connect(str(DB_PATH))
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT service, key_name, value_enc, updated_at FROM api_keys ORDER BY service, key_name"
                ).fetchall()
                conn.close()
                keys = []
                for r in rows:
                    try:
                        val = _dec_hash(r["value_enc"]) if r["value_enc"] else ""
                    except Exception:
                        val = "⚠ decrypt failed"
                    keys.append({
                        "service":    r["service"],
                        "key_name":   r["key_name"],
                        "value":      val,
                        "updated_at": r["updated_at"][:19],
                    })
                return self._json(200, {"keys": keys})
            except Exception as e:
                return self._json(500, {"error": str(e)})

        # Redirect to setup if default credentials still active
        if _needs_setup() and path not in ("/setup",) and not _static:
            return self._redirect("/setup")

        if not self._authed() and not _static:
            return self._redirect(f"/login?next={self.path}")
        # ── End auth gate ─────────────────────────────────────────────────────

        if path == "/api/snapshot":
            return self._json(200, build_snapshot())

        if path == "/api/open_positions":
            params = parse_qs(parsed.query)
            sort_by = (params.get("sort_by") or ["opened"])[0]
            sort_dir = (params.get("sort_dir") or ["desc"])[0]
            return self._json(200, {
                "positions": get_open_positions(sort_by, sort_dir),
                "sort_by": sort_by,
                "sort_dir": sort_dir,
                "db_path": str(DB_PATH),
            })

        if path == "/api/chart":
            params = parse_qs(parsed.query)
            symbol    = (params.get("symbol")    or ["BTC/USD"])[0].strip()
            timeframe = (params.get("timeframe") or ["5m"])[0].strip()
            limit     = (params.get("limit")     or ["300"])[0].strip()
            broker    = (params.get("broker")    or [""])[0].strip().lower() or None
            try:
                return self._json(200, get_chart_data(symbol, timeframe, limit, broker))
            except Exception as _ce:
                import logging as _lg
                _lg.getLogger(__name__).error(f"[/api/chart] {symbol} crashed: {_ce}", exc_info=True)
                return self._json(200, {"bars": [], "markers": [], "symbol": symbol, "error": str(_ce)})

        if path == "/api/chart/symbols":
            return self._json(200, {"symbols": get_chart_symbols()})

        if path == "/api/theme":
            _theme_path = Path(__file__).parent / "config" / "theme.json"
            try:
                theme = json.loads(_theme_path.read_text()) if _theme_path.exists() else {}
            except Exception:
                theme = {}
            return self._json(200, {"theme": theme})

        if path == "/api/tradovate_status":
            if not _tradovate_available:
                return self._json(200, {"available": False, "connected": False})
            connected   = bool(_tradovate_feed and _tradovate_feed.is_connected())
            auth_failed = bool(_tradovate_feed and _tradovate_feed.is_auth_failed())
            running     = bool(_tradovate_feed and _tradovate_feed._running)
            bar_counts  = {}
            if _tradovate_feed:
                from core.tradovate_feed import _bar_cache, _cache_lock
                with _cache_lock:
                    bar_counts = {k: len(v) for k, v in _bar_cache.items()}
            return self._json(200, {
                "available":   True,
                "connected":   connected,
                "running":     running,
                "auth_failed": auth_failed,   # frontend can show "token expired" message
                "bar_counts":  bar_counts,
            })

        if path == "/api/candles":
            params = parse_qs(parsed.query)
            symbol = (params.get("symbol") or [""])[0].strip().upper()
            asset_class = (params.get("asset_class") or params.get("asset") or ["crypto"])[0].lower()
            timeframe = (params.get("timeframe") or ["5 Min"])[0]
            limit = (params.get("limit") or ["100"])[0]
            if not symbol:
                return self._json(400, {"error": "symbol is required"})
            return self._json(200, {"candles": get_candles(symbol, asset_class, timeframe, limit)})

        if path == "/api/injected_symbols":
            return self._json(200, get_injected_symbols())

        if path == "/api/trade_log/today":
            return self._json(200, {"trades": get_today_trade_log(), "db_path": str(DB_PATH)})

        if path == "/api/overview":
            return self._json(200, get_overview_data())

        if path == "/api/quick_status":
            params = parse_qs(parsed.query)
            year = (params.get("year") or [date.today().year])[0]
            return self._json(200, get_quick_status(year))

        # ── Database browser API ──────────────────────────────────────────
        if path == "/api/db/tables":
            return self._json(200, db_tables())

        if path == "/api/db/browse":
            params = parse_qs(parsed.query)
            table  = (params.get("table") or ["trades"])[0]
            limit  = int((params.get("limit")  or ["500"])[0])
            offset = int((params.get("offset") or ["0"])[0])
            return self._json(200, db_browse(table, limit, offset))

        if path == "/api/db/quick":
            params = parse_qs(parsed.query)
            label  = (params.get("q") or [""])[0]
            return self._json(200, db_quick(label))

        if path == "/api/manual_trade/config":
            return self._json(200, get_manual_trade_config())

        if path == "/api/backtest/config":
            return self._json(200, get_backtest_config())

        if path == "/api/optimize/params":
            from urllib.parse import parse_qs as _pqs
            _qs = _pqs(parsed.query)
            strat = (_qs.get("strategy", [""])[0] or "").strip()
            if not strat:
                return self._json(400, {"ok": False, "error": "strategy required"})
            p = get_strategy_optimizer_params(strat)
            return self._json(200, {"ok": True, "strategy": strat, "params": p})

        if path == "/api/optimize/progress":
            _is_done = (not _optimizer_progress["running"]
                        and _optimizer_progress["results"] is not None)
            # Zombie state: not running, no results, no error, no active process
            # (happens when worker was killed mid-run).  Treat as done so the UI
            # stops polling and lets the user start a new run.
            _is_zombie = (not _optimizer_progress["running"]
                          and not _opt_running.is_set()
                          and _optimizer_progress["results"] is None
                          and not _optimizer_progress.get("error"))
            return self._json(200, {**_optimizer_progress,
                                    "cancel_pending": _opt_cancel.is_set(),
                                    "done": _is_done or _is_zombie,
                                    "zombie": _is_zombie})

        if path == "/api/candle_store":
            try:
                from intelligence.candle_store import get_store as _get_cs
                rows = _get_cs().list_available()
                return self._json(200, {"ok": True, "entries": rows})
            except Exception as _e:
                return self._json(200, {"ok": False, "error": str(_e), "entries": []})

        if path == "/api/manual_trade/preview":
            ok, msg, details = get_manual_trade_preview(parse_qs(parsed.query))
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **details})

        if path == "/" or path == "/index.html":
            page, mode = load_entry_html(prefer_modular=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("X-Dashboard-Mode", mode)
            self._cors_headers()
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        if path == "/stage3":
            page, _ = load_entry_html(prefer_modular=False)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors_headers()
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        rel_path = path.lstrip("/")
        if rel_path.startswith("static/"):
            rel_path = rel_path[len("static/"):]
        if rel_path:
            file_path = HTML_ROOT / rel_path
            if file_path.exists() and file_path.is_file():
                content = file_path.read_bytes()
                self.send_response(200)
                if file_path.suffix in (".jsx", ".js"):
                    mime = "text/javascript"
                elif file_path.suffix == ".css":
                    mime = "text/css"
                elif file_path.suffix == ".html":
                    mime = "text/html; charset=utf-8"
                else:
                    mime = "application/octet-stream"
                self.send_header("Content-Type", mime)
                self._cors_headers()
                self.send_header("Content-Length", str(len(content)))
                if file_path.suffix in (".jsx", ".js"):
                    self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
                return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        data = json.loads(body_raw or "{}")

        global _tradovate_feed, _tradovate_available

        # ── Tradovate debug (no auth needed) ─────────────────────────────────
        if path == "/api/tradovate_debug":
            from core.tradovate_feed import _bar_cache, _cache_lock
            snap = {}
            if _tradovate_feed is not None:
                with _cache_lock:
                    for k, q in _bar_cache.items():
                        bars = list(q)
                        snap[k] = {"count": len(bars), "last_bar": bars[-1] if bars else None}
                return self._json(200, {
                    "connected":    _tradovate_feed.is_connected(),
                    "auth_failed":  _tradovate_feed.is_auth_failed(),
                    "running":      _tradovate_feed._running,
                    "ws_url":       _tradovate_feed._ws_url,
                    "subscriptions": list(_tradovate_feed._subscriptions.keys()),
                    "chart_id_map": {str(k): v for k, v in _tradovate_feed._chart_id_map.items()},
                    "cache": snap,
                })
            return self._json(200, {"status": "no_feed"})

        # ── Theme persistence ─────────────────────────────────────────────────
        if path == "/api/theme":
            _theme_path = Path(__file__).parent / "config" / "theme.json"
            theme = data.get("theme")
            if not isinstance(theme, dict):
                return self._json(400, {"ok": False, "error": "theme must be an object"})
            try:
                _theme_path.parent.mkdir(parents=True, exist_ok=True)
                _theme_path.write_text(json.dumps(theme, indent=2))
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        # ── Tradovate real-time feed token ────────────────────────────────────
        if path == "/api/tradovate_token":
            if not _tradovate_available:
                return self._json(503, {"ok": False, "error": "tradovate_feed module not available"})
            token = data.get("token", "").strip()
            if not token:
                return self._json(400, {"ok": False, "error": "token is required"})

            # ── Auto-detect live vs demo from JWT payload ─────────────────────
            # JWT middle segment (payload) is base64url-encoded JSON.
            # envEndpoint.marketData tells us exactly which server to use.
            def _detect_use_live(jwt: str) -> bool:
                try:
                    import base64, json as _json
                    raw = jwt[jwt.index("eyJ"):]          # strip any Bearer prefix
                    parts = raw.split(".")
                    if len(parts) < 2:
                        return False
                    pad = parts[1] + "=="                  # fix base64 padding
                    payload = _json.loads(base64.urlsafe_b64decode(pad))
                    md_url = (payload.get("envEndpoint") or {}).get("marketData", "")
                    return "md-live" in md_url             # live account → live WS
                except Exception:
                    return False                           # default to demo

            _use_live = _detect_use_live(token)
            import logging as _lg2
            _lg2.getLogger(__name__).warning(
                "Tradovate token inject: use_live=%s", _use_live
            )

            if _tradovate_feed is None:
                from core.tradovate_feed import init_feed as _tv_init_feed
                # init_feed pre-subscribes MESM6 + MNQM6 (1/3/5m each)
                _tradovate_feed = _tv_init_feed(token=token, use_live=_use_live)
            else:
                # If the user switched accounts (demo↔live), recreate the feed
                # with the correct WS URL rather than reusing the old one
                from core.tradovate_feed import WS_URL_LIVE, WS_URL_DEMO
                _needed_url = WS_URL_LIVE if _use_live else WS_URL_DEMO
                if _tradovate_feed._ws_url != _needed_url:
                    # Different endpoint — spin up a fresh feed instance
                    _tradovate_feed.stop()
                    import time as _time; _time.sleep(0.3)
                    from core.tradovate_feed import init_feed as _tv_init_feed2
                    _tradovate_feed = _tv_init_feed2(token=token, use_live=_use_live)
                else:
                    # Same endpoint — reset auth and restart
                    _tradovate_feed.reset_auth(token)

                    # Ensure MNQM6 subscriptions exist
                    for _mnq_name, _mnq_es in [("MNQM6", 5), ("MNQM6_1", 1), ("MNQM6_3", 3)]:
                        if _mnq_name not in _tradovate_feed._subscriptions:
                            _tradovate_feed.subscribe(_mnq_name, symbol_id="MNQM6",
                                                      element_size=_mnq_es)

                    # Stop and restart so new token takes effect
                    _tradovate_feed.stop()
                    import time as _time; _time.sleep(0.3)
                    _tradovate_feed._authenticated = False
                    _tradovate_feed.start()

            # Pre-resolve contract IDs for all symbols in the map (background thread)
            # so the first chart request always has a numeric ID ready.
            def _prefetch_contract_ids(feed, sym_map, id_cache):
                import logging as _lg
                _log = _lg.getLogger(__name__)
                for root, contract_name in sym_map.items():
                    if contract_name not in id_cache:
                        cid = feed.lookup_symbol_id(contract_name)
                        if cid:
                            id_cache[contract_name] = cid
                            _log.info("Tradovate pre-fetch: %s → id=%d", contract_name, cid)
                        else:
                            _log.warning("Tradovate pre-fetch: could not resolve %s", contract_name)
            import threading as _threading
            _threading.Thread(
                target=_prefetch_contract_ids,
                args=(_tradovate_feed, TRADOVATE_SYMBOL_MAP, TRADOVATE_SYMBOL_IDS),
                name="tv-prefetch-ids",
                daemon=True,
            ).start()

            return self._json(200, {"ok": True, "message": "Tradovate feed started"})

        if path == "/api/tradovate_stop":
            # Called by the frontend when the indicator is toggled off or the
            # futures chart tab is closed — prevents zombie reconnect loops.
            if _tradovate_feed is not None:
                _tradovate_feed.stop()
                import logging as _lg; _lg.getLogger(__name__).info("TradovateFeed: stopped via /api/tradovate_stop")
            return self._json(200, {"ok": True, "message": "Tradovate feed stopped"})

        # ── Auth endpoints (no session required) ──────────────────────────────
        if path == "/api/auth/setup":
            # Only allowed while default admin/admin is still active
            if not _needs_setup():
                return self._json(403, {"ok": False, "message": "Setup already complete."})
            admin_pass = data.get("admin_password", "")
            username   = data.get("username", "").strip()
            password   = data.get("password", "")
            if len(admin_pass) < 8:
                return self._json(400, {"ok": False, "message": "Admin password must be at least 8 characters."})
            if not username or username.lower() == _ADMIN_DEFAULT:
                return self._json(400, {"ok": False, "message": "Choose a username other than 'admin'."})
            if len(password) < 8:
                return self._json(400, {"ok": False, "message": "Your password must be at least 8 characters."})
            users = _load_users()
            users[_ADMIN_DEFAULT] = {"hash": _hash_password(admin_pass), "role": "admin", "force_change": False}
            users[username]       = {"hash": _hash_password(password),   "role": "admin", "force_change": False}
            _save_users(users)
            token = _new_session(username)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie",
                f"session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}")
            body = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            import logging as _lg
            _lg.getLogger("dashboard.auth").info(
                f"[AUTH] Setup complete. Admin password changed. User created: {username!r}")
            return

        if path == "/api/auth/login":
            username = data.get("username", "").strip()
            password = data.get("password", "")
            if _needs_setup():
                return self._json(403, {"ok": False, "message": "Complete first-time setup first."})
            if _auth_check(username, password):
                token = _new_session(username)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie",
                    f"session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}")
                body = json.dumps({"ok": True}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json(401, {"ok": False, "message": "Invalid credentials"})
            return

        if path == "/api/auth/recover":
            tok_in   = data.get("token", "").strip()
            password = data.get("password", "")
            if not secrets.compare_digest(tok_in, _RECOVERY_TOKEN):
                return self._json(401, {"ok": False, "message": "Invalid or expired recovery token."})
            if len(password) < 8:
                return self._json(400, {"ok": False, "message": "Password must be at least 8 characters."})
            users = _load_users()
            if not users:
                users = {}
            users[_ADMIN_DEFAULT] = {"hash": _hash_password(password), "role": "admin", "force_change": False}
            _save_users(users)
            import logging as _lg
            _lg.getLogger("dashboard.auth").warning("[AUTH] Admin password reset via recovery token.")
            return self._json(200, {"ok": True})

        if path == "/api/auth/change_password":
            if not self._authed():
                return self._json(401, {"error": "Unauthorized"})
            username = data.get("username", "").strip() or self._current_user()
            old_pass = data.get("old_password", "")
            new_pass = data.get("new_password", "")
            if not _auth_check(username, old_pass):
                return self._json(401, {"ok": False, "message": "Current password incorrect."})
            if len(new_pass) < 8:
                return self._json(400, {"ok": False, "message": "New password must be at least 8 characters."})
            users = _load_users()
            users[username]["hash"]         = _hash_password(new_pass)
            users[username]["force_change"] = False
            _save_users(users)
            return self._json(200, {"ok": True})

        # ── Admin: reset any user's password ──────────────────────────────────
        if path == "/api/auth/admin/reset_password":
            if not self._authed():
                return self._json(401, {"error": "Unauthorized"})
            if not _is_admin(self._current_user()):
                return self._json(403, {"error": "Admin only"})
            target   = data.get("username", "").strip()
            new_pass = data.get("new_password", "")
            users    = _load_users()
            if target not in users:
                return self._json(404, {"ok": False, "message": f"User '{target}' not found."})
            if len(new_pass) < 8:
                return self._json(400, {"ok": False, "message": "New password must be at least 8 characters."})
            users[target]["hash"]         = _hash_password(new_pass)
            users[target]["force_change"] = True   # force them to change on next login
            _save_users(users)
            import logging as _lg
            _lg.getLogger("dashboard.auth").info(
                f"[AUTH] Admin {self._current_user()!r} reset password for {target!r}")
            return self._json(200, {"ok": True})

        # ── Admin: add user ────────────────────────────────────────────────────
        if path == "/api/auth/admin/add_user":
            if not self._authed():
                return self._json(401, {"error": "Unauthorized"})
            if not _is_admin(self._current_user()):
                return self._json(403, {"error": "Admin only"})
            username = data.get("username", "").strip()
            password = data.get("password", "")
            role     = data.get("role", "user")
            if not username:
                return self._json(400, {"ok": False, "message": "Username cannot be empty."})
            if len(password) < 8:
                return self._json(400, {"ok": False, "message": "Password must be at least 8 characters."})
            users = _load_users()
            if username in users:
                return self._json(409, {"ok": False, "message": f"User '{username}' already exists."})
            users[username] = {"hash": _hash_password(password), "role": role, "force_change": True}
            _save_users(users)
            return self._json(200, {"ok": True})

        # ── Admin: delete user ─────────────────────────────────────────────────
        if path == "/api/auth/admin/delete_user":
            if not self._authed():
                return self._json(401, {"error": "Unauthorized"})
            if not _is_admin(self._current_user()):
                return self._json(403, {"error": "Admin only"})
            username = data.get("username", "").strip()
            if username == _ADMIN_DEFAULT:
                return self._json(400, {"ok": False, "message": "Cannot delete the admin account."})
            if username == self._current_user():
                return self._json(400, {"ok": False, "message": "Cannot delete your own account."})
            users = _load_users()
            if username not in users:
                return self._json(404, {"ok": False, "message": f"User '{username}' not found."})
            del users[username]
            _save_users(users)
            return self._json(200, {"ok": True})

        # ── Admin: set / update an API key ────────────────────────────────────
        if path == "/api/admin/api_keys/set":
            if not self._authed() or not _is_admin(self._current_user()):
                return self._json(403, {"error": "Admin only"})
            service  = data.get("service",  "").strip().lower()
            key_name = data.get("key_name", "").strip().lower()
            value    = data.get("value",    "").strip()
            if not service or not key_name:
                return self._json(400, {"ok": False, "message": "service and key_name are required"})
            if not value:
                return self._json(400, {"ok": False, "message": "value cannot be empty"})
            try:
                f   = _get_fernet()
                enc = f.encrypt(value.encode()).decode() if f else "plain:" + value
                now = datetime.now(timezone.utc).isoformat()
                conn = sqlite3.connect(str(DB_PATH))
                conn.execute("""
                    INSERT INTO api_keys (service, key_name, value_enc, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(service, key_name) DO UPDATE SET
                        value_enc=excluded.value_enc, updated_at=excluded.updated_at
                """, (service, key_name, enc, now))
                conn.commit(); conn.close()
                import logging as _lg
                _lg.getLogger("dashboard.auth").info(
                    f"[API_KEYS] {self._current_user()!r} set {service}/{key_name}")
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "message": str(e)})

        # ── Admin: delete an API key ───────────────────────────────────────────
        if path == "/api/admin/api_keys/delete":
            if not self._authed() or not _is_admin(self._current_user()):
                return self._json(403, {"error": "Admin only"})
            service  = data.get("service",  "").strip().lower()
            key_name = data.get("key_name", "").strip().lower()
            if not service or not key_name:
                return self._json(400, {"ok": False, "message": "service and key_name are required"})
            conn = sqlite3.connect(str(DB_PATH))
            cur  = conn.execute("DELETE FROM api_keys WHERE service=? AND key_name=?", (service, key_name))
            conn.commit(); conn.close()
            if cur.rowcount:
                return self._json(200, {"ok": True, "message": f"{service}/{key_name} deleted — will fall back to .env"})
            return self._json(404, {"ok": False, "message": "Key not found in DB"})

        # ── Candle Store routes ───────────────────────────────────────────────────
        if path == "/api/candle_store/fetch":
            # Fetch candles from yfinance and save to persistent store.
            # Payload: {symbols: ["BTC/USD", ...], timeframe: "1h", days: 365, asset_class: "crypto"}
            symbols     = data.get("symbols") or []
            timeframe   = data.get("timeframe", "1h")
            days        = int(data.get("days", 365))
            asset_class = data.get("asset_class", "crypto")
            if not symbols:
                return self._json(400, {"ok": False, "error": "No symbols provided"})

            def _do_fetch():
                from intelligence.backtester import Backtester
                from intelligence.candle_store import get_store as _gcs
                bt    = Backtester()
                store = _gcs()
                results = []
                for sym in symbols:
                    try:
                        if asset_class == "crypto":
                            df = bt.fetch_history_crypto(sym, days=days, timeframe=timeframe)
                        else:
                            df = bt.fetch_history(sym, days=days, timeframe=timeframe)
                        if df is not None and not df.empty:
                            n = store.save(sym, timeframe, df)
                            results.append({"symbol": sym, "ok": True, "bars": n})
                        else:
                            results.append({"symbol": sym, "ok": False, "error": "No data returned"})
                    except Exception as _fe:
                        results.append({"symbol": sym, "ok": False, "error": str(_fe)})
                return results

            try:
                fetch_results = _do_fetch()
                return self._json(200, {"ok": True, "results": fetch_results})
            except Exception as _e:
                return self._json(500, {"ok": False, "error": str(_e)})

        if path == "/api/candle_store/delete":
            # Delete cached data for one symbol/timeframe.
            # Payload: {symbol: "BTC/USD", timeframe: "1h"}
            symbol    = data.get("symbol", "")
            timeframe = data.get("timeframe", "1h")
            if not symbol:
                return self._json(400, {"ok": False, "error": "No symbol provided"})
            try:
                from intelligence.candle_store import get_store as _gcs
                n = _gcs().delete(symbol, timeframe)
                return self._json(200, {"ok": True, "deleted_bars": n,
                                        "message": f"Cleared {symbol}/{timeframe} ({n:,} bars)"})
            except Exception as _e:
                return self._json(500, {"ok": False, "error": str(_e)})

        # ── Optimizer routes (no separate auth needed — dashboard access implies auth) ──
        if path == "/api/optimize/cancel":
            was_running = _opt_running.is_set()
            _opt_cancel.set()
            # Kill the subprocess if one is running
            if _opt_proc is not None:
                try:
                    _opt_proc.terminate()
                except Exception:
                    pass
            return self._json(200, {"ok": True, "was_running": was_running,
                                    "message": "Cancel signal sent to optimizer"})

        if path == "/api/optimize":
            _opt_cancel.clear()   # reset cancel flag so new run starts clean
            if _opt_running.is_set():
                return self._json(200, {"ok": False, "error": "Optimizer already running — cancel first", "results": []})
            _optimizer_progress.update({"running": False, "iteration": 0, "total": 0,
                                        "best_score": None, "cancelled": False,
                                        "results": None, "error": None})
            t = threading.Thread(target=run_optimize_api, args=(data,), daemon=True)
            t.start()
            return self._json(200, {"ok": True, "status": "started"})

        # ── Auth gate for all other POSTs ─────────────────────────────────────
        if not self._authed():
            return self._json(401, {"error": "Unauthorized"})
        # ── End auth gate ─────────────────────────────────────────────────────

        if path == "/api/trades/manual":
            ok, msg, details = open_manual_trade(data)
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **details})

        if path == "/api/injected_symbols":
            ok, msg, symbols = inject_symbols(
                str(data.get("kind") or "").lower(),
                data.get("symbols"),
                replace=bool(data.get("replace", False)),
            )
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **symbols})

        if path == "/api/injected_symbols/clear":
            return self._json(200, {"ok": True, "message": "Injected symbols cleared.", **clear_injected_symbols()})

        if path == "/api/report/daily":
            ok, msg, details = generate_daily_report()
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **details})

        if path == "/api/tax/export":
            ok, msg, details = export_tax_csv(data.get("year"))
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **details})

        if path == "/api/withdrawal":
            ok, msg, details = process_withdrawal(data.get("amount"), data.get("reason"))
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **details})

        if path == "/api/ml/retrain":
            ok, msg, details = retrain_ml()
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **details})

        if path == "/api/backtest/cancel":
            running = _bt_running.is_set()
            _bt_cancel.set()
            try:
                from intelligence.backtester import request_cancel
                request_cancel()
            except Exception:
                pass
            msg = "Cancel signal sent — waiting for next backtest checkpoint." if running else "Cancel signal set; no active backtest was marked running."
            return self._json(200, {"ok": True, "running": running, "message": msg})

        if path == "/api/chronos_predict":
            return self._json(200, _chronos_predict(data))

        if path == "/api/backtest":
            ok, msg, details = run_backtest_api(data)
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **details})

        if path == "/api/chat":
            ok, msg, details = chat_with_bot(data.get("message"))
            return self._json(200 if ok else 400, {"ok": ok, "message": msg, **details})

        # ── Database browser POST actions ─────────────────────────────────────
        if path in ("/api/db/search", "/api/db/edit_row",
                    "/api/db/flush_ghost", "/api/db/strategy_toggle"):
            if path == "/api/db/search":
                return self._json(200, db_search(data))
            if path == "/api/db/edit_row":
                ok, msg = db_edit_row(data)
                return self._json(200 if ok else 400, {"ok": ok, "message": msg})
            if path == "/api/db/flush_ghost":
                ok, msg = db_flush_ghost(data)
                return self._json(200 if ok else 400, {"ok": ok, "message": msg})
            if path == "/api/db/strategy_toggle":
                ok, msg = db_strategy_toggle(data)
                return self._json(200 if ok else 400, {"ok": ok, "message": msg})

        if path != "/api/control":
            return self._json(404, {"error": "not found"})

        action = data.get("action")

        if action == "pause_trading":
            if risk_manager is not None:
                risk_manager.manual_halt("Paused via HTML dashboard")
                return self._json(200, {"ok": True, "message": "Trading paused."})
            return self._json(202, {"ok": False, "message": "Risk manager unavailable in fallback mode."})

        if action == "resume_trading":
            if risk_manager is not None:
                risk_manager.manual_resume()
                return self._json(200, {"ok": True, "message": "Trading resumed."})
            return self._json(202, {"ok": False, "message": "Risk manager unavailable in fallback mode."})

        if action == "start_bot":
            ok, msg = start_bot_process()
            return self._json(200 if ok else 400, {"ok": ok, "message": msg})

        if action == "stop_bot":
            ok, msg = stop_bot_process()
            return self._json(200 if ok else 400, {"ok": ok, "message": msg})

        if action == "restart_bot":
            stop_bot_process()
            # light delay to reduce duplicate-start races
            try:
                import time
                time.sleep(2)
            except Exception:
                pass
            if is_bot_running():
                return self._json(400, {"ok": False, "message": "Bot still running after stop; restart aborted."})
            ok, msg = start_bot_process()
            return self._json(200 if ok else 400, {"ok": ok, "message": f"Restart requested. {msg}"})

        if action == "open_db_panel":
            try:
                subprocess.Popen(
                    [str(BOT_PYTHON), str(ROOT / "db_panel.py")],
                    cwd=str(ROOT),
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
                return self._json(200, {"ok": True, "message": "DB Panel launched."})
            except Exception as e:
                return self._json(500, {"ok": False, "message": str(e)})

        if action == "refresh_scan":
            return self._json(200, {"ok": True, "message": "Scan refresh request received."})

        if action == "open_terminal":
            ok, msg = open_project_terminal()
            return self._json(200 if ok else 400, {"ok": ok, "message": msg})

        if action == "close_position":
            position_id = data.get("position_id")
            if position_id is None:
                return self._json(400, {"ok": False, "message": "position_id is required"})
            ok, msg = request_close_position(position_id)
            return self._json(202 if ok else 400, {"ok": ok, "message": msg})

        return self._json(400, {"error": f"Unknown action: {action}"})


if __name__ == "__main__":
    # Seed default admin/admin on very first start
    _ensure_admin_seeded()

    print(f"Starting dashboard on http://localhost:{WEB_DASHBOARD_PORT}", flush=True)
    print(f"DB: {DB_PATH}", flush=True)
    print("=" * 60, flush=True)

    if _needs_setup():
        print("  FIRST-TIME SETUP REQUIRED", flush=True)
        print(f"  Default credentials:  admin / admin", flush=True)
        print(f"  Visit http://localhost:{WEB_DASHBOARD_PORT}/setup to get started.", flush=True)
    else:
        users = _load_users()
        print(f"  Auth enabled — accounts: {list(users.keys())}", flush=True)

    print(f"  Recovery token : {_RECOVERY_TOKEN}", flush=True)
    print(f"  Locked out?    : http://localhost:{WEB_DASHBOARD_PORT}/recover", flush=True)
    print("=" * 60, flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", WEB_DASHBOARD_PORT), Handler)
    server.serve_forever()
