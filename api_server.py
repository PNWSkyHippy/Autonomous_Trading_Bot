"""
api_server.py
=============
Lightweight FastAPI server that runs alongside bot_engine.py.
Exposes /api/breakout_signal for BreakoutScanner to POST signals to.

Start with:
    python api_server.py

Or integrate into bot_engine.py startup (see bottom of this file).

Endpoints:
    POST /api/breakout_signal   — receive scanner breakout signal
    GET  /api/health            — health check
    GET  /api/open_positions    — open positions (for HTML dashboard)
    POST /api/control           — bot control commands
"""

import hashlib
import hmac
import logging
import os
import threading
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
from core.breakout_receiver import breakout_receiver
from data.database import db

logger = logging.getLogger(__name__)

app = FastAPI(title="Trading Bot API", version="2.0")

# Allow dashboard and scanner to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Key auth ────────────────────────────────────────────────────────────

API_KEY = getattr(config, "BOT_API_KEY", os.getenv("BOT_API_KEY", ""))


def verify_api_key(x_api_key: str = Header(None)) -> bool:
    if not API_KEY:
        logger.warning("BOT_API_KEY not set — all requests accepted (dev mode)")
        return True
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")
    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(status_code=403, detail="Invalid API key")
    return True


# ── Request models ──────────────────────────────────────────────────────────

class BreakoutSignalPayload(BaseModel):
    symbol:          str
    broker:          str
    direction:       str                # "long" | "short"
    asset_class:     str                # "crypto" | "stock"
    entry_price:     float
    current_price:   float
    source_price:    Optional[float]    = None
    move_pct:        float
    best_move_pct:   Optional[float]    = None
    escalation:      int                = 0
    volume_spike:    Optional[float]    = None
    momentum_score:  Optional[float]    = None
    confidence:      Optional[float]    = None
    candle_open:     Optional[float]    = None
    candle_high:     Optional[float]    = None
    candle_low:      Optional[float]    = None
    candle_close:    Optional[float]    = None
    rsi:             Optional[float]    = None
    sma200:          Optional[float]    = None
    structural_stop_price: Optional[float] = None
    market_pct_change:     Optional[float] = None
    pattern:         Optional[str]      = None
    timestamp:       Optional[str]      = None
    source_timestamp: Optional[str]     = None
    signal_source:   Optional[str]      = None
    source_broker:   Optional[str]      = None
    bypass_win_cooldown: Optional[bool] = None
    breakout_level:  Optional[float]    = None
    bars_since_breakout: Optional[int]  = None
    distance_from_breakout_pct: Optional[float] = None


class ControlPayload(BaseModel):
    command: str
    value:   Optional[str] = None


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status":    "ok",
        "timestamp": datetime.now().isoformat(),
        "trading":   not db.get_session().get("trading_halted", False),
    }


@app.post("/api/breakout_signal")
def receive_breakout_signal(
    payload: BreakoutSignalPayload,
    x_api_key: str = Header(None),
):
    verify_api_key(x_api_key)

    # Only accept escalation level 2+ (>5% move) to avoid noise
    MIN_ESCALATION = getattr(config, "BREAKOUT_MIN_ESCALATION", 2)
    if payload.escalation < MIN_ESCALATION:
        return {
            "accepted": False,
            "reason":   f"Escalation {payload.escalation} below minimum {MIN_ESCALATION}",
            "trade_id": None,
        }

    result = breakout_receiver.receive_signal(payload.dict())
    return result


@app.get("/api/open_positions")
def get_open_positions(x_api_key: str = Header(None)):
    verify_api_key(x_api_key)
    trades = db.get_open_trades()
    # Augment each position with per-trade state keys for dashboard indicators:
    #   tp_ext_count  — how many times the momentum rider extended the take-profit
    #   sl_raise_count — how many times the trailing stop was ratcheted up
    for trade in trades:
        tid = trade.get("trade_id", "")
        if tid:
            try:
                # prefer bot_state momentum_ext (live count); fall back to trades.tp_hit_count
                ext = int(db.get_state(f"momentum_ext_{tid}", default=0) or 0)
                trade["tp_ext_count"]   = ext or int(trade.get("tp_hit_count") or 0)
                trade["sl_raise_count"] = int(db.get_state(f"sl_raise_count_{tid}", default=0) or 0)
            except Exception:
                trade["tp_ext_count"]   = 0
                trade["sl_raise_count"] = 0
        else:
            trade["tp_ext_count"]   = 0
            trade["sl_raise_count"] = 0
    return {"positions": trades, "count": len(trades)}


@app.post("/api/control")
def control(payload: ControlPayload, x_api_key: str = Header(None)):
    verify_api_key(x_api_key)
    cmd = payload.command.lower().strip()

    if cmd == "halt":
        today = datetime.now().date().isoformat()
        db.update_session(today, trading_halted=1, halt_reason="API command")
        return {"ok": True, "message": "Trading halted"}

    if cmd == "resume":
        today = datetime.now().date().isoformat()
        db.update_session(today, trading_halted=0, halt_reason=None, consecutive_losses=0)
        return {"ok": True, "message": "Trading resumed"}

    if cmd == "status":
        session = db.get_session()
        return {"ok": True, "session": session}

    return {"ok": False, "message": f"Unknown command: {cmd}"}


# ── Standalone startup ───────────────────────────────────────────────────────

def start_api_server(host: str = "0.0.0.0", port: int = 8181):
    """
    Start the API server in a background thread.
    Call this from bot_engine.py after initializing components.

    Example in bot_engine.py __init__:
        from api_server import start_api_server
        from core.breakout_receiver import breakout_receiver
        breakout_receiver.set_executor(self.trade_executor)
        breakout_receiver.set_scanner(self.market_scanner)
        start_api_server(port=8181)
    """
    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info(f"[API SERVER] Breakout signal receiver running on {host}:{port}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8181, reload=False)
