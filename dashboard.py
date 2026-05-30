"""
=============================================================
  LIVE TRADING DASHBOARD
  Run with: streamlit run dashboard.py
  Opens in your browser at http://localhost:8501
  Auto-refreshes every 10 seconds.
=============================================================
"""

import sys
import os
import logging
sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
import subprocess
import signal
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import date, datetime, timedelta
from pathlib import Path

import config
from data.database import db
from core.risk_manager import risk_manager
from core.position_monitor import monitor
from intelligence.chat_interface import chat
from reporting.report_generator import ReportGenerator

report_generator = ReportGenerator()
dash_logger = logging.getLogger("dashboard")


def parse_db_time(raw):
    """Parse DB timestamps under the local-time policy, preserving old tz-tagged rows."""
    if not raw:
        return None
    try:
        parsed = pd.to_datetime(raw)
        if getattr(parsed, "tzinfo", None) is not None:
            parsed = parsed.tz_convert(None)
        return parsed
    except Exception:
        return None


def normalize_entry_exit(entry_dt, exit_dt):
    """Older rows may have UTC entry_time and local exit_time in the same trade."""
    if entry_dt is None or exit_dt is None or entry_dt <= exit_dt:
        return entry_dt, exit_dt
    hours_ahead = (entry_dt - exit_dt).total_seconds() / 3600
    if 0 < hours_ahead <= 8.5:
        adjusted = entry_dt - pd.Timedelta(hours=7)
        if adjusted <= exit_dt:
            return adjusted, exit_dt
    return entry_dt, exit_dt


def fmt_db_time(raw):
    parsed = parse_db_time(raw)
    if parsed is None:
        return (raw[:16] if raw else "—"), pd.Timestamp.min
    return parsed.strftime("%m/%d %H:%M PDT"), parsed

# ----------------------------------------------------------
#  PAGE CONFIG
# ----------------------------------------------------------
st.set_page_config(
    page_title="Trading AI Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
    .stApp { background-color: #0f1117; color: #e0e0e0; }
    .metric-card { background: #1a1f35; border: 1px solid #2a3a5c; border-radius: 10px; padding: 20px; text-align: center; }
    .metric-label { font-size: 12px; color: #90a4ae; text-transform: uppercase; letter-spacing: 1px; }
    .metric-value { font-size: 28px; font-weight: bold; margin-top: 6px; }
    .green  { color: #4caf50; } .red { color: #f44336; } .blue { color: #4fc3f7; } .yellow { color: #ffb300; }
    .halted-banner { background: #c62828; color: white; padding: 12px; border-radius: 8px; text-align: center; font-weight: bold; font-size: 16px; margin-bottom: 16px; }
    .active-banner { background: #1b5e20; color: #a5d6a7; padding: 10px; border-radius: 8px; text-align: center; margin-bottom: 16px; }
    .stCaption, [data-testid="stCaptionContainer"] p, label, .stTextInput label, .stSelectbox label, .stNumberInput label, .stSlider label { color: #b0bec5 !important; font-size: 13px !important; }
    .stSelectbox > label, .stNumberInput > label, .stTextInput > label, .stSlider > label { color: #cfd8dc !important; font-weight: 500 !important; }
    .streamlit-expanderHeader { color: #e0e0e0 !important; font-weight: 600 !important; }
    [data-testid="stMetricLabel"] { color: #cfd8dc !important; font-size: 13px !important; font-weight: 500 !important; }
    [data-testid="stMetricValue"] { color: #ffffff !important; }
    .stSelectbox div[data-baseweb="select"] { color: #ffffff !important; }
    div[data-testid="stChatMessage"] { background: #1a1f35 !important; border-radius: 10px !important; }
    div[data-testid="stChatMessage"] p { color: #ffffff !important; font-size: 14px !important; }
    div[data-testid="stChatMessage"] li { color: #ffffff !important; }
    div[data-testid="stChatMessage"] td { color: #ffffff !important; }
    div[data-testid="stChatMessage"] th { color: #ffffff !important; }
    div[data-testid="stChatMessage"] code { color: #4fc3f7 !important; }
    div[data-testid="stChatMessage"] strong { color: #ffffff !important; }
    div[data-testid="stChatMessage"] em { color: #e0e0e0 !important; }
    div[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] * { color: #ffffff !important; }
    div[data-testid="stChatInput"] { background: #ffffff !important; border: 1px solid #2a3a5c !important; border-radius: 10px !important; }
    div[data-testid="stChatInput"] textarea { color: #111111 !important; background: #ffffff !important; }
    div[data-testid="stChatInput"] textarea::placeholder { color: #888888 !important; }
    div[data-testid="stSuccess"] { background-color: #0d3b1a !important; border-color: #22c55e !important; color: #86efac !important; }
    div[data-testid="stError"]   { background-color: #3b0d0d !important; border-color: #ef4444 !important; color: #fca5a5 !important; }
    div[data-testid="stWarning"] { background-color: #3b2500 !important; border-color: #f59e0b !important; color: #fcd34d !important; }
    div[data-testid="stInfo"]    { background-color: #0d1f3b !important; border-color: #3b82f6 !important; color: #93c5fd !important; }
    .section-label { color: #90a4ae; font-size: 13px; margin-top: -8px; margin-bottom: 8px; }
    section[data-testid="stSidebar"] { background-color: #0d1117 !important; }
    section[data-testid="stSidebar"] p, section[data-testid="stSidebar"] span, section[data-testid="stSidebar"] label, section[data-testid="stSidebar"] div, section[data-testid="stSidebar"] small { color: #ffffff !important; }
    section[data-testid="stSidebar"] [data-testid="stMetricValue"] { color: #4fc3f7 !important; }
    section[data-testid="stSidebar"] [data-testid="stMetricLabel"] { color: #90a4ae !important; }
    section[data-testid="stSidebar"] hr { border-color: #2a3a5c !important; }
    section[data-testid="stSidebar"] input { background-color: #1a1f35 !important; color: #e0e0e0 !important; border-color: #2a3a5c !important; }
    section[data-testid="stSidebar"] [data-testid="stProgressBar"] > div { background-color: #4fc3f7 !important; }
    section[data-testid="stSidebar"] .stButton > button, section[data-testid="stSidebar"] [data-testid="baseButton-secondary"] { color: #0f1117 !important; background-color: #d0d8e8 !important; border-color: #a0b0c8 !important; font-weight: 700 !important; }
    section[data-testid="stSidebar"] [data-testid="baseButton-primary"], section[data-testid="stSidebar"] .stButton > button[kind="primary"] { color: #ffffff !important; background-color: #1565c0 !important; border-color: #1565c0 !important; font-weight: 700 !important; }
</style>
""", unsafe_allow_html=True)

def notify_success(msg: str):
    st.markdown(f'<div style="background:#0d3b1a;border:2px solid #22c55e;border-radius:8px;padding:12px 16px;color:#86efac;font-weight:600;font-size:14px;margin:8px 0;">✅ {msg}</div>', unsafe_allow_html=True)

def notify_error(msg: str):
    st.markdown(f'<div style="background:#3b0d0d;border:2px solid #ef4444;border-radius:8px;padding:12px 16px;color:#fca5a5;font-weight:600;font-size:14px;margin:8px 0;">❌ {msg}</div>', unsafe_allow_html=True)

def notify_info(msg: str):
    st.markdown(f'<div style="background:#0d1f3b;border:2px solid #3b82f6;border-radius:8px;padding:12px 16px;color:#93c5fd;font-weight:600;font-size:14px;margin:8px 0;">ℹ️ {msg}</div>', unsafe_allow_html=True)

def section_note(msg: str):
    st.markdown(f'<p class="section-label">{msg}</p>', unsafe_allow_html=True)

# ----------------------------------------------------------
#  CANDLESTICK CHART HELPER
# ----------------------------------------------------------
def render_chart(symbol: str, asset_class: str, entry_price: float = None, stop_loss: float = None, take_profit: float = None, entry_time: str = None):
    """
    Fetch OHLCV bars and render a Plotly candlestick chart.
    Supports 5Min, 1Hour, 1Day for stocks (Alpaca)
    and 5m, 1h, 1d for crypto (CCXT/Kraken).
    Entry, SL, TP lines drawn if provided.
    """
    tf_col, _ = st.columns([1, 3])
    with tf_col:
        timeframe = st.selectbox(
            "Candle size",
            options=["5 Min", "1 Hour", "1 Day"],
            key=f"tf_{symbol}_{asset_class}"
        )

    tf_map_stock  = {"5 Min": "5Min",  "1 Hour": "1Hour", "1 Day": "1Day"}
    tf_map_crypto = {"5 Min": "5m",    "1 Hour": "1h",    "1 Day": "1d"}
    limit_map     = {"5 Min": 100,     "1 Hour": 100,     "1 Day": 90}

    bars = None
    try:
        if asset_class == "stock":
            import alpaca_trade_api as tradeapi
            api  = tradeapi.REST(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, config.ALPACA_BASE_URL)
            tf   = tf_map_stock[timeframe]
            raw  = api.get_bars(symbol, tf, limit=limit_map[timeframe]).df
            if not raw.empty:
                raw.index = pd.to_datetime(raw.index)
                bars = raw.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c"})
        else:
            import ccxt
            exchange = ccxt.kraken({"enableRateLimit": True, "timeout": 10000})
            tf       = tf_map_crypto[timeframe]
            ohlcv    = exchange.fetch_ohlcv(symbol, tf, limit=limit_map[timeframe])
            if ohlcv:
                bars = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
                bars["ts"] = pd.to_datetime(bars["ts"], unit="ms")
                bars = bars.set_index("ts")
    except Exception as e:
        st.warning(f"Could not load chart data: {e}")
        return

    if bars is None or bars.empty:
        st.info("No chart data available.")
        return

    fig = go.Figure(go.Candlestick(
        x     = bars.index,
        open  = bars["o"],
        high  = bars["h"],
        low   = bars["l"],
        close = bars["c"],
        increasing_line_color = "#4caf50",
        decreasing_line_color = "#f44336",
        name  = symbol,
    ))

    # Draw entry / SL / TP reference lines
    if entry_price:
        fig.add_hline(y=entry_price, line_color="#4fc3f7",  line_dash="dash", annotation_text="Entry",  annotation_font_color="#4fc3f7")
    if stop_loss:
        fig.add_hline(y=stop_loss,   line_color="#f44336",  line_dash="dot",  annotation_text="SL",     annotation_font_color="#f44336")
    if take_profit:
        fig.add_hline(y=take_profit, line_color="#4caf50",  line_dash="dot",  annotation_text="TP",     annotation_font_color="#4caf50")
    if entry_time:
        try:
            et = pd.to_datetime(entry_time)
            fig.add_vline(
                x          = et.timestamp() * 1000,
                line_color = "#4fc3f7",
                line_dash  = "dash",
                line_width = 1,
                annotation_text      = f"Entered {et.strftime('%H:%M')}",
                annotation_font_color= "#4fc3f7",
                annotation_position  = "top right",
            )
        except Exception:
            pass

    fig.update_layout(
        paper_bgcolor = "#0f1117",
        plot_bgcolor  = "#0f1117",
        font          = dict(color="#e0e0e0"),
        margin        = dict(l=10, r=10, t=30, b=10),
        xaxis      = dict(
            gridcolor   = "#2a3a5c",
            rangeslider = dict(
                visible    = True,
                thickness  = 0.08,
                bgcolor    = "#1a1f35",
                bordercolor= "#2a3a5c",
            ),
            rangeselector = dict(
                bgcolor     = "#1a2035",
                activecolor = "#3266ad",
                font        = dict(color="#e0e0e0", size=10),
                buttons     = [
                    dict(count=3,  label="3H",  step="hour", stepmode="backward"),
                    dict(count=6,  label="6H",  step="hour", stepmode="backward"),
                    dict(count=12, label="12H", step="hour", stepmode="backward"),
                    dict(count=1,  label="1D",  step="day",  stepmode="backward"),
                    dict(count=3,  label="3D",  step="day",  stepmode="backward"),
                    dict(step="all", label="All"),
                ]
            )
        ),
        yaxis      = dict(gridcolor="#2a3a5c", tickprefix="$", fixedrange=False),
        height     = 320,
        title      = dict(text=f"{symbol} — {timeframe}", font=dict(color="#e0e0e0", size=14)),
        showlegend = False,
    )
    st.plotly_chart(fig, width='stretch')

# ----------------------------------------------------------
#  STOCK CLOSE HELPER
# ----------------------------------------------------------
def close_stock_position(symbol: str, trade_record: dict, current_price: float) -> tuple[bool, str]:
    try:
        import alpaca_trade_api as tradeapi
        api = tradeapi.REST(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, config.ALPACA_BASE_URL)
        cancelled = 0
        try:
            open_orders = api.list_orders(status="open", symbols=[symbol])
            for order in open_orders:
                try:
                    api.cancel_order(order.id)
                    cancelled += 1
                except Exception:
                    pass
        except Exception:
            pass
        try:
            api.close_position(symbol)
        except Exception as ce:
            try:
                pos = api.get_position(symbol)
                if pos:
                    return False, f"Position still open: {ce}"
            except Exception:
                pass
        entry_price = trade_record.get("entry_price", current_price)
        quantity    = trade_record.get("quantity", 0)
        direction   = trade_record.get("direction", "long")
        pos_val     = trade_record.get("position_value", 0)
        pnl     = (current_price - entry_price) * quantity
        if direction == "short":
            pnl = -pnl
        pnl_pct = (pnl / pos_val * 100) if pos_val else 0
        db.close_trade(trade_id=trade_record["trade_id"], exit_price=current_price, exit_reason="manual_close", pnl=round(pnl, 4), pnl_pct=round(pnl_pct, 4))
        try:
            risk_manager.record_trade_result(pnl, pnl > 0)
        except Exception:
            pass
        msg = f"Closed {symbol} @ ${current_price:.4f} | P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%) | Cancelled {cancelled} orders"
        return True, msg
    except Exception as e:
        return False, f"Stock close error for {symbol}: {e}"

# ----------------------------------------------------------
#  AUTO REFRESH + LOAD DATA
# ----------------------------------------------------------
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = datetime.now()

@st.cache_data(ttl=10)
def load_data():
    session_obj  = risk_manager.get_daily_status()
    # Convert session to plain dict so st.cache_data can pickle it
    session = {
        "trading_active":         session_obj.trading_active,
        "halt_reason":            session_obj.halt_reason,
        "pnl_today":              session_obj.pnl_today,
        "trades_today":           session_obj.trades_today,
        "consecutive_losses":     session_obj.consecutive_losses,
        "starting_capital_today": session_obj.starting_capital_today,
    }
    cap          = db.get_latest_capital()
    positions    = monitor.get_positions_summary()
    summaries    = db.get_daily_summaries(30)
    today_trades = db.get_trades_for_date(date.today().isoformat())
    all_trades   = db.get_all_closed_trades(200)
    return session, cap, positions, summaries, today_trades, all_trades

session, cap, positions, summaries, today_trades, all_trades = load_data()
capital      = cap["total_capital"] if cap else 0
daily_pnl    = session["pnl_today"]

# Use locked starting capital — never drifts on restart
starting_cap  = session["starting_capital_today"] if session["starting_capital_today"] > 0 else max(capital - daily_pnl, 1.0)
daily_pnl_pct = (daily_pnl / starting_cap * 100) if starting_cap else 0

# ----------------------------------------------------------
#  HEADER
# ----------------------------------------------------------
col_logo, col_title, col_time = st.columns([1, 4, 2])
with col_title:
    st.markdown("# 📈 Autonomous Trading AI")
    st.markdown(f"*{datetime.now().strftime('%A, %B %d %Y — %H:%M:%S')}*")
with col_time:
    if st.button("🔄 Refresh", width='stretch'):
        st.cache_data.clear()
        st.rerun()
st.divider()
if not session["trading_active"]:
    st.markdown(f'<div class="halted-banner">🛑 TRADING HALTED — {session["halt_reason"] or "Unknown reason"}</div>', unsafe_allow_html=True)
else:
    st.markdown('<div class="active-banner">✅ Trading Active — Bot is running and scanning markets</div>', unsafe_allow_html=True)

# ----------------------------------------------------------
#  TOP METRICS ROW
# ----------------------------------------------------------
c1, c2, c3, c4, c5, c6 = st.columns(6)
def metric_card(col, label, value, color_class="blue"):
    col.markdown(f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value {color_class}">{value}</div></div>', unsafe_allow_html=True)
pnl_color = "green" if daily_pnl >= 0 else "red"
metric_card(c1, "Total Capital",  f"${capital:,.2f}",        "blue")
metric_card(c2, "Daily P&L",      f"${daily_pnl:+,.2f}",    pnl_color)
metric_card(c3, "Daily P&L %",    f"{daily_pnl_pct:+.2f}%", pnl_color)
metric_card(c4, "Open Positions", str(len(positions)),        "yellow")
metric_card(c5, "Trades Today",   str(session["trades_today"]), "blue")
metric_card(c6, "Consec. Losses", str(session["consecutive_losses"]), "red" if session["consecutive_losses"] >= 2 else "green")
st.markdown("&nbsp;", unsafe_allow_html=True)

# ----------------------------------------------------------
#  CHARTS ROW
# ----------------------------------------------------------
chart_col1, chart_col2 = st.columns([2, 1])
with chart_col1:
    st.markdown("### 📊 Capital Growth (30 Days)")
    if summaries:
        df_sum = pd.DataFrame(summaries[::-1])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df_sum["trade_date"], y=df_sum["ending_capital"], mode="lines+markers", name="Capital", line=dict(color="#4fc3f7", width=2), fill="tozeroy", fillcolor="rgba(79,195,247,0.08)"))
        fig.add_hline(y=starting_cap, line_dash="dot", line_color="#546e7a", annotation_text="Today's Start")
        fig.update_layout(paper_bgcolor="#1a1f35", plot_bgcolor="#1a1f35", font=dict(color="#e0e0e0"), margin=dict(l=20, r=20, t=20, b=20), xaxis=dict(gridcolor="#2a3a5c"), yaxis=dict(gridcolor="#2a3a5c", tickprefix="$", fixedrange=False))
        st.plotly_chart(fig, width='stretch')
    else:
        st.info("No historical data yet.")
with chart_col2:
    st.markdown("### 🫙 Win/Loss Distribution")
    if all_trades:
        wins   = sum(1 for t in all_trades if t.get("pnl", 0) > 0)
        losses = len(all_trades) - wins
        fig_pie = go.Figure(go.Pie(labels=["Wins", "Losses"], values=[wins, losses], hole=0.5, marker_colors=["#4caf50", "#f44336"]))
        fig_pie.update_layout(paper_bgcolor="#1a1f35", font=dict(color="#e0e0e0"), margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
        win_rate = (wins / len(all_trades) * 100) if all_trades else 0
        st.plotly_chart(fig_pie, width='stretch')
        st.markdown(f"<div style='text-align:center;color:#90caf9'>Overall Win Rate: <b>{win_rate:.1f}%</b></div>", unsafe_allow_html=True)
    else:
        st.info("No trade history yet.")
st.divider()

# ----------------------------------------------------------
#  OPEN POSITIONS TABLE
# ----------------------------------------------------------
st.markdown("### 📍 Open Positions")
section_note("Newest position first.")
if positions:
    pos_rows = []
    for p in positions:
        raw_time = p.get("entry_time", "") or ""
        entry_str, entry_dt = fmt_db_time(raw_time)
        pnl_val  = p.get("unrealized_pnl", 0) or 0
        pnl_pct  = p.get("pnl_pct", 0) or 0
        dist_sl  = p.get("distance_to_sl", 0) or 0
        dist_tp  = p.get("distance_to_tp", 0) or 0
        is_stuck = p.get("is_stuck", False)
        stuck_label = " ⚠️ STUCK" if is_stuck else ""
        pos_rows.append({"_entry_dt": entry_dt, "Opened": entry_str, "Symbol": p["symbol"] + stuck_label, "Type": p["asset_class"].capitalize(), "Dir": p["direction"].upper(), "Entry $": float(p["entry_price"]), "Current $": float(p["current_price"]), "Stop Loss $": float(p["stop_loss"]), "Take Profit $": (f"${p['take_profit']:.4f}" + (" 🔴 *" + str(p.get("tp_hit_count", 0)) if p.get("tp_hit_count", 0) > 0 else "")), "Unreal P&L $": float(pnl_val), "P&L %": float(pnl_pct), "To SL %": float(dist_sl), "To TP %": float(dist_tp)})
    df_pos = pd.DataFrame(pos_rows).sort_values("_entry_dt", ascending=False).reset_index(drop=True).drop(columns=["_entry_dt"])
    st.dataframe(df_pos, width='stretch', hide_index=True, column_config={"Opened": st.column_config.TextColumn("Opened", width="small"), "Symbol": st.column_config.TextColumn("Symbol", width="small"), "Type": st.column_config.TextColumn("Type", width="small"), "Dir": st.column_config.TextColumn("Dir", width="small"), "Entry $": st.column_config.NumberColumn("Entry $", format="$%.4f"), "Current $": st.column_config.NumberColumn("Current $", format="$%.4f"), "Stop Loss $": st.column_config.NumberColumn("Stop Loss $", format="$%.4f"), "Take Profit $": st.column_config.NumberColumn("Take Profit $", format="$%.4f"), "Unreal P&L $": st.column_config.NumberColumn("Unreal P&L $", format="$%+.2f"), "P&L %": st.column_config.NumberColumn("P&L %", format="%+.2f%%"), "To SL %": st.column_config.NumberColumn("To SL %", format="%.2f%%"), "To TP %": st.column_config.NumberColumn("To TP %", format="%.2f%%")})
    with st.expander(f"💰 Manage Positions — Close or Review ({len(positions)} open)", expanded=False):
        section_note("Stocks: cancels bracket orders first then closes. Crypto: closes via exchange. ⚠️ STUCK = exceeded 3 failed close attempts, needs manual close.")
        if "close_messages" not in st.session_state:
            st.session_state.close_messages = {}
        for trade_id, msg_data in st.session_state.close_messages.items():
            if msg_data["type"] == "success":
                notify_success(msg_data["msg"])
            else:
                notify_error(msg_data["msg"])
        for p in positions:
            pnl       = p.get("unrealized_pnl", 0)
            pnl_pct   = p.get("pnl_pct", 0)
            is_stuck  = p.get("is_stuck", False)
            pnl_color = "#4caf50" if pnl >= 0 else "#f44336"
            pnl_icon  = "🟢" if pnl >= 0 else "🔴"
            dir_color = "#4fc3f7" if p["direction"] == "long" else "#ff8a65"
            dir_icon  = "📈" if p["direction"] == "long" else "📉"
            sl = p.get("stop_loss", 0)
            tp = p.get("take_profit", 0)
            cur = p.get("current_price", 0)
            progress = max(0.0, min(1.0, (cur - sl) / (tp - sl))) if tp != sl else 0.5
            progress_pct   = int(progress * 100)
            progress_color = "#4caf50" if progress > 0.5 else "#ff8a65" if progress > 0.25 else "#f44336"
            border_color   = "#f59e0b" if is_stuck else "#2a3a5c"
            stuck_badge    = '<span style="background:#f59e0b;color:#000;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:bold;margin-left:8px;">⚠️ STUCK — needs manual close</span>' if is_stuck else ""
            card_col, btn_col = st.columns([5, 1])
            with card_col:
                st.markdown(f"""
                <div style="background:#1a1f35;border:1px solid {border_color};border-radius:10px;padding:14px 18px;margin-bottom:6px;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <span style="font-weight:bold;font-size:16px;">{p['symbol']} {'🌙' if p.get('is_overnight') else ''}{stuck_badge}</span>
                        <span style="color:{dir_color};font-weight:bold;">{dir_icon} {p['direction'].upper()}</span>
                        <span style="color:{pnl_color};font-weight:bold;font-size:15px;">{pnl_icon} ${pnl:+.2f} <small>({pnl_pct:+.2f}%)</small></span>
                    </div>
                    <div style="display:flex;gap:20px;font-size:12px;color:#90a4ae;margin-bottom:8px;">
                        <span>Entry: <b style="color:#e0e0e0">${p['entry_price']:.4f}</b></span>
                        <span>Current: <b style="color:#e0e0e0">${p['current_price']:.4f}</b></span>
                        <span>SL: <b style="color:#f44336">${p['stop_loss']:.4f}</b></span>
                        <span>TP: <b style="color:#4caf50">${p['take_profit']:.4f}</b></span>
                        <span>To SL: <b style="color:#e0e0e0">{p['distance_to_sl']:.2f}%</b></span>
                        <span>To TP: <b style="color:#e0e0e0">{p['distance_to_tp']:.2f}%</b></span>
                    </div>
                    <div style="background:#0f1117;border-radius:4px;height:6px;overflow:hidden;">
                        <div style="background:{progress_color};width:{progress_pct}%;height:100%;border-radius:4px;"></div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
            with btn_col:
                st.markdown("<div style='margin-top:18px'></div>", unsafe_allow_html=True)
                if st.button("💰 Close Now", key=f"cancel_{p['trade_id']}", width='stretch'):
                    trade_id = p["trade_id"]
                    db.set_state(f"close_stuck_{trade_id}", 0)
                    db.set_state(f"close_attempts_{trade_id}", 0)
                    trade_record = None
                    try:
                        open_trades = db.get_open_trades()
                        for t in open_trades:
                            if t["trade_id"] == trade_id:
                                trade_record = t
                                break
                    except Exception:
                        pass
                    if not trade_record:
                        st.session_state.close_messages[trade_id] = {"type": "error", "msg": f"Trade record not found for {p['symbol']} — try refreshing"}
                        st.cache_data.clear()
                        st.rerun()
                    current_price = p.get("current_price") or p.get("entry_price")
                    asset_class   = p.get("asset_class", "crypto")
                    if asset_class == "stock":
                        success, msg = close_stock_position(p["symbol"], trade_record, current_price)
                    else:
                        try:
                            from core.trade_executor import executor
                            success = executor.close_trade(trade_record, current_price, "manual_close")
                            if success:
                                pnl_amt = (current_price - trade_record["entry_price"]) * trade_record["quantity"]
                                if trade_record["direction"] == "short":
                                    pnl_amt = -pnl_amt
                                msg = f"Closed {p['symbol']} @ ${current_price:.4f} | P&L: ${pnl_amt:+.2f}"
                            else:
                                msg = f"Executor returned failure for {p['symbol']}"
                        except Exception as e:
                            success = False
                            msg = f"Crypto close error for {p['symbol']}: {e}"
                    st.session_state.close_messages[trade_id] = {"type": "success" if success else "error", "msg": msg}
                    st.cache_data.clear()
                    st.rerun()

            # ── Clickable chart per position ────────────────────────────────────────
            with st.expander(f"📊 {p['symbol']} — view chart", expanded=False):
                render_chart(
                    symbol      = p["symbol"],
                    asset_class = p.get("asset_class", "crypto"),
                    entry_price = float(p.get("entry_price") or 0) or None,
                    stop_loss   = float(p.get("stop_loss")   or 0) or None,
                    take_profit = float(p.get("take_profit") or 0) or None,
                    entry_time  = p.get("entry_time"),
                )
else:
    st.info("No open positions currently.")

# IBKR Gateway positions panel removed — caused duplicate clientId connections
# which forced stock trades to fall back to Alpaca. Monitor IBKR positions
# by stopping Gateway temporarily and logging into IBKR website instead.
# TODO: Implement properly using the bot's ib_insync event loop, not a
# separate dashboard connection.

# ----------------------------------------------------------
#  MANUAL TRADE ENTRY
# ----------------------------------------------------------
st.markdown("### ✍️ Manual Trade Entry")
section_note("Enter a trade you spotted yourself — bot submits to broker and manages it with trailing stops.")
with st.expander("➕ Open Manual Trade", expanded=False):
    mc1, mc2, mc3 = st.columns(3)
    with mc1:
        m_symbol    = st.text_input("Symbol", placeholder="RAVE/USD or SOXL")
        m_direction = st.selectbox("Direction", ["long", "short"])
        m_asset     = st.selectbox("Asset Class", ["stock", "crypto"])
    with mc2:
        m_size      = st.number_input("Position Size ($)", min_value=1.0, value=2000.0,
                                      help="Notional size. For leveraged Kraken shorts, margin required = size ÷ leverage.")
        _brokers = ["alpaca", "coinbase", "kraken", "paper"]
        if getattr(config, "IBKR_ENABLED", False):
            _brokers.insert(0, "ibkr")
        m_broker    = st.selectbox("Broker", _brokers)
        m_overnight = st.checkbox("🌙 Hold Overnight (!Day_Trade)", value=False, help="Stock will NOT be auto-closed at 15:45 ET.")
    with mc3:
        m_sl_pct = st.number_input("Stop Loss %", min_value=0.1, value=1.5, step=0.1)
        m_tp_pct = st.number_input("Take Profit %", min_value=0.1, value=3.0, step=0.1)
        m_strat  = st.text_input("Strategy Label", value="manual")

    # Leverage selector — only relevant for Kraken shorts
    # Shows available levels pulled from exchange_capabilities cache;
    # falls back to [1,2,3,5] if cache not warm yet.
    m_leverage = 1
    _kraken_max = getattr(config, "KRAKEN_MAX_LEVERAGE", 10)
    _kraken_default = getattr(config, "KRAKEN_SHORT_LEVERAGE", 2)
    if m_broker == "kraken" and m_direction == "short":
        try:
            from core.exchange_capabilities import exchange_capabilities as _ec
            _pair_max = _ec.get_max_short_leverage(
                (m_symbol.strip().upper() if m_symbol else "BTC/USD"), "kraken"
            )
        except Exception:
            _pair_max = _kraken_max
        _lev_choices = [v for v in [1, 2, 3, 4, 5] if v <= max(_pair_max, 1)]
        if not _lev_choices:
            _lev_choices = [1]
        _default_idx = _lev_choices.index(_kraken_default) if _kraken_default in _lev_choices else 0
        m_leverage = st.selectbox(
            "Leverage (Kraken short)",
            options=_lev_choices,
            index=_default_idx,
            help=(
                f"Overrides config KRAKEN_SHORT_LEVERAGE ({_kraken_default}×) for this trade. "
                f"Pair max: {_pair_max}×. "
                f"Margin required = Position Size ÷ Leverage. "
                f"Order notional = Position Size × (Leverage ÷ {_kraken_max})."
            ),
        )
        # Visual warning if leverage > 2 chosen
        if m_leverage > 2:
            st.warning(f"⚠️ {m_leverage}× leverage selected — higher risk. Losses scale with leverage.")

    m_entry = None
    if m_symbol:
        try:
            from scanners.market_scanner import scanner
            sym_upper  = m_symbol.strip().upper()
            # Try all exchanges, show which one found the price
            if m_asset == "crypto":
                try:
                    from scanners.market_scanner import scanner as _sc
                    crypto_fetcher = _sc.crypto_scanner
                    live_price, found_broker = crypto_fetcher.get_current_price_with_broker(sym_upper)
                except Exception:
                    live_price, found_broker = scanner.get_current_price(sym_upper, m_asset), None
            else:
                live_price, found_broker = scanner.get_current_price(sym_upper, m_asset), "ALPACA"
            if live_price:
                m_entry = live_price
                if m_direction == "long":
                    preview_sl = live_price * (1 - m_sl_pct / 100)
                    preview_tp = live_price * (1 + m_tp_pct / 100)
                else:
                    preview_sl = live_price * (1 + m_sl_pct / 100)
                    preview_tp = live_price * (1 - m_tp_pct / 100)
                broker_label = f" (found on {found_broker})" if found_broker else ""
                # Leverage sizing preview for Kraken shorts
                if m_broker == "kraken" and m_direction == "short" and m_leverage > 1:
                    _scale        = m_leverage / _kraken_max
                    _order_notional = m_size * _scale
                    _margin_req     = _order_notional / m_leverage
                    _order_qty      = _order_notional / live_price
                    lev_line = (
                        f"<br>📐 <b>Leverage:</b> <code>{m_leverage}×</code> &nbsp; "
                        f"Scale: <code>{_scale:.2f}</code> &nbsp; "
                        f"Order notional: <code>${_order_notional:.2f}</code> &nbsp; "
                        f"Margin req: <code>${_margin_req:.2f}</code> &nbsp; "
                        f"Qty: <code>{_order_qty:.6f}</code>"
                    )
                else:
                    lev_line = ""
                st.markdown(f"""
                <div style="background:#1a2a1a;border:1px solid #2a5c2a;border-radius:8px;padding:10px 14px;font-size:13px;margin-top:8px;">
                    📡 <b>Live Price{broker_label}:</b> <code>${live_price:.6f}</code><br>
                    🔴 <b>Stop Loss:</b> <code>${preview_sl:.6f}</code> &nbsp;&nbsp;
                    🟢 <b>Take Profit:</b> <code>${preview_tp:.6f}</code><br>
                    💰 <b>Position:</b> <code>{m_size/live_price:.4f} units @ ${live_price:.6f}</code>
                    {lev_line}
                </div>
                """, unsafe_allow_html=True)
            else:
                st.warning(f"Could not fetch live price for {sym_upper} on any exchange")
        except Exception as e:
            st.warning(f"Price lookup error: {e}")

    if st.button("🚀 Open Trade at Market Price", type="primary", width='stretch'):
        if not m_symbol:
            st.error("Please enter a symbol.")
        elif not m_entry:
            st.error(f"Could not get live price for {m_symbol}. Check symbol and try again.")
        else:
            try:
                import uuid, sqlite3
                from datetime import datetime as dt

                sym_upper = m_symbol.strip().upper()
                trade_id  = str(uuid.uuid4())[:12]
                qty       = m_size / m_entry

                if m_direction == "long":
                    sl = m_entry * (1 - m_sl_pct / 100)
                    tp = m_entry * (1 + m_tp_pct / 100)
                else:
                    sl = m_entry * (1 + m_sl_pct / 100)
                    tp = m_entry * (1 - m_tp_pct / 100)

                side = "buy" if m_direction == "long" else "sell"
                broker_order_id   = None
                actual_fill_price = m_entry

                if m_broker == "ibkr" and m_asset == "stock":
                    try:
                        from core.trade_executor import executor
                        if executor._ibkr and executor._ibkr.is_available():
                            submit_qty = int(qty) if qty >= 1 else qty
                            result = executor._ibkr.submit_order(
                                symbol      = sym_upper,
                                qty         = submit_qty,
                                side        = side,
                                stop_loss   = round(sl, 2),
                                take_profit = round(tp, 2)
                            )
                            if result:
                                broker_order_id   = result.get("broker_order_id")
                                fill_price        = result.get("filled_avg_price", 0)
                                actual_fill_price = fill_price if fill_price and fill_price > 0 else m_entry
                            else:
                                notify_error(f"IBKR rejected the order for {sym_upper}. Check TWS is running.")
                                st.stop()
                        else:
                            notify_error("IBKR is not available. Check TWS is running on port 7497.")
                            st.stop()
                    except Exception as e:
                        notify_error(f"IBKR order error: {e}")
                        st.stop()

                elif m_broker == "alpaca" and m_asset == "stock":
                    from core.trade_executor import AlpacaExecutor
                    alpaca_ex  = AlpacaExecutor()
                    submit_qty = int(qty) if qty >= 1 else qty
                    result = alpaca_ex.submit_order(
                        symbol      = sym_upper,
                        qty         = submit_qty,
                        side        = side,
                        stop_loss   = round(sl, 2),
                        take_profit = round(tp, 2)
                    )
                    if result is None:
                        notify_error(f"Alpaca rejected the order for {sym_upper}. Check the bot log for the exact error.")
                        st.stop()
                    broker_order_id   = result.get("broker_order_id")
                    fill_price        = result.get("filled_avg_price", 0)
                    actual_fill_price = fill_price if fill_price and fill_price > 0 else m_entry
                    dash_logger.info(f"Manual stock trade: {sym_upper} {side} qty={submit_qty} | order_id={broker_order_id} | fill=${actual_fill_price:.4f}")

                elif m_broker == "kraken" and m_asset == "crypto":
                    from core.kraken_executor import KrakenExecutor
                    import config as _cfg
                    kraken_ex = KrakenExecutor(
                        api_key    = _cfg.KRAKEN_API_KEY,
                        api_secret = _cfg.KRAKEN_SECRET_KEY,
                        paper      = getattr(_cfg, "KRAKEN_PAPER_MODE", True),
                    )
                    # Apply leverage scaling for short orders.
                    # m_leverage is 1 for longs (no scaling); >1 for margin shorts.
                    # scale = desired_lev / kraken_max_lev  (e.g. 2/10 = 0.20)
                    _ref_max   = getattr(_cfg, "KRAKEN_MAX_LEVERAGE", 10)
                    _lev_scale = (m_leverage / _ref_max) if m_leverage > 1 else 1.0
                    submit_qty = round(qty * _lev_scale, 8)
                    result = kraken_ex.submit_order(
                        symbol      = sym_upper,
                        qty         = submit_qty,
                        side        = side,
                        stop_loss   = round(sl, 6),
                        take_profit = round(tp, 6),
                        leverage    = m_leverage if m_leverage > 1 else 0,
                    )
                    if result is None:
                        notify_error(f"Kraken rejected the order for {sym_upper}. Check bot log.")
                        st.stop()
                    broker_order_id   = result.get("broker_order_id")
                    fill_price        = result.get("filled_avg_price", 0)
                    actual_fill_price = fill_price if fill_price and fill_price > 0 else m_entry
                    # For leveraged shorts, record position_value as the scaled notional
                    qty = submit_qty   # keep qty consistent with what was actually submitted
                    dash_logger.info(
                        f"Manual Kraken {'[PAPER] ' if getattr(_cfg, 'KRAKEN_PAPER_MODE', True) else ''}"
                        f"trade: {sym_upper} {side} lev={m_leverage}x qty={submit_qty:.6f} "
                        f"| order_id={broker_order_id} | fill=${actual_fill_price:.6f}"
                    )

                if m_direction == "long":
                    sl  = actual_fill_price * (1 - m_sl_pct / 100)
                    tp  = actual_fill_price * (1 + m_tp_pct / 100)
                else:
                    sl  = actual_fill_price * (1 + m_sl_pct / 100)
                    tp  = actual_fill_price * (1 - m_tp_pct / 100)
                # For leveraged Kraken shorts the qty was already scaled in the
                # broker branch — don't overwrite it with the full notional qty.
                if not (m_broker == "kraken" and m_leverage > 1):
                    qty = m_size / actual_fill_price

                conn = sqlite3.connect(config.DB_PATH)
                conn.execute("""
                    INSERT INTO trades
                        (trade_id, symbol, asset_class, direction, status,
                         entry_time, entry_price, quantity, position_value,
                         stop_loss, take_profit, pnl, pnl_pct,
                         signal_score, broker, strategy_name)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,1.0,?,?)
                """, (
                    trade_id, sym_upper, m_asset, m_direction, "open",
                    dt.now().isoformat(),
                    round(actual_fill_price, 6),
                    round(qty, 6),
                    round(m_size, 2),
                    round(sl, 6),
                    round(tp, 6),
                    m_broker, m_strat
                ))
                try:
                    conn.execute("UPDATE trades SET is_overnight=?, broker_order_id=? WHERE trade_id=?", (1 if m_overnight else 0, broker_order_id, trade_id))
                except Exception:
                    pass
                conn.commit()
                conn.close()
                notify_success(f"{m_direction.upper()} {sym_upper} opened @ ${actual_fill_price:.6f} | SL: ${sl:.6f} | TP: ${tp:.6f} | Qty: {qty:.4f} | Broker: {m_broker}")
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                notify_error(f"Trade entry error: {e}")

# ----------------------------------------------------------
#  MANUAL WATCHLIST INJECTION
# ----------------------------------------------------------
st.markdown("### 🔭 Inject Symbols Into Today's Watchlist")
section_note("Spotted something? Add it here — temporary for today only. Resets on next scan or reboot. To make permanent, edit the master watchlist files.")
with st.expander("➕ Inject Stocks or Crypto", expanded=False):
    inj_col1, inj_col2 = st.columns(2)

    with inj_col1:
        st.markdown("**📈 Stocks**")
        stock_input = st.text_area(
            "Tickers (one per line or comma-separated)",
            height=100,
            key="manual_stock_inject",
            placeholder="AAPL\nMSFT\nNVDA",
        )
        if st.button("Inject Stocks", key="inject_stocks_btn", width='stretch'):
            raw     = stock_input.replace(",", "\n")
            tickers = [t.strip().upper() for t in raw.splitlines() if t.strip()]
            if tickers:
                temp_file = Path("watchlist/scanned_stocks.txt")
                temp_file.parent.mkdir(parents=True, exist_ok=True)
                existing = []
                if temp_file.exists():
                    existing = [t for t in temp_file.read_text().splitlines()
                                if t.strip() and t.strip() not in tickers]
                temp_file.write_text("\n".join(tickers + existing))
                notify_success(f"Injected {len(tickers)} stock(s): {', '.join(tickers)}")
                st.caption("Bot will pick these up on next scan cycle. Restart bot to load immediately.")
            else:
                st.warning("No valid tickers entered.")

    with inj_col2:
        st.markdown("**🪙 Crypto**")
        crypto_input = st.text_area(
            "Symbols (e.g. BTC or BTC/USD, one per line or comma-separated)",
            height=100,
            key="manual_crypto_inject",
            placeholder="BTC\nETH\nSOL",
        )
        if st.button("Inject Crypto", key="inject_crypto_btn", width='stretch'):
            raw     = crypto_input.replace(",", "\n")
            symbols = [s.strip().upper() for s in raw.splitlines() if s.strip()]
            pairs   = [s if "/" in s else f"{s}/USD" for s in symbols]
            if pairs:
                temp_file = Path("watchlist/scanned_crypto.txt")
                temp_file.parent.mkdir(parents=True, exist_ok=True)
                existing = []
                if temp_file.exists():
                    existing = [p for p in temp_file.read_text().splitlines()
                                if p.strip() and p.strip() not in pairs]
                temp_file.write_text("\n".join(pairs + existing))
                notify_success(f"Injected {len(pairs)} crypto pair(s): {', '.join(pairs)}")
                st.caption("Bot will pick these up on next scan cycle. Restart bot to load immediately.")
            else:
                st.warning("No valid symbols entered.")

    # Show current temp watchlist contents
    stocks_file = Path("watchlist/scanned_stocks.txt")
    crypto_file = Path("watchlist/scanned_crypto.txt")
    has_stocks  = stocks_file.exists() and stocks_file.read_text().strip()
    has_crypto  = crypto_file.exists() and crypto_file.read_text().strip()

    if has_stocks or has_crypto:
        st.markdown("---")
        st.markdown("**Currently active injected symbols:**")
        if has_stocks:
            tickers = [t for t in stocks_file.read_text().splitlines() if t.strip()]
            st.info(f"📈 Stocks ({len(tickers)}): {', '.join(tickers)}")
        if has_crypto:
            pairs = [p for p in crypto_file.read_text().splitlines() if p.strip()]
            st.info(f"🪙 Crypto ({len(pairs)}): {', '.join(pairs)}")

        if st.button("🗑️ Clear All Injected Symbols", key="clear_injected_btn"):
            if stocks_file.exists():
                stocks_file.write_text("")
            if crypto_file.exists():
                crypto_file.write_text("")
            notify_success("Injected symbols cleared.")
            st.rerun()

# ----------------------------------------------------------
#  TODAY'S TRADE LOG
# ----------------------------------------------------------
st.markdown("### 📋 Today's Trade Log")
section_note("Newest close first.")
closed_today = [t for t in today_trades if t["status"] == "closed"]
if closed_today:
    log_rows = []
    for t in closed_today:
        pnl     = t.get("pnl", 0) or 0
        pnl_pct = t.get("pnl_pct", 0) or 0
        raw_exit  = t.get("exit_time", "") or ""
        raw_entry = t.get("entry_time", "") or ""
        exit_str, exit_dt = fmt_db_time(raw_exit)
        entry_str, entry_dt = fmt_db_time(raw_entry)
        entry_dt, exit_dt = normalize_entry_exit(entry_dt, exit_dt)
        if entry_dt is not None and entry_dt != pd.Timestamp.min:
            entry_str = entry_dt.strftime("%m/%d %H:%M PDT")
        if exit_dt is not None and exit_dt != pd.Timestamp.min:
            exit_str = exit_dt.strftime("%m/%d %H:%M PDT")
        try:
            # Calculate duration
            duration_mins  = int((exit_dt - entry_dt).total_seconds() / 60)
            if duration_mins < 0:
                duration_str = "—"
            elif duration_mins >= 60:
                duration_str = f"{duration_mins // 60}h {duration_mins % 60}m"
            else:
                duration_str = f"{duration_mins}m"
        except Exception:
            duration_str = "—"
        log_rows.append({"_exit_dt": exit_dt, "Opened": entry_str, "Closed": exit_str, "Duration": duration_str, "Symbol": t["symbol"], "Dir": t["direction"].upper(), "Strategy": t.get("strategy_name", "original"), "Entry $": float(t.get("entry_price", 0) or 0), "Exit $": float(t.get("exit_price", 0) or 0), "Reason": t.get("exit_reason", "").replace("_", " ").title(), "P&L $": float(pnl), "P&L %": float(pnl_pct), "Result": "✅ Win" if pnl > 0 else "❌ Loss"})
    df_log = pd.DataFrame(log_rows).sort_values("_exit_dt", ascending=False).reset_index(drop=True).drop(columns=["_exit_dt"])
    st.dataframe(df_log, width='stretch', hide_index=True, column_config={"Opened": st.column_config.TextColumn("Opened", width="small"), "Closed": st.column_config.TextColumn("Closed", width="small"), "Duration": st.column_config.TextColumn("Duration", width="small"), "Symbol": st.column_config.TextColumn("Symbol", width="small"), "Dir": st.column_config.TextColumn("Dir", width="small"), "Strategy": st.column_config.TextColumn("Strategy", width="medium"), "Entry $": st.column_config.NumberColumn("Entry $", format="$%.4f"), "Exit $": st.column_config.NumberColumn("Exit $", format="$%.4f"), "Reason": st.column_config.TextColumn("Reason", width="medium"), "P&L $": st.column_config.NumberColumn("P&L $", format="$%+.2f"), "P&L %": st.column_config.NumberColumn("P&L %", format="%+.2f%%"), "Result": st.column_config.TextColumn("Result", width="small")})
else:
    st.info("No closed trades today.")
st.divider()

# ----------------------------------------------------------
#  RECENT PERFORMANCE TABLE
# ----------------------------------------------------------
st.markdown("### 📅 Recent Daily Performance")
if summaries:
    perf_data = []
    for s in summaries[:14]:
        perf_data.append({"Date": s["trade_date"], "P&L $": f"${s['daily_pnl']:+.2f}", "P&L %": f"{s['daily_pnl_pct']:+.2f}%", "Trades": s["total_trades"], "W/L": f"{s['winning_trades']}W / {s['losing_trades']}L", "Win Rate": f"{s['win_rate']:.1f}%", "Capital": f"${s['ending_capital']:,.2f}", "Goal Met": "✅" if s.get("goal_met") else "❌", "Halted": "⚠️" if s.get("trading_halted") else ""})
    st.dataframe(pd.DataFrame(perf_data), width='stretch', hide_index=True)
else:
    st.info("No daily summaries yet.")
st.divider()

# ===========================================================================
#  BACKTEST PANEL
# ===========================================================================
st.markdown("### 🔬 Strategy Backtester")
section_note("Test any strategy on historical data. Compare Standard vs 2-Bar Trailing stop to find the best exit approach.")
bt_c1, bt_c2, bt_c3, bt_c4 = st.columns(4)
with bt_c1:
    bt_asset = st.selectbox("Asset Class", ["Stocks", "Crypto"], index=0)
    if bt_asset == "Stocks":
        all_symbols = ["ALL"] + sorted(config.STOCK_WATCHLIST)
    else:
        try:
            with open("watchlists/crypto.txt") as f:
                crypto_list = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        except Exception:
            crypto_list = []
        all_symbols = ["ALL"] + sorted(crypto_list)
    bt_symbol = st.selectbox("Symbol", all_symbols, index=0)
with bt_c2:
    strategy_names = ["ALL strategies", "original_scorer", "rsi_momentum", "bollinger_breakout", "ema_crossover", "mean_reversion", "scalp_master", "swing_trader", "grid_bot", "dca_accumulator", "vwap_momentum", "hammer_reversal", "orb_breakout"]
    bt_strategy = st.selectbox("Strategy", strategy_names, index=0)
with bt_c3:
    bt_tf = st.selectbox("Timeframe", ["1h", "1d", "5m"], index=0, help="1h = up to 730 days | 1d = up to 10 years | 5m = max 30 days")
    max_days_map = {"5m": 30, "1h": 730, "1d": 3650}
    max_days     = max_days_map.get(bt_tf, 730)
    day_options  = [d for d in [30, 60, 90, 180, 365, 730, 1460] if d <= max_days]
    default_idx  = min(2, len(day_options) - 1)
    bt_days      = st.selectbox("Duration", day_options, index=default_idx, format_func=lambda x: f"{x}d ({x//30}mo)" if x >= 30 else f"{x}d")
with bt_c4:
    bt_capital = st.number_input("Starting Capital ($)", min_value=100.0, value=float(config.STARTING_CAPITAL), step=1000.0)
    st.markdown("&nbsp;")
    run_bt = st.button("🚀 Run Backtest", type="primary", width='stretch')

# Stop loss mode row
bt_stop_c1, bt_stop_c2, bt_stop_c3 = st.columns([2, 1, 3])
with bt_stop_c1:
    bt_stop_mode = st.radio(
        "Stop Loss Mode",
        options=["Standard Stop Loss %", "2-Bar Trailing Stop"],
        index=0,
        horizontal=True,
        help="Standard: fixed % stop from strategy config. 2-Bar: places stop at lowest low of prior N bars, trails up on new N-bar highs. Never moves down."
    )
with bt_stop_c2:
    bt_lookback = st.number_input(
        "Lookback Bars (N)",
        min_value=1, max_value=20, value=2, step=1,
        help="N bars for 2-bar trailing stop. Default 2. Higher = wider stop, slower trail. Only active in 2-Bar mode.",
        disabled=(bt_stop_mode == "Standard Stop Loss %")
    )
with bt_stop_c3:
    if bt_stop_mode == "2-Bar Trailing Stop":
        notify_info(f"📏 2-Bar Stop (N={bt_lookback}): initial stop = lowest low of prior {bt_lookback} bars. Trails up on each new {bt_lookback}-bar high. Ratchets up only — never down. Fills at stop price on exit.")
    else:
        st.markdown("<div style='padding-top:8px;color:#90a4ae;font-size:13px;'>Fixed % stop from strategy config with trailing. Standard bot behavior unchanged.</div>", unsafe_allow_html=True)

bt_stop_mode_val = "two_bar" if bt_stop_mode == "2-Bar Trailing Stop" else "standard"

if bt_tf == "5m":
    notify_info("5m timeframe: max 30 days. Best for Scalp Master and ORB strategies.")
elif bt_tf == "1d":
    notify_info("Daily timeframe: signals are infrequent — best for long multi-year views.")
if run_bt:
    try:
        from intelligence.backtester import Backtester
        import numpy as np
        bt = Backtester(starting_capital=bt_capital)
        if bt_asset == "Crypto":
            if bt_symbol == "ALL":
                try:
                    with open("watchlists/crypto.txt") as f:
                        symbols_to_test = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                except Exception:
                    symbols_to_test = []
            else:
                symbols_to_test = [bt_symbol]
            data_source = "ccxt"
        else:
            symbols_to_test = [s for s in config.STOCK_WATCHLIST if "/" not in s and "-" not in s] if bt_symbol == "ALL" else [bt_symbol]
            data_source = "yfinance"
        strategies_to_run = ["original_scorer", "rsi_momentum", "bollinger_breakout", "ema_crossover", "mean_reversion", "scalp_master", "swing_trader", "grid_bot", "dca_accumulator", "vwap_momentum", "hammer_reversal", "orb_breakout"] if bt_strategy == "ALL strategies" else [bt_strategy]
        if not symbols_to_test:
            notify_info("No valid symbols selected.")
        else:
            all_results  = []
            progress_bar = st.progress(0)
            status_text  = st.empty()
            total_runs   = len(symbols_to_test) * len(strategies_to_run)
            run_count    = 0
            skip_count   = 0
            for strat in strategies_to_run:
                for sym in symbols_to_test:
                    stop_label = f" [{bt_stop_mode_val} N={bt_lookback}]" if bt_stop_mode_val == "two_bar" else ""
                    status_text.text(f"Testing {strat} on {sym} ({bt_tf}, {bt_days}d){stop_label}…")
                    try:
                        if data_source == "ccxt":
                            result = bt.run(sym, bt_days, bt_capital, bt_tf, asset_class="crypto", stop_mode=bt_stop_mode_val, two_bar_lookback=bt_lookback) if strat == "original_scorer" else bt.run_strategy(sym, strat, bt_days, bt_capital, bt_tf, asset_class="crypto", stop_mode=bt_stop_mode_val, two_bar_lookback=bt_lookback)
                        else:
                            result = bt.run(sym, bt_days, bt_capital, bt_tf, stop_mode=bt_stop_mode_val, two_bar_lookback=bt_lookback) if strat == "original_scorer" else bt.run_strategy(sym, strat, bt_days, bt_capital, bt_tf, stop_mode=bt_stop_mode_val, two_bar_lookback=bt_lookback)
                        if result and result.total_trades > 0:
                            all_results.append((strat, sym, result))
                        else:
                            skip_count += 1
                    except Exception:
                        skip_count += 1
                    run_count += 1
                    progress_bar.progress(run_count / total_runs)
            progress_bar.empty()
            status_text.empty()
            if skip_count > 0 and not all_results:
                notify_error(f"No results — {skip_count} symbol(s) had insufficient data for {bt_days}d on {bt_tf}.")
            if all_results:
                stop_display = f"2-Bar Trailing (N={bt_lookback})" if bt_stop_mode_val == "two_bar" else "Standard Stop %"
                notify_success(f"Backtest complete — {len(all_results)} result(s) | Stop mode: {stop_display}")
                if len(all_results) > 1:
                    avg_ret = np.mean([r.total_return_pct for _, _, r in all_results])
                    avg_pf  = np.mean([r.profit_factor    for _, _, r in all_results])
                    avg_dd  = np.mean([r.max_drawdown_pct for _, _, r in all_results])
                    avg_sh  = np.mean([r.sharpe_ratio     for _, _, r in all_results])
                    total_w = sum(r.winning_trades for _, _, r in all_results)
                    total_l = sum(r.losing_trades  for _, _, r in all_results)
                    overall_wr = total_w / (total_w + total_l) * 100 if (total_w + total_l) else 0
                    st.markdown("#### 📊 Portfolio Summary")
                    pa1, pa2, pa3, pa4, pa5 = st.columns(5)
                    pa1.metric("Overall Win Rate",  f"{overall_wr:.1f}%")
                    pa2.metric("Avg Profit Factor", f"{avg_pf:.2f}")
                    pa3.metric("Avg Return",        f"{avg_ret:+.2f}%")
                    pa4.metric("Avg Max Drawdown",  f"{avg_dd:.2f}%")
                    pa5.metric("Avg Sharpe",        f"{avg_sh:.2f}")
                    st.markdown("---")
                summary_rows = []
                for strategy, sym, r in all_results:
                    approved = r.win_rate >= 50 and r.profit_factor >= 1.2
                    summary_rows.append({"Symbol": sym, "Strategy": strategy, "Stop": "2-Bar" if r.stop_mode == "two_bar" else "Std%", "Period": f"{r.start_date} → {r.end_date}", "Start $": f"${r.starting_capital:,.2f}", "End $": f"${r.ending_capital:,.2f}", "Return %": f"{r.total_return_pct:+.2f}%", "Win Rate": f"{r.win_rate:.1f}%", "Trades": r.total_trades, "W/L": f"{r.winning_trades}W/{r.losing_trades}L", "Profit Factor": f"{r.profit_factor:.2f}", "Max Drawdown": f"{r.max_drawdown_pct:.2f}%", "Sharpe": f"{r.sharpe_ratio:.2f}", "Sortino": f"{r.sortino_ratio:.2f}", "Verdict": "✅ GO" if approved else "⚠️ TUNE"})
                st.markdown("**📋 Results Table**")
                st.dataframe(pd.DataFrame(summary_rows), width='stretch', hide_index=True)
                chart_result = None
                chart_label  = ""
                if len(all_results) == 1:
                    _, sym, chart_result = all_results[0]
                    chart_label = f"{sym} — {all_results[0][0]}"
                else:
                    st.markdown("---")
                    chart_options = [f"{sym} / {strat}" for strat, sym, _ in all_results]
                    chosen     = st.selectbox("Charts for", chart_options, label_visibility="collapsed")
                    chosen_idx = chart_options.index(chosen)
                    _, sym, chart_result = all_results[chosen_idx]
                    chart_label = chosen
                if chart_result and chart_result.equity_curve:
                    eq_curve = chart_result.equity_curve
                    step     = max(1, len(eq_curve) // 500)
                    sampled  = eq_curve[::step]
                    timestamps  = [ep.timestamp[:16] for ep in sampled]
                    equities    = [ep.equity for ep in sampled]
                    drawdowns   = [ep.drawdown for ep in sampled]
                    bar_returns = [ep.daily_ret for ep in sampled]
                    r = chart_result
                    stop_label_chart = f"2-Bar N={bt_lookback}" if r.stop_mode == "two_bar" else "Standard %"
                    st.markdown(f"#### 📈 Equity Charts — {chart_label} | Stop: {stop_label_chart}")
                    s1, s2, s3, s4, s5, s6 = st.columns(6)
                    s1.metric("Total Return",  f"{r.total_return_pct:+.2f}%")
                    s2.metric("Final Equity",  f"${r.ending_capital:,.2f}")
                    s3.metric("Max Drawdown",  f"{r.max_drawdown_pct:.2f}%")
                    s4.metric("Sharpe Ratio",  f"{r.sharpe_ratio:.2f}")
                    s5.metric("Sortino Ratio", f"{r.sortino_ratio:.2f}")
                    s6.metric("Profit Factor", f"{r.profit_factor:.2f}")
                    _PAPER="#1e293b"; _GRID="#334155"; _PURPLE="#8b5cf6"; _GREEN="#10b981"; _RED="#ef4444"; _TEXT="#94a3b8"
                    _layout = dict(paper_bgcolor=_PAPER, plot_bgcolor=_PAPER, font=dict(color=_TEXT, size=11), margin=dict(l=10, r=10, t=40, b=40), xaxis=dict(gridcolor=_GRID, showgrid=True, tickangle=-30, tickfont=dict(size=9)), yaxis=dict(gridcolor=_GRID, showgrid=True, fixedrange=False), hovermode="x unified")
                    fig_eq = go.Figure()
                    fig_eq.add_trace(go.Scatter(x=timestamps, y=equities, mode="lines", name="Portfolio Value", line=dict(color=_PURPLE, width=2), fill="tozeroy", fillcolor="rgba(139,92,246,0.12)", hovertemplate="<b>%{x}</b><br>$%{y:,.2f}<extra></extra>"))
                    fig_eq.add_hline(y=r.starting_capital, line_dash="dot", line_color="#475569", annotation_text=f"Start ${r.starting_capital:,.0f}", annotation_font_color=_TEXT)
                    fig_eq.update_layout(**_layout, title=dict(text="Equity Curve", font=dict(color=_TEXT, size=13)), height=300, showlegend=False)
                    fig_eq.update_yaxes(tickprefix="$", tickformat=",.0f")
                    st.plotly_chart(fig_eq, width='stretch')
                    ch2, ch3 = st.columns(2)
                    with ch2:
                        fig_dd = go.Figure()
                        fig_dd.add_trace(go.Scatter(x=timestamps, y=drawdowns, mode="lines", name="Drawdown", line=dict(color=_RED, width=1.5), fill="tozeroy", fillcolor="rgba(239,68,68,0.15)", hovertemplate="<b>%{x}</b><br>%{y:.2f}%<extra></extra>"))
                        fig_dd.update_layout(**_layout, title=dict(text="Drawdown %", font=dict(color=_TEXT, size=13)), height=260, showlegend=False)
                        fig_dd.update_yaxes(ticksuffix="%", autorange="reversed")
                        st.plotly_chart(fig_dd, width='stretch')
                    with ch3:
                        bar_colors = [_GREEN if v >= 0 else _RED for v in bar_returns]
                        fig_ret = go.Figure()
                        fig_ret.add_trace(go.Bar(x=timestamps, y=bar_returns, name="Bar Return", marker_color=bar_colors, opacity=0.75, hovertemplate="<b>%{x}</b><br>%{y:.3f}%<extra></extra>"))
                        fig_ret.add_hline(y=0, line_color="#475569", line_width=0.8)
                        fig_ret.update_layout(**_layout, title=dict(text="Returns per Bar", font=dict(color=_TEXT, size=13)), height=260, showlegend=False, bargap=0.1)
                        fig_ret.update_yaxes(ticksuffix="%")
                        st.plotly_chart(fig_ret, width='stretch')
                if len(all_results) == 1:
                    _, _, r = all_results[0]
                    if r.trades:
                        with st.expander(f"📋 Trade Log ({len(r.trades)} trades)", expanded=False):
                            trade_rows = []
                            for t in r.trades:
                                trade_rows.append({"Entry": t.entry_date[:10], "Exit": t.exit_date[:10], "Dir": t.direction.upper(), "Entry $": f"${t.entry_price:.2f}", "Exit $": f"${t.exit_price:.2f}", "P&L $": f"${t.pnl:+.2f}", "P&L %": f"{t.pnl_pct:+.2f}%", "Reason": t.exit_reason.replace("_", " ").title(), "Bars": t.bars_held, "Score": f"{t.signal_score:.2f}", "Strategy": t.strategy_name, "Stop": t.stop_mode, "Result": "✅" if t.pnl > 0 else "❌"})
                            st.dataframe(pd.DataFrame(trade_rows), width='stretch', hide_index=True)
    except ImportError:
        notify_error("yfinance not installed. Run: pip install yfinance --break-system-packages")
    except Exception as e:
        notify_error(f"Backtest error: {e}")
        import traceback
        st.code(traceback.format_exc())
st.divider()

# ===========================================================================
#  CHAT INTERFACE
# ===========================================================================
st.markdown("### 🤖 Chat with Your Bot")
section_note("Ask anything in plain English — pause trading, check stats, manage strategies, or just ask how you're doing.")
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
if prompt := st.chat_input("Ask your bot anything…"):
    st.session_state.chat_history.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            response = chat.chat(prompt)
        st.markdown(response)
        st.session_state.chat_history.append({"role": "assistant", "content": response})

# ===========================================================================
#  SIDEBAR
# ===========================================================================
with st.sidebar:
    st.markdown("## ⚙️ Quick Controls")
    st.markdown("**Risk Settings**")
    current_pos_pct = int(config.MAX_POSITION_PCT)
    new_pos_pct = st.slider("Max Position %", 1, 5, current_pos_pct)
    if new_pos_pct != current_pos_pct:
        config.MAX_POSITION_PCT = float(new_pos_pct)
        st.success(f"Position size: {new_pos_pct}%")
    st.divider()
    st.markdown("**🤖 Bot Process Control**")
    import subprocess, sys, os
    def is_bot_running():
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = ' '.join(proc.info['cmdline'] or [])
                    if 'bot_engine' in cmdline and 'python' in proc.info['name'].lower():
                        return True, proc.info['pid']
                except Exception:
                    pass
            return False, None
        except ImportError:
            return None, None
    bot_running, bot_pid = is_bot_running()
    if bot_running is None:
        st.caption("Install psutil: pip install psutil")
        bot_status_label = "⚪ Status Unknown"
    elif bot_running:
        bot_status_label = f"🟢 Bot Running (PID {bot_pid})"
    else:
        bot_status_label = "🔴 Bot Not Running"
    st.markdown(f"**{bot_status_label}**")
    b1, b2 = st.columns(2)
    with b1:
        if st.button("🔄 Restart Bot", width='stretch', type="primary"):
            try:
                import psutil
                if bot_running and bot_pid:
                    try:
                        psutil.Process(bot_pid).terminate()
                    except Exception:
                        pass
                subprocess.Popen([sys.executable, "bot_engine.py"], cwd=os.path.dirname(os.path.abspath(__file__)), creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0)
                st.success("✅ Bot restarting...")
                st.rerun()
            except Exception as e:
                st.error(f"Restart error: {e}")
    with b2:
        if bot_running:
            if st.button("🛑 Stop Bot", width='stretch', type="secondary"):
                try:
                    psutil.Process(bot_pid).terminate()
                    st.success("Bot stopped.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Stop error: {e}")
        else:
            if st.button("▶️ Start Bot", width='stretch', type="primary"):
                try:
                    subprocess.Popen([sys.executable, "bot_engine.py"], cwd=os.path.dirname(os.path.abspath(__file__)), creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0)
                    st.success("✅ Bot started!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Start error: {e}")
    st.divider()
    st.markdown("**📊 Trading Control**")
    if session["trading_active"]:
        if st.button("⏸️ Pause Trading", width='stretch', type="secondary"):
            risk_manager.manual_halt("Paused via dashboard")
            st.rerun()
    else:
        if st.button("▶️ Resume Trading", width='stretch', type="primary"):
            risk_manager.manual_resume()
            st.rerun()
    st.caption("Pause stops new trades but keeps bot running.")
    st.divider()
    st.markdown("**Tax & Reports**")
    if st.button("📄 Generate Today's Report", width='stretch'):
        with st.spinner("Generating..."):
            summary = report_generator.generate_daily_summary()
            db.save_daily_summary(summary)
        st.success("Report generated!")
    year = st.number_input("Tax Year", value=datetime.now().year, step=1)
    if st.button("📊 Export Form 8949 CSV", width='stretch'):
        filepath = f"exports/tax_form_8949_{int(year)}.csv"
        os.makedirs("exports", exist_ok=True)
        db.export_8949_csv(int(year), filepath)
        st.success(f"Saved: {filepath}")
    st.divider()
    st.markdown("**Withdrawal**")
    withdraw_amount = st.number_input("Amount ($)", min_value=0.0, step=10.0)
    withdraw_reason = st.text_input("Reason", value="Living expenses")
    if st.button("💸 Process Withdrawal", width='stretch'):
        if withdraw_amount > 0:
            result = risk_manager.process_withdrawal(withdraw_amount, withdraw_reason)
            if result["approved"]:
                st.success(result["message"])
                st.cache_data.clear()
            else:
                st.error(result["message"])
    st.divider()
    tax = db.get_tax_year_summary(datetime.now().year)
    st.markdown(f"**YTD Tax Summary ({datetime.now().year})**")
    st.metric("Total Gains/Losses", f"${float(tax.get('total_gains') or 0):,.2f}")
    st.metric("Short-Term", f"${float(tax.get('short_term_gains') or 0):,.2f}")
    st.metric("Long-Term",  f"${float(tax.get('long_term_gains') or 0):,.2f}")
    st.divider()
    st.markdown("**🤖 ML Scorer**")
    try:
        from intelligence.ml_scorer import ml_scorer
        ml_status  = ml_scorer.get_status()
        stage_icon = {"warming_up": "🟡", "learning": "🔵", "active": "🟢"}.get(ml_status["stage"], "⚪")
        st.markdown(f"{stage_icon} **{ml_status['stage'].replace('_', ' ').title()}**")
        st.progress(ml_status["progress_pct"] / 100)
        st.caption(ml_status["message"])
        if ml_status["stage"] == "warming_up":
            st.caption(f"{ml_status['trade_count']} / {ml_status['min_to_activate']} trades collected")
        if st.button("🔄 Retrain ML Now", width='stretch'):
            with st.spinner("Training..."):
                ml_scorer.retrain()
            st.success("Retrain complete!")
    except Exception:
        st.caption("ML scorer initializing...")
    st.divider()
    st.markdown("**📊 Market Condition**")
    try:
        from intelligence.condition_detector import condition_detector
        from scanners.market_scanner import scanner
        cond = condition_detector.get_spy_condition(scanner.stock_scanner)
        cond_icons = {"trending_up": "📈 Trending Up", "trending_down": "📉 Trending Down", "ranging": "↔️ Ranging", "volatile": "⚡ Volatile", "unknown": "❓ Unknown"}
        label = cond_icons.get(cond.condition.value, "❓ Unknown")
        st.markdown(f"**{label}**")
        st.caption(f"ADX: {cond.adx} | Scalar: {cond.position_scalar:.0%}")
        st.caption(cond.reason)
        if not cond.should_trade:
            st.warning("Conditions unfavorable — bot is pausing new entries")
    except Exception:
        st.caption("Condition detector initializing...")
