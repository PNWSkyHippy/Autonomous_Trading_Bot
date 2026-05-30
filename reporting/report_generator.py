"""
=============================================================
  DAILY REPORT GENERATOR
  Generates comprehensive daily trading reports and emails them.
  Also produces end-of-day database summaries for tax records.
  Includes a Claude AI Activity Summary section in every report.
=============================================================
"""

import os
import smtplib
import logging
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List
import sys

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)

import config
from data.database import db
from core.risk_manager import risk_manager

logger = logging.getLogger(__name__)


class ReportGenerator:

    def __init__(self, db_ref=None):
        self._db = db_ref or db

    def generate_daily_summary(self, report_date: str = None) -> Dict:
        """Compile all data for a given trading day into a summary dict."""
        report_date = report_date or date.today().isoformat()
        trades  = self._db.get_trades_for_date(report_date)
        closed  = [t for t in trades if t["status"] == "closed"]

        wins   = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]

        total_pnl    = sum(t.get("pnl", 0) for t in closed)
        win_rate     = (len(wins) / len(closed) * 100) if closed else 0
        largest_win  = max((t.get("pnl", 0) for t in wins),   default=0)
        largest_loss = min((t.get("pnl", 0) for t in losses), default=0)
        avg_win  = (sum(t["pnl"] for t in wins)   / len(wins))   if wins   else 0
        avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0

        cap             = self._db.get_latest_capital()
        ending_capital  = cap["total_capital"] if cap else 0
        starting_capital= ending_capital - total_pnl
        pnl_pct         = (total_pnl / starting_capital * 100) if starting_capital else 0

        session  = risk_manager.get_daily_status()
        goal_met = pnl_pct >= config.DAILY_PROFIT_TARGET_PCT

        profit_factor = 999
        if losses:
            loss_sum = abs(sum(t["pnl"] for t in losses))
            win_sum  = sum(t["pnl"] for t in wins)
            profit_factor = round(win_sum / loss_sum, 2) if loss_sum else 999

        summary = {
            "trade_date":        report_date,
            "starting_capital":  round(starting_capital or 0, 2),
            "ending_capital":    round(ending_capital or 0, 2),
            "daily_pnl":         round(total_pnl or 0, 2),
            "daily_pnl_pct":     round(pnl_pct or 0, 2),
            "total_trades":      len(closed),
            "winning_trades":    len(wins),
            "losing_trades":     len(losses),
            "win_rate":          round(win_rate or 0, 1),
            "largest_win":       round(largest_win or 0, 2),
            "largest_loss":      round(largest_loss or 0, 2),
            "avg_win":           round(avg_win or 0, 2),
            "avg_loss":          round(avg_loss or 0, 2),
            "profit_factor":     profit_factor or 0,
            "trading_halted":    int(not session.trading_active),
            "halt_reason":       session.halt_reason or "",
            "goal_met":          int(goal_met),
            "trades":            closed
        }
        return summary

    def _build_claude_summary(self, report_date: str) -> Dict:
        """
        Compile Claude AI activity stats for the given date.
        Returns a dict with counts and action breakdown for the EOD report.
        """
        try:
            actions = self._db.get_chat_actions_for_date(report_date)
        except Exception:
            return {}

        if not actions:
            return {"total": 0, "actions": [], "breakdown": {}}

        # Separate informational messages from actual commands
        messages  = [a for a in actions if a["action_type"] == "message"]
        commands  = [a for a in actions if a["action_type"] != "message"]

        # Count by action type
        breakdown = {}
        for a in commands:
            t = a["action_type"]
            breakdown[t] = breakdown.get(t, 0) + 1

        return {
            "total":        len(messages),       # how many times user talked to Claude
            "commands":     len(commands),        # how many actions Claude executed
            "breakdown":    breakdown,            # action type -> count
            "actions":      commands,             # full list for detail table
        }

    def generate_html_report(self, summary: Dict) -> str:
        """Generate a clean HTML email report including Claude activity."""
        trades_html = ""
        for t in summary.get("trades", []):
            pnl   = t.get("pnl", 0)
            color = "#27ae60" if pnl > 0 else "#e74c3c"
            icon  = "UP" if pnl > 0 else "DN"
            strat = t.get("strategy_name", "original")
            trades_html += f"""
            <tr>
                <td>{t['symbol']}</td>
                <td>{t['asset_class'].capitalize()}</td>
                <td>{t['direction'].capitalize()}</td>
                <td>{strat}</td>
                <td>${t['entry_price']:.4f}</td>
                <td>${t.get('exit_price', 0):.4f}</td>
                <td>{t.get('exit_reason', '').replace('_', ' ').title()}</td>
                <td style="color:{color};font-weight:bold">[{icon}] ${abs(pnl):.2f}</td>
                <td style="color:{color}">{t.get('pnl_pct', 0):+.2f}%</td>
            </tr>"""

        goal_color  = "#27ae60" if summary["goal_met"] else "#e67e22"
        goal_text   = "GOAL MET" if summary["goal_met"] else "GOAL NOT MET"
        halt_banner = ""
        if summary.get("trading_halted"):
            halt_banner = f"""
            <div style="background:#e74c3c;color:white;padding:10px;margin:10px 0;border-radius:4px;">
                <b>TRADING HALTED:</b> {summary.get('halt_reason', 'Unknown reason')}
            </div>"""

        # Claude activity section
        claude_data = self._build_claude_summary(summary["trade_date"])
        if claude_data.get("total", 0) > 0 or claude_data.get("commands", 0) > 0:
            breakdown_rows = ""
            for action_type, count in claude_data.get("breakdown", {}).items():
                breakdown_rows += f"""
                <tr>
                    <td style="padding:4px 8px;">{action_type.replace('_', ' ').title()}</td>
                    <td style="padding:4px 8px;text-align:right;">{count}</td>
                </tr>"""

            action_detail_rows = ""
            for a in claude_data.get("actions", [])[-10:]:  # last 10 actions
                ts = a.get("timestamp", "")[:16].replace("T", " ")
                action_detail_rows += f"""
                <tr style="font-size:11px;">
                    <td style="padding:3px 6px;color:#7f8c8d;">{ts}</td>
                    <td style="padding:3px 6px;">{a['action_type'].replace('_', ' ').title()}</td>
                    <td style="padding:3px 6px;color:#7f8c8d;">{a.get('user_message', '')[:60]}</td>
                    <td style="padding:3px 6px;">{a.get('result', '')[:60]}</td>
                </tr>"""

            claude_section = f"""
        <h3 style="color:#8e44ad;margin-top:30px;">🤖 Claude AI Activity Summary</h3>
        <table style="width:100%;border-collapse:collapse;margin-bottom:10px;">
            <tr style="background:#8e44ad;color:white;">
                <th style="padding:8px;text-align:left;">Metric</th>
                <th style="padding:8px;text-align:right;">Count</th>
            </tr>
            <tr style="background:#f8f9fa;">
                <td style="padding:6px 8px;">User messages to Claude</td>
                <td style="padding:6px 8px;text-align:right;">{claude_data['total']}</td>
            </tr>
            <tr>
                <td style="padding:6px 8px;">Commands Claude executed</td>
                <td style="padding:6px 8px;text-align:right;">{claude_data['commands']}</td>
            </tr>
            {breakdown_rows}
        </table>
        {'<h4 style="color:#8e44ad;">Action Log (last 10)</h4><table style="width:100%;border-collapse:collapse;font-size:11px;"><tr style="background:#8e44ad;color:white;"><th style="padding:4px 6px;">Time</th><th style="padding:4px 6px;">Action</th><th style="padding:4px 6px;">Message</th><th style="padding:4px 6px;">Result</th></tr>' + action_detail_rows + '</table>' if action_detail_rows else ''}
            """
        else:
            claude_section = """
        <h3 style="color:#8e44ad;margin-top:30px;">🤖 Claude AI Activity</h3>
        <p style="color:#7f8c8d;font-size:12px;">No Claude chat interactions today.</p>
            """

        html = f"""
        <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px;">
        <h2 style="color:#2c3e50;">Daily Trading Report — {summary['trade_date']}</h2>
        {halt_banner}

        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
            <tr style="background:#34495e;color:white;">
                <th style="padding:10px;text-align:left;">Metric</th>
                <th style="padding:10px;text-align:right;">Value</th>
            </tr>
            <tr style="background:#f8f9fa;">
                <td style="padding:8px;">Starting Capital</td>
                <td style="padding:8px;text-align:right;">${summary['starting_capital']:,.2f}</td>
            </tr>
            <tr>
                <td style="padding:8px;">Ending Capital</td>
                <td style="padding:8px;text-align:right;">${summary['ending_capital']:,.2f}</td>
            </tr>
            <tr style="background:#f8f9fa;">
                <td style="padding:8px;font-weight:bold;">Daily P&L</td>
                <td style="padding:8px;text-align:right;font-weight:bold;
                    color:{'#27ae60' if summary['daily_pnl'] >= 0 else '#e74c3c'};">
                    ${summary['daily_pnl']:+,.2f} ({summary['daily_pnl_pct']:+.2f}%)</td>
            </tr>
            <tr>
                <td style="padding:8px;">Daily Goal</td>
                <td style="padding:8px;text-align:right;color:{goal_color};font-weight:bold;">
                    {goal_text}</td>
            </tr>
            <tr style="background:#f8f9fa;">
                <td style="padding:8px;">Trades</td>
                <td style="padding:8px;text-align:right;">
                    {summary['total_trades']} total |
                    {summary['winning_trades']}W /
                    {summary['losing_trades']}L |
                    {summary['win_rate']:.1f}% win rate</td>
            </tr>
            <tr>
                <td style="padding:8px;">Profit Factor</td>
                <td style="padding:8px;text-align:right;">{summary['profit_factor']}</td>
            </tr>
            <tr style="background:#f8f9fa;">
                <td style="padding:8px;">Largest Win / Loss</td>
                <td style="padding:8px;text-align:right;">
                    <span style="color:#27ae60;">${summary['largest_win']:,.2f}</span> /
                    <span style="color:#e74c3c;">${summary['largest_loss']:,.2f}</span></td>
            </tr>
        </table>

        <h3 style="color:#2c3e50;">Trade Log</h3>
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
            <tr style="background:#34495e;color:white;">
                <th style="padding:6px;">Symbol</th>
                <th style="padding:6px;">Type</th>
                <th style="padding:6px;">Dir</th>
                <th style="padding:6px;">Strategy</th>
                <th style="padding:6px;">Entry</th>
                <th style="padding:6px;">Exit</th>
                <th style="padding:6px;">Reason</th>
                <th style="padding:6px;">P&L $</th>
                <th style="padding:6px;">P&L %</th>
            </tr>
            {trades_html if trades_html else '<tr><td colspan="9" style="padding:10px;text-align:center;color:#7f8c8d;">No trades today.</td></tr>'}
        </table>

        {claude_section}

        <p style="color:#7f8c8d;font-size:11px;margin-top:20px;">
            Generated by Trading Bot v2 at {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}
        </p>
        </body></html>
        """
        return html

    def generate_and_send_daily_report(self, report_date: str = None):
        """Generate end-of-day summary, save to DB, and email it."""
        summary = self.generate_daily_summary(report_date)

        self._db.save_daily_summary(summary)

        for trade in summary.get("trades", []):
            try:
                self._db.record_tax_event(trade)
            except Exception as e:
                logger.error(f"Tax record error for {trade.get('trade_id')}: {e}")

        if config.EMAIL_SENDER and config.EMAIL_PASSWORD and config.EMAIL_RECIPIENT:
            try:
                self._send_email(summary)
            except Exception as e:
                logger.error(f"Failed to send daily report email: {e}")
        else:
            logger.info(
                "Email not configured — daily report saved to database only. "
                "Add EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECIPIENT to .env to enable."
            )

        # Log Claude activity count for the day
        claude_data = self._build_claude_summary(summary["trade_date"])
        if claude_data.get("total", 0) > 0:
            logger.info(
                f"Claude activity today: {claude_data['total']} messages, "
                f"{claude_data['commands']} commands executed: "
                f"{claude_data['breakdown']}"
            )

        logger.info(
            f"Daily report complete: "
            f"${summary['daily_pnl']:+.2f} | "
            f"{summary['total_trades']} trades | "
            f"Capital: ${summary['ending_capital']:,.2f}"
        )
        return summary

    def _send_email(self, summary: Dict):
        """Send the HTML report via email (Gmail SMTP)."""
        pnl_str  = f"{summary['daily_pnl']:+.2f}"
        pnl_pct  = f"{summary['daily_pnl_pct']:+.2f}%"
        goal_str = "GOAL MET" if summary["goal_met"] else "goal not met"
        subject  = (
            f"Trading Bot Report {summary['trade_date']} | "
            f"P&L: ${pnl_str} ({pnl_pct}) | {goal_str}"
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = config.EMAIL_SENDER
        msg["To"]      = config.EMAIL_RECIPIENT

        html_body = self.generate_html_report(summary)
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            server.sendmail(
                config.EMAIL_SENDER,
                config.EMAIL_RECIPIENT,
                msg.as_string()
            )

        logger.info(f"Daily report emailed to {config.EMAIL_RECIPIENT}")

    def export_tax_csv(self, year: int = None) -> str:
        """Export Form 8949 CSV for a given tax year."""
        year = year or datetime.now().year
        os.makedirs("exports", exist_ok=True)
        filepath = f"exports/tax_form_8949_{year}.csv"
        self._db.export_8949_csv(year, filepath)
        return filepath
