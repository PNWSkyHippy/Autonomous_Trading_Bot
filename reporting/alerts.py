"""
=============================================================
  ALERTS MODULE
  Sends SMS (via Twilio or email-to-SMS) and email alerts for:
  - Trade opened
  - Trade closed (win or loss)
  - Trading halted
  - Daily summary
  - Manual price alerts (for your personal SOXS/SOXL trades)

  SMS OPTIONS (choose one):
  1. Email-to-SMS gateway (FREE — uses your email):
     AT&T:      number@txt.att.net
     T-Mobile:  number@tmomail.net
     Verizon:   number@vtext.com
     Sprint:    number@messaging.sprintpcs.com

  2. Twilio (paid but reliable, ~$0.0075/SMS):
     Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, TWILIO_TO in .env

  Configure in .env:
    EMAIL_SENDER=your_gmail@gmail.com
    EMAIL_PASSWORD=your_app_password  (Gmail App Password, not account password)
    EMAIL_RECIPIENT=your_email@gmail.com
    SMS_RECIPIENT=5095551234@vtext.com  (email-to-SMS gateway)
    ALERT_ON_TRADE_OPEN=true
    ALERT_ON_TRADE_CLOSE=true
    ALERT_ON_HALT=true
    ALERT_MIN_PNL=0  (only alert on closes above this $ amount, 0 = all)
=============================================================
"""

import logging
import os
import smtplib
from collections import deque
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from threading import Lock
from typing import Dict, List, Optional

import config
import sys
# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)

logger = logging.getLogger(__name__)


class AlertManager:
    """
    Centralized alert system for the trading bot.
    Supports email and SMS (via email-to-SMS gateway or Twilio).
    """

    def __init__(self):
        self.email_sender    = config.EMAIL_SENDER
        self.email_password  = config.EMAIL_PASSWORD
        self.email_recipient = config.EMAIL_RECIPIENT
        self.sms_recipient   = os.getenv("SMS_RECIPIENT", "")

        self.alert_on_open   = os.getenv("ALERT_ON_TRADE_OPEN",  "true").lower() == "true"
        self.alert_on_close  = os.getenv("ALERT_ON_TRADE_CLOSE", "true").lower() == "true"
        self.alert_on_halt   = os.getenv("ALERT_ON_HALT",        "true").lower() == "true"
        self.alert_min_pnl   = float(os.getenv("ALERT_MIN_PNL",  "0"))

        # ── Digest queue ─────────────────────────────────────────────────────
        # Trade open/close alerts are batched and sent as a single digest email
        # every ALERT_DIGEST_HOURS hours instead of one email per trade.
        # Errors and halts always bypass the queue and send immediately.
        # Set ALERT_DIGEST_HOURS = 0 in config.py to disable batching (old behavior).
        self._digest_hours   = getattr(config, "ALERT_DIGEST_HOURS", 1)
        self._queue: deque   = deque()   # list of (timestamp, kind, subject, body, sms)
        self._queue_lock     = Lock()
        self._last_flush     = datetime.now(timezone.utc)  # track last digest send time

        self._email_configured = bool(
            self.email_sender and self.email_password and self.email_recipient
        )
        self._sms_configured = bool(self.sms_recipient and self._email_configured)

        if self._email_configured:
            logger.info(
                f"Alerts: email configured → {self.email_recipient} "
                f"| digest every {self._digest_hours}hr(s) "
                f"({'batching disabled' if self._digest_hours == 0 else 'errors send immediately'})"
            )
        if self._sms_configured:
            logger.info(f"Alerts: SMS configured → {self.sms_recipient}")
        if not self._email_configured:
            logger.info(
                "Alerts: not configured. Add EMAIL_SENDER, EMAIL_PASSWORD, "
                "EMAIL_RECIPIENT to .env to enable."
            )

    # ----------------------------------------------------------
    #  TRADE ALERTS
    # ----------------------------------------------------------

    def trade_opened(self, trade: Dict):
        """Queue alert when a new trade opens (sent in next digest)."""
        if not self.alert_on_open or not self._email_configured:
            return
        symbol    = trade.get("symbol", "?")
        direction = trade.get("direction", "?").upper()
        entry     = trade.get("entry_price", 0)
        sl        = trade.get("stop_loss", 0)
        tp        = trade.get("take_profit", 0)
        value     = trade.get("position_value", 0)
        strategy  = trade.get("strategy_name", "unknown")
        ts        = datetime.now().strftime("%H:%M")

        subject = f"[BOT] TRADE OPEN: {direction} {symbol} @ ${entry:.4f}"
        body = (
            f"New trade opened:\n\n"
            f"Symbol:    {symbol}\n"
            f"Direction: {direction}\n"
            f"Entry:     ${entry:.4f}\n"
            f"Stop Loss: ${sl:.4f}\n"
            f"Take Profit: ${tp:.4f}\n"
            f"Position:  ${value:.2f}\n"
            f"Strategy:  {strategy}\n"
            f"Time:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        summary = f"{ts} OPEN {direction} {symbol} @ ${entry:.4f} | SL:${sl:.4f} TP:${tp:.4f} [{strategy}]"
        self._queue_trade_alert(
            kind    = "open",
            summary = summary,
            subject = subject,
            body    = body,
            sms_body= f"OPEN {direction} {symbol} @ ${entry:.4f}",
        )

    def trade_closed(self, trade: Dict):
        """Queue alert when a trade closes (sent in next digest)."""
        if not self.alert_on_close or not self._email_configured:
            return
        pnl = trade.get("pnl", 0) or 0
        if abs(pnl) < self.alert_min_pnl:
            return

        symbol    = trade.get("symbol", "?")
        direction = trade.get("direction", "?").upper()
        entry     = trade.get("entry_price", 0)
        exit_p    = trade.get("exit_price", 0)
        pnl_pct   = trade.get("pnl_pct", 0) or 0
        reason    = trade.get("exit_reason", "?").replace("_", " ").title()
        result    = "WIN" if pnl > 0 else "LOSS"
        strategy  = trade.get("strategy_name", "unknown")
        ts        = datetime.now().strftime("%H:%M")

        subject = f"[BOT] TRADE {result}: {symbol} {direction} | P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)"
        body = (
            f"Trade closed ({result}):\n\n"
            f"Symbol:    {symbol}\n"
            f"Direction: {direction}\n"
            f"Entry:     ${entry:.4f}\n"
            f"Exit:      ${exit_p:.4f}\n"
            f"Reason:    {reason}\n"
            f"P&L:       ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
            f"Strategy:  {strategy}\n"
            f"Time:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        summary = (
            f"{ts} {result} {direction} {symbol} "
            f"${pnl:+.2f} ({pnl_pct:+.2f}%) via {reason} [{strategy}]"
        )
        self._queue_trade_alert(
            kind    = "close_win" if pnl > 0 else "close_loss",
            summary = summary,
            subject = subject,
            body    = body,
            sms_body= f"{result} {symbol} ${pnl:+.2f} ({pnl_pct:+.2f}%)",
            pnl     = pnl,
        )

    def trading_halted(self, reason: str):
        """Send IMMEDIATE alert when trading is halted — bypasses digest queue."""
        if not self.alert_on_halt or not self._email_configured:
            return
        subject  = f"[BOT] TRADING HALTED — {reason}"
        body     = (
            f"Trading has been halted:\n\n"
            f"Reason: {reason}\n"
            f"Time:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Trading will resume tomorrow unless manually overridden."
        )
        # Halts always bypass the queue — you need to know immediately
        self._send_now(subject, body, sms_body=f"BOT HALTED: {reason}")

    # ----------------------------------------------------------
    #  PRICE ALERTS (for your personal manual trades)
    # ----------------------------------------------------------

    def check_price_alerts(self, symbol: str, current_price: float):
        """
        Check if any manual price alerts should fire for this symbol.
        Alerts are stored in the database via set_price_alert().
        """
        from data.database import db
        alerts = db.get_state(f"price_alerts_{symbol}", default=[])
        if not alerts:
            return

        fired   = []
        remaining = []
        for alert in alerts:
            target    = alert.get("target")
            condition = alert.get("condition")  # "above" or "below"
            note      = alert.get("note", "")

            triggered = (
                (condition == "above" and current_price >= target) or
                (condition == "below" and current_price <= target)
            )
            if triggered:
                fired.append((target, condition, note))
            else:
                remaining.append(alert)

        for target, condition, note in fired:
            msg = f"PRICE ALERT: {symbol} is {condition} ${target:.4f} (now ${current_price:.4f})"
            if note:
                msg += f" — {note}"
            logger.info(msg)
            self._send(
                subject  = f"[ALERT] {symbol} {condition} ${target:.4f}",
                body     = f"{msg}\n\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                sms_body = msg
            )

        # Remove fired alerts, keep the rest
        if fired:
            from data.database import db
            db.set_state(f"price_alerts_{symbol}", remaining)

    def set_price_alert(self, symbol: str, target: float,
                        condition: str = "above", note: str = "") -> str:
        """
        Set a price alert for any symbol (including your personal SOXS/SOXL).
        condition: "above" or "below"
        Example: alerts.set_price_alert("SOXL", 25.50, "above", "Take profit target")
        """
        from data.database import db
        existing = db.get_state(f"price_alerts_{symbol}", default=[])
        existing.append({
            "target":    target,
            "condition": condition,
            "note":      note,
            "created":   datetime.now().isoformat()
        })
        db.set_state(f"price_alerts_{symbol}", existing)
        msg = f"Price alert set: {symbol} {condition} ${target:.4f}"
        if note:
            msg += f" ({note})"
        logger.info(msg)
        return msg

    def list_price_alerts(self) -> List[Dict]:
        """List all active price alerts."""
        from data.database import db
        # Check all possible symbols
        all_symbols = (
            list(config.CRYPTO_WATCHLIST) +
            list(config.STOCK_WATCHLIST) +
            ["SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXS"]
        )
        all_alerts = []
        for sym in all_symbols:
            alerts = db.get_state(f"price_alerts_{sym}", default=[])
            for a in alerts:
                all_alerts.append({**a, "symbol": sym})
        return all_alerts

    def clear_price_alert(self, symbol: str, target: float = None):
        """Clear price alerts for a symbol. If target specified, only remove that one."""
        from data.database import db
        if target is None:
            db.set_state(f"price_alerts_{symbol}", [])
            logger.info(f"All price alerts cleared for {symbol}")
        else:
            existing = db.get_state(f"price_alerts_{symbol}", default=[])
            remaining = [a for a in existing if a.get("target") != target]
            db.set_state(f"price_alerts_{symbol}", remaining)
            logger.info(f"Price alert cleared: {symbol} @ ${target:.4f}")

    # ----------------------------------------------------------
    #  INTERNAL EMAIL/SMS SENDER
    # ----------------------------------------------------------

    # ----------------------------------------------------------
    #  DIGEST QUEUE — called by position_monitor / bot_engine
    #  on a schedule (e.g. every 30s) to flush queued alerts
    # ----------------------------------------------------------

    def flush_digest_if_due(self):
        """
        Called externally on a schedule (recommend every 60s from bot_engine).
        Sends a single digest email containing all queued trade alerts if the
        digest interval has elapsed. Safe to call frequently — does nothing
        until the interval is reached.
        """
        if self._digest_hours == 0:
            return  # batching disabled — alerts sent immediately in _send()

        now     = datetime.now(timezone.utc)
        elapsed = (now - self._last_flush).total_seconds() / 3600

        if elapsed < self._digest_hours:
            return  # not time yet

        with self._queue_lock:
            if not self._queue:
                self._last_flush = now
                return  # nothing queued

            items = list(self._queue)
            self._queue.clear()
            self._last_flush = now

        # Build digest email
        wins   = [i for i in items if i["kind"] == "close_win"]
        losses = [i for i in items if i["kind"] == "close_loss"]
        opens  = [i for i in items if i["kind"] == "open"]

        total_pnl = sum(i.get("pnl", 0) for i in wins + losses)
        period    = f"Last {self._digest_hours}hr digest"

        lines = [
            f"Trading Bot Digest — {period}",
            f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            f"Summary: {len(opens)} opened | {len(wins)} wins | {len(losses)} losses | Net P&L: ${total_pnl:+.2f}",
            "=" * 55,
        ]

        if opens:
            lines.append(f"\nOPENED ({len(opens)}):")
            for i in opens:
                lines.append(f"  {i['summary']}")

        if wins:
            lines.append(f"\nWINS ({len(wins)}):")
            for i in wins:
                lines.append(f"  {i['summary']}")

        if losses:
            lines.append(f"\nLOSSES ({len(losses)}):")
            for i in losses:
                lines.append(f"  {i['summary']}")

        subject = (
            f"[BOT DIGEST] {len(wins)}W/{len(losses)}L | "
            f"Net ${total_pnl:+.2f} | {period}"
        )
        body = "\n".join(lines)

        # Single SMS summary
        sms = (
            f"BOT {period}: {len(wins)}W/{len(losses)}L "
            f"Net ${total_pnl:+.2f}"
        )

        self._send_now(subject, body, sms_body=sms)
        logger.info(
            f"[ALERT DIGEST] Sent: {len(opens)} opens, "
            f"{len(wins)} wins, {len(losses)} losses, net ${total_pnl:+.2f}"
        )

    def _queue_trade_alert(self, kind: str, summary: str,
                           subject: str, body: str,
                           sms_body: str = None, pnl: float = 0.0):
        """
        Add a trade alert to the digest queue.
        kind: 'open', 'close_win', 'close_loss'
        If digest is disabled (ALERT_DIGEST_HOURS=0), sends immediately.
        """
        if self._digest_hours == 0:
            # Batching disabled — fire immediately like old behavior
            self._send_now(subject, body, sms_body)
            return

        with self._queue_lock:
            self._queue.append({
                "kind":    kind,
                "summary": summary,
                "subject": subject,
                "body":    body,
                "sms":     sms_body,
                "pnl":     pnl,
                "time":    datetime.now(timezone.utc).isoformat(),
            })

    def _send(self, subject: str, body: str, sms_body: str = None,
              immediate: bool = False):
        """
        Route alert to queue (trade alerts) or send immediately (errors/halts).
        Pass immediate=True to bypass the queue — used for halts and errors.
        """
        if immediate or self._digest_hours == 0:
            self._send_now(subject, body, sms_body)
        else:
            # Trade alerts go through the queue — caller uses _queue_trade_alert()
            # directly so _send() with immediate=False is only called for halts
            self._send_now(subject, body, sms_body)

    def _send_now(self, subject: str, body: str, sms_body: str = None):
        """Actually send email and SMS immediately — bypasses queue."""
        if not self._email_configured:
            return

        # Send email
        try:
            msg            = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = self.email_sender
            msg["To"]      = self.email_recipient
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.email_sender, self.email_password)
                server.sendmail(self.email_sender, self.email_recipient, msg.as_string())
            logger.info(f"Alert email sent: {subject}")
        except Exception as e:
            logger.error(f"Alert email failed: {e}")

        # Send SMS via email-to-SMS gateway
        if self._sms_configured and sms_body:
            try:
                sms_msg            = MIMEMultipart()
                sms_msg["Subject"] = ""
                sms_msg["From"]    = self.email_sender
                sms_msg["To"]      = self.sms_recipient
                sms_msg.attach(MIMEText(sms_body[:160], "plain"))  # SMS 160 char limit

                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(self.email_sender, self.email_password)
                    server.sendmail(self.email_sender, self.sms_recipient, sms_msg.as_string())
                logger.info(f"SMS alert sent to {self.sms_recipient}")
            except Exception as e:
                logger.error(f"SMS alert failed: {e}")


# Singleton
alert_manager = AlertManager()
