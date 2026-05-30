"""
db_panel.py
===========
Database Browser & Query Panel for trading_bot_v2.

Launch:   python db_panel.py
Shortcut: db-panel  (defined in bot_functions.ps1)
"""

import sqlite3
import time
import re
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "trading_bot.db"

# ── palette ──────────────────────────────────────────────────────────────────
BG       = "#1a1a2e"
PANEL_BG = "#16213e"
BTN_BG   = "#0f3460"
BTN_ACT  = "#e94560"
FG       = "#e0e0e0"
FG_DIM   = "#888"
GRID_BG  = "#0d1b2a"
GRID_ALT = "#111f30"
GRID_SEL = "#1e4d78"
HEADER   = "#2a4a7f"
GREEN    = "#00c896"
RED      = "#e94560"
YELLOW   = "#f0c040"
ORANGE   = "#f08030"

# ── quick queries ─────────────────────────────────────────────────────────────

QUICK_QUERIES = [
    ("Open Today",       "trades",
     """SELECT trade_id,symbol,direction,status,strategy_name,
               entry_time,entry_price,stop_loss,take_profit,position_value,broker
        FROM trades
        WHERE status='open' AND DATE(entry_time)=DATE('now','localtime')
        ORDER BY entry_time DESC"""),

    ("Closed Today",     "trades",
     """SELECT trade_id,symbol,direction,status,strategy_name,
               entry_time,exit_time,entry_price,exit_price,pnl,pnl_pct,exit_reason,broker
        FROM trades
        WHERE status='closed' AND DATE(exit_time)=DATE('now','localtime')
        ORDER BY exit_time DESC"""),

    ("All Open",         "trades",
     """SELECT trade_id,symbol,direction,status,strategy_name,
               entry_time,entry_price,stop_loss,take_profit,position_value,broker
        FROM trades WHERE status='open' ORDER BY entry_time DESC"""),

    ("Ghost Trades",     "trades",
     """SELECT trade_id,symbol,direction,status,strategy_name,
               entry_time,entry_price,position_value,broker,
               ROUND((julianday('now','localtime')-julianday(entry_time))*24,1) AS hours_open
        FROM trades
        WHERE status='open'
          AND entry_time < datetime('now','-8 hours','localtime')
        ORDER BY entry_time ASC"""),

    ("Capital Today",    "capital",
     """SELECT timestamp,total_capital,available_cash,invested_value,
               daily_pnl,total_pnl,note
        FROM capital
        WHERE DATE(timestamp)=DATE('now','localtime')
        ORDER BY timestamp DESC"""),

    ("Settlement Queue", "settlement_queue",
     "SELECT * FROM settlement_queue ORDER BY rowid DESC LIMIT 300"),

    ("By Strategy",  "trades",
     """SELECT trade_id,symbol,direction,status,strategy_name,entry_time,
               exit_time,entry_price,exit_price,pnl,broker
        FROM trades
        ORDER BY entry_time DESC LIMIT 500"""),

    ("Strategies",       "_strategies",
     """SELECT replace(key,'strategy_','') AS strategy,
               UPPER(value) AS enabled
        FROM bot_state
        WHERE key LIKE 'strategy_%_enabled'
        ORDER BY strategy"""),

    ("Daily P&L",        "daily_summaries",
     """SELECT trade_date,daily_pnl,daily_pnl_pct,total_trades,
               winning_trades,losing_trades,win_rate,
               largest_win,largest_loss,starting_capital,ending_capital
        FROM daily_summaries ORDER BY trade_date DESC LIMIT 60"""),

    ("Signal Reviews",   "ai_signal_reviews",
     """SELECT timestamp,symbol,direction,strategy_name,
               signal_score,decision,confidence,reasoning
        FROM ai_signal_reviews ORDER BY timestamp DESC LIMIT 200"""),

    ("Yesterday Closed", "trades",
     """SELECT trade_id,symbol,direction,strategy_name,
               entry_time,exit_time,entry_price,exit_price,
               pnl,pnl_pct,exit_reason,broker
        FROM trades
        WHERE status='closed'
          AND DATE(exit_time)=DATE('now','-1 day','localtime')
        ORDER BY exit_time DESC"""),

    ("This Week Trades", "trades",
     """SELECT trade_id,symbol,direction,strategy_name,
               entry_time,exit_time,entry_price,exit_price,
               pnl,pnl_pct,exit_reason,broker
        FROM trades
        WHERE status='closed'
          AND DATE(exit_time)>=DATE('now','-7 days','localtime')
        ORDER BY exit_time DESC"""),

    ("Strategy P&L",     "trades",
     """SELECT strategy_name,
               COUNT(*) AS trades,
               SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS losses,
               ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) AS win_rate_pct,
               ROUND(SUM(pnl),2) AS total_pnl,
               ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END),2) AS gross_wins,
               ROUND(ABS(SUM(CASE WHEN pnl<=0 THEN pnl ELSE 0 END)),2) AS gross_losses,
               ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END)
                     /NULLIF(ABS(SUM(CASE WHEN pnl<=0 THEN pnl ELSE 0 END)),0),3) AS profit_factor
        FROM trades WHERE status='closed'
        GROUP BY strategy_name ORDER BY total_pnl DESC"""),

    ("💰 Financials",    "capital",
     """SELECT label,value,detail FROM
        (SELECT 'Total Capital' AS label,
                '$'||printf('%.2f',total_capital) AS value,
                'Cash: $'||printf('%.2f',available_cash)||'   Invested: $'||printf('%.2f',invested_value) AS detail
         FROM capital ORDER BY timestamp DESC LIMIT 1)
        UNION ALL SELECT label,value,detail FROM
        (SELECT 'Daily P&L (tracker)' AS label,
                (CASE WHEN daily_pnl>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',daily_pnl) AS value,
                'Cumulative: '||(CASE WHEN total_pnl>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',total_pnl) AS detail
         FROM capital ORDER BY timestamp DESC LIMIT 1)
        UNION ALL
        SELECT 'In Open Trades',
               '$'||printf('%.2f',COALESCE(SUM(position_value),0)),
               CAST(COUNT(*) AS TEXT)||' open positions'
        FROM trades WHERE status='open'
        UNION ALL
        SELECT 'In Settlement',
               '$'||printf('%.2f',COALESCE(SUM(amount),0)),
               CAST(COUNT(*) AS TEXT)||' unsettled items'
        FROM settlement_queue WHERE settled=0
        UNION ALL
        SELECT 'Today Closed P&L',
               (CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0)),
               CAST(COUNT(*) AS TEXT)||' trades  '||CAST(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS TEXT)||'W / '||CAST(SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS TEXT)||'L'
        FROM trades WHERE status='closed' AND DATE(exit_time)=DATE('now','localtime')
        UNION ALL
        SELECT 'Yesterday P&L',
               (CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0)),
               CAST(COUNT(*) AS TEXT)||' trades  '||CAST(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS TEXT)||'W / '||CAST(SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) AS TEXT)||'L'
        FROM trades WHERE status='closed' AND DATE(exit_time)=DATE('now','-1 day','localtime')
        UNION ALL
        SELECT 'This Week P&L',
               (CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0)),
               CAST(COUNT(*) AS TEXT)||' trades'
        FROM trades WHERE status='closed' AND DATE(exit_time)>=DATE('now','-7 days','localtime')
        UNION ALL
        SELECT 'This Month P&L',
               (CASE WHEN COALESCE(SUM(pnl),0)>=0 THEN '+' ELSE '' END)||'$'||printf('%.2f',COALESCE(SUM(pnl),0)),
               CAST(COUNT(*) AS TEXT)||' trades'
        FROM trades WHERE status='closed'
          AND strftime('%Y-%m',exit_time)=strftime('%Y-%m','now','localtime')"""),
]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(str(DB_PATH))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=3000")
    c.row_factory = sqlite3.Row
    return c

def _all_tables():
    with _conn() as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows]

def _table_rowcount(t):
    try:
        with _conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    except Exception:
        return 0

def _run_query(sql, params=()):
    t0 = time.perf_counter()
    try:
        with _conn() as c:
            cur  = c.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
        ms = int((time.perf_counter() - t0) * 1000)
        return cols, [list(r) for r in rows], ms, None
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        return [], [], ms, str(e)

def _execute_write(sql, params=()):
    with _conn() as c:
        c.execute(sql, params)
        c.commit()


# ── Edit row dialog ───────────────────────────────────────────────────────────

class EditDialog(tk.Toplevel):
    def __init__(self, parent, table, rowid, cols, values):
        super().__init__(parent)
        self.title(f"Edit  {table}  rowid={rowid}")
        self.configure(bg=BG)
        self.grab_set()
        self.result = None
        self.table  = table
        self.rowid  = rowid

        canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frame = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        self.vars = {}
        for i, (col, val) in enumerate(zip(cols, values)):
            tk.Label(frame, text=col, fg=FG_DIM, bg=BG,
                     font=("Consolas", 9)).grid(row=i, column=0,
                                                sticky="e", padx=(12, 6), pady=2)
            var = tk.StringVar(value="" if val is None else str(val))
            tk.Entry(frame, textvariable=var, width=58,
                     bg=PANEL_BG, fg=FG, insertbackground=FG,
                     relief="flat", font=("Consolas", 10)
                     ).grid(row=i, column=1, sticky="ew", padx=(0, 12), pady=2)
            self.vars[col] = var

        bf = tk.Frame(self, bg=BG)
        bf.pack(fill="x", padx=12, pady=10)
        tk.Button(bf, text="  Save  ", bg=GREEN, fg="#000",
                  font=("Consolas", 10, "bold"), relief="flat",
                  command=self._save).pack(side="left", padx=4)
        tk.Button(bf, text="  Cancel  ", bg=BTN_BG, fg=FG,
                  font=("Consolas", 10), relief="flat",
                  command=self.destroy).pack(side="left")
        self.geometry("700x540")

    def _save(self):
        if not messagebox.askyesno("Confirm", f"Save changes to rowid={self.rowid}?",
                                   parent=self):
            return
        for col, var in self.vars.items():
            try:
                _execute_write(
                    f"UPDATE [{self.table}] SET [{col}]=? WHERE rowid=?",
                    (var.get(), self.rowid)
                )
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=self)
                return
        self.result = True
        self.destroy()


# ── Main app ──────────────────────────────────────────────────────────────────

class DBPanel(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("DB Panel — trading_bot_v2")
        self.configure(bg=BG)
        self.state("zoomed")

        self._current_table = None   # real DB table name
        self._current_view  = None   # quick-query label
        self._cols          = []
        self._rowids        = []
        self._sort_col      = None
        self._sort_asc      = True

        self._build_ui()
        self._load_table_list()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── quick query buttons ──────────────────────────────────────────────
        top = tk.Frame(self, bg=PANEL_BG, pady=5)
        top.pack(fill="x")
        tk.Label(top, text="  Queries:", fg=FG_DIM, bg=PANEL_BG,
                 font=("Consolas", 9)).pack(side="left")
        for label, _tbl, sql in QUICK_QUERIES:
            color = RED if label == "Ghost Trades" else (
                    ORANGE if label == "Strategies" else BTN_BG)
            tk.Button(top, text=label, bg=color, fg=FG,
                      font=("Consolas", 9), relief="flat", padx=8, pady=3,
                      activebackground=BTN_ACT, activeforeground="#fff",
                      command=lambda l=label, t=_tbl, s=sql:
                              self._run_quick(l, t, s)
                      ).pack(side="left", padx=2)

        # ── search bar ────────────────────────────────────────────────────────
        sf = tk.Frame(self, bg=BG, pady=4)
        sf.pack(fill="x", padx=8)

        def lbl(t): return tk.Label(sf, text=t, fg=FG_DIM, bg=BG,
                                    font=("Consolas", 9))

        lbl("Strategy:").pack(side="left", padx=(4, 2))
        self._v_strat  = tk.StringVar()
        tk.Entry(sf, textvariable=self._v_strat, width=14,
                 bg=PANEL_BG, fg=FG, insertbackground=FG, relief="flat",
                 font=("Consolas", 10)).pack(side="left", padx=(0, 6))

        lbl("Status:").pack(side="left", padx=(0, 2))
        self._v_status = tk.StringVar(value="any")
        ttk.Combobox(sf, textvariable=self._v_status, width=9, state="readonly",
                     values=["any","open","closed","pending","cancelled"],
                     font=("Consolas", 10)).pack(side="left", padx=(0, 6))

        lbl("Time:").pack(side="left", padx=(0, 2))
        self._v_time   = tk.StringVar(value="any")
        ttk.Combobox(sf, textvariable=self._v_time, width=11, state="readonly",
                     values=["any","last 1h","last 4h","last 8h",
                             "today","yesterday","this week"],
                     font=("Consolas", 10)).pack(side="left", padx=(0, 6))

        lbl("Symbol:").pack(side="left", padx=(0, 2))
        self._v_sym    = tk.StringVar()
        tk.Entry(sf, textvariable=self._v_sym, width=10,
                 bg=PANEL_BG, fg=FG, insertbackground=FG, relief="flat",
                 font=("Consolas", 10)).pack(side="left", padx=(0, 6))

        lbl("Broker:").pack(side="left", padx=(0, 2))
        self._v_broker = tk.StringVar(value="any")
        ttk.Combobox(sf, textvariable=self._v_broker, width=9, state="readonly",
                     values=["any","alpaca","coinbase","kraken","ibkr"],
                     font=("Consolas", 10)).pack(side="left", padx=(0, 6))

        tk.Button(sf, text="  Search  ", bg=GREEN, fg="#000",
                  font=("Consolas", 9, "bold"), relief="flat",
                  command=self._run_search).pack(side="left", padx=4)
        tk.Button(sf, text="⟳ Refresh", bg=BTN_BG, fg=FG,
                  font=("Consolas", 9), relief="flat",
                  command=self._refresh).pack(side="right", padx=8)

        # ── main split: table list | grid ────────────────────────────────────
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True)

        # left sidebar
        left = tk.Frame(main, bg=PANEL_BG, width=185)
        left.pack(side="left", fill="y", padx=(0, 2))
        left.pack_propagate(False)
        tk.Label(left, text="Tables", fg=FG, bg=PANEL_BG,
                 font=("Consolas", 10, "bold")).pack(pady=(8, 4))
        self._tbl_list = tk.Listbox(left, bg=PANEL_BG, fg=FG,
                                    selectbackground=GRID_SEL,
                                    activestyle="none", relief="flat", bd=0,
                                    font=("Consolas", 10), exportselection=False)
        self._tbl_list.pack(fill="both", expand=True, padx=4, pady=4)
        self._tbl_list.bind("<<ListboxSelect>>", self._on_table_select)

        # right grid area
        right = tk.Frame(main, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._grid_lbl = tk.Label(right, text="Select a table or click a query button",
                                  fg=FG_DIM, bg=BG, font=("Consolas", 10))
        self._grid_lbl.pack(anchor="w", padx=8, pady=(4, 0))

        gf = tk.Frame(right, bg=BG)
        gf.pack(fill="both", expand=True, padx=4, pady=4)
        self._tree = ttk.Treeview(gf, show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(gf, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(gf, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        gf.rowconfigure(0, weight=1)
        gf.columnconfigure(0, weight=1)
        self._tree.bind("<Double-1>",  self._on_dbl_click)
        self._tree.bind("<Button-1>",  self._on_header_click)
        self._tree.bind("<<TreeviewSelect>>", self._on_row_select)

        # ── action bar (context-sensitive) ───────────────────────────────────
        self._action_bar = tk.Frame(self, bg=PANEL_BG, pady=4)
        self._action_bar.pack(fill="x", padx=4)
        self._build_action_bar()

        # ── status bar ───────────────────────────────────────────────────────
        self._status = tk.Label(self, text="Ready", fg=FG_DIM, bg=PANEL_BG,
                                anchor="w", font=("Consolas", 9))
        self._status.pack(fill="x", padx=8, pady=2)

        # style
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=GRID_BG, foreground=FG,
                         fieldbackground=GRID_BG, rowheight=22,
                         font=("Consolas", 9))
        style.configure("Treeview.Heading", background=HEADER, foreground=FG,
                         font=("Consolas", 9, "bold"), relief="flat")
        style.map("Treeview",
                  background=[("selected", GRID_SEL)],
                  foreground=[("selected", "#fff")])

    def _build_action_bar(self):
        for w in self._action_bar.winfo_children():
            w.destroy()

        tk.Label(self._action_bar, text="  Actions:", fg=FG_DIM, bg=PANEL_BG,
                 font=("Consolas", 9)).pack(side="left")

        view = self._current_view or ""

        # ── trade actions ────────────────────────────────────────────────────
        if self._current_table == "trades" or view in (
                "Open Today","Closed Today","All Open","Ghost Trades","By Strategy"):
            tk.Button(self._action_bar, text="✏ Edit Row",
                      bg=BTN_BG, fg=FG, font=("Consolas", 9), relief="flat",
                      padx=8, pady=2,
                      command=self._action_edit).pack(side="left", padx=3)
            tk.Button(self._action_bar, text="☠ Flush Ghost Trade",
                      bg=RED, fg="#fff", font=("Consolas", 9, "bold"),
                      relief="flat", padx=8, pady=2,
                      command=self._action_flush_ghost).pack(side="left", padx=3)
            tk.Label(self._action_bar,
                     text="← select a row first",
                     fg=FG_DIM, bg=PANEL_BG, font=("Consolas", 8)
                     ).pack(side="left", padx=4)

        # ── strategy actions ─────────────────────────────────────────────────
        elif view == "Strategies":
            tk.Button(self._action_bar, text="✔ Enable Strategy",
                      bg=GREEN, fg="#000", font=("Consolas", 9, "bold"),
                      relief="flat", padx=8, pady=2,
                      command=lambda: self._action_toggle_strategy(True)
                      ).pack(side="left", padx=3)
            tk.Button(self._action_bar, text="✖ Disable Strategy",
                      bg=RED, fg="#fff", font=("Consolas", 9, "bold"),
                      relief="flat", padx=8, pady=2,
                      command=lambda: self._action_toggle_strategy(False)
                      ).pack(side="left", padx=3)
            tk.Label(self._action_bar,
                     text="← select a strategy row first",
                     fg=FG_DIM, bg=PANEL_BG, font=("Consolas", 8)
                     ).pack(side="left", padx=4)

        # ── generic edit for all other tables ────────────────────────────────
        else:
            tk.Button(self._action_bar, text="✏ Edit Row",
                      bg=BTN_BG, fg=FG, font=("Consolas", 9), relief="flat",
                      padx=8, pady=2,
                      command=self._action_edit).pack(side="left", padx=3)

    # ── table list ────────────────────────────────────────────────────────────

    def _load_table_list(self):
        self._tbl_list.delete(0, tk.END)
        for t in _all_tables():
            cnt = _table_rowcount(t)
            self._tbl_list.insert(tk.END, f"  {t}  ({cnt:,})")

    def _on_table_select(self, _=None):
        sel = self._tbl_list.curselection()
        if not sel:
            return
        raw = self._tbl_list.get(sel[0]).strip()
        table = raw.split("  (")[0].strip()
        self._current_table = table
        self._current_view  = None
        self._build_action_bar()
        sql = f"SELECT rowid, * FROM [{table}] ORDER BY rowid DESC LIMIT 2000"
        self._exec_display(sql, title=table)

    # ── quick queries ─────────────────────────────────────────────────────────

    def _run_quick(self, label, table, sql):
        self._current_table = table if not table.startswith("_") else None
        self._current_view  = label
        self._build_action_bar()
        # Run the SQL directly -- no subquery wrapping (breaks rowid access)
        self._exec_display(sql, title=label)

    # ── search ────────────────────────────────────────────────────────────────

    # Per-table search config: which filters apply and which timestamp col to use
    _TABLE_SEARCH = {
        "trades":           {"strategy":True, "status":True, "symbol":True,
                             "broker":True,   "time_col":"entry_time"},
        "capital":          {"time_col":"timestamp"},
        "daily_summaries":  {"time_col":"trade_date"},
        "settlement_queue": {"time_col":"created_at"},
        "ai_signal_reviews":{"strategy":True, "symbol":True, "time_col":"timestamp"},
        "strategy_results": {"strategy":True, "time_col":"recorded_at"},
        "tax_ledger":       {"symbol":True,   "time_col":"close_date"},
        "withdrawals":      {"time_col":"timestamp"},
        "bot_state":        {},
        "chat_actions":     {"time_col":"timestamp"},
        "fund_events":      {"time_col":"timestamp"},
        "session_state":    {},
    }

    def _run_search(self):
        table = self._current_table
        # If coming from a virtual view (Strategies etc.), default to trades
        if not table or table.startswith("_"):
            table = "trades"
        self._current_table = table

        cfg    = self._TABLE_SEARCH.get(table, {})
        where, params = [], []

        if cfg.get("strategy"):
            s = self._v_strat.get().strip()
            if s:
                where.append("strategy_name LIKE ?"); params.append(f"%{s}%")

        if cfg.get("status"):
            st = self._v_status.get()
            if st != "any":
                where.append("status=?"); params.append(st)

        if cfg.get("symbol"):
            sym = self._v_sym.get().strip().upper()
            if sym:
                where.append("symbol LIKE ?"); params.append(f"%{sym}%")

        if cfg.get("broker"):
            br = self._v_broker.get()
            if br != "any":
                where.append("broker=?"); params.append(br)

        tcol = cfg.get("time_col")
        tf   = self._v_time.get()
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
                where.append(f"DATE({tcol})=DATE('now','-1 day','localtime')")
            elif tf == "this week":
                where.append(f"{tcol}>=datetime('now','-7 days','localtime')")

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        sql    = (f"SELECT rowid, * FROM [{table}] {clause} "
                  f"ORDER BY rowid DESC LIMIT 2000")
        self._current_view = None
        self._build_action_bar()
        self._exec_display(sql, params=params, title=f"Search → {table}")

    # ── execute & display ─────────────────────────────────────────────────────

    def _exec_display(self, sql, params=(), title=""):
        cols, rows, ms, err = _run_query(sql, params)
        if err:
            self._status.config(text=f"ERROR: {err}", fg=RED)
            messagebox.showerror("Query error", err)
            return

        has_rowid = cols and cols[0] == "rowid"
        if has_rowid:
            self._rowids = [r[0] for r in rows]
            dcols        = cols[1:]
            drows        = [r[1:] for r in rows]
        else:
            # No rowid in result -- store None; edit will look up by trade_id if needed
            self._rowids = [None] * len(rows)
            dcols        = cols
            drows        = rows

        self._cols = dcols
        self._grid_lbl.config(
            text=f"{title}  ({len(rows):,} rows)", fg=YELLOW)
        self._populate(dcols, drows)
        self._status.config(
            text=f"{len(rows):,} rows  ·  {ms}ms  ·  {title}", fg=FG_DIM)

    def _populate(self, cols, rows):
        t = self._tree
        t.delete(*t.get_children())
        t["columns"] = cols
        wide = {"trade_id","reason","reasoning","exit_reason",
                "indicators_json","warnings_json","note","strategy"}
        for col in cols:
            t.heading(col, text=col,
                      command=lambda c=col: self._sort_by(c))
            t.column(col, width=150 if col in wide else 90,
                     anchor="w", stretch=True)
        for i, row in enumerate(rows):
            tag  = "even" if i % 2 == 0 else "odd"
            vals = ["" if v is None else str(v) for v in row]
            t.insert("", "end", iid=str(i), values=vals, tags=(tag,))
        t.tag_configure("even", background=GRID_BG)
        t.tag_configure("odd",  background=GRID_ALT)

    # ── sorting ───────────────────────────────────────────────────────────────

    def _on_header_click(self, event):
        if self._tree.identify("region", event.x, event.y) == "heading":
            col = self._tree.identify_column(event.x)
            idx = int(col.replace("#", "")) - 1
            if 0 <= idx < len(self._cols):
                self._sort_by(self._cols[idx])

    def _sort_by(self, col):
        self._sort_asc = not self._sort_asc if self._sort_col == col else True
        self._sort_col = col
        t     = self._tree
        items = [(t.set(k, col), k) for k in t.get_children("")]
        try:
            items.sort(key=lambda x: float(x[0]) if x[0] else 0,
                       reverse=not self._sort_asc)
        except ValueError:
            items.sort(key=lambda x: x[0].lower(), reverse=not self._sort_asc)
        for idx, (_, k) in enumerate(items):
            t.move(k, "", idx)
            t.item(k, tags=("even" if idx % 2 == 0 else "odd",))

    # ── row selection ─────────────────────────────────────────────────────────

    def _on_row_select(self, _=None):
        pass   # placeholder — buttons enabled regardless, show error if no sel

    def _selected_iid_and_rowid(self):
        sel = self._tree.selection()
        if not sel:
            messagebox.showwarning("No row selected", "Click a row first.")
            return None, None
        iid = sel[0]
        try:
            rowid = self._rowids[int(iid)]
        except (ValueError, IndexError):
            rowid = None
        return iid, rowid

    def _selected_values(self):
        iid, rowid = self._selected_iid_and_rowid()
        if iid is None:
            return None, None, None
        vals = list(self._tree.item(iid, "values"))
        return iid, rowid, vals

    # ── actions ───────────────────────────────────────────────────────────────

    def _action_edit(self):
        iid, rowid, vals = self._selected_values()
        if iid is None:
            return
        table = self._current_table
        if not table or table.startswith("_"):
            messagebox.showinfo("Cannot edit",
                                "Browse a real table first (click one in the left list).")
            return
        # If rowid wasn't in the query result, fetch it via trade_id or id
        if rowid is None:
            rowid = self._lookup_rowid(table, vals)
        if rowid is None:
            messagebox.showwarning("Cannot edit",
                                   "Cannot determine row ID. Open the table directly from the left list to edit.")
            return
        dlg = EditDialog(self, table, rowid, self._cols, vals)
        self.wait_window(dlg)
        if dlg.result:
            self._status.config(text=f"Saved rowid={rowid}", fg=GREEN)
            self._refresh()

    def _action_flush_ghost(self):
        iid, rowid, vals = self._selected_values()
        if iid is None:
            return

        # Find trade_id and symbol from row values
        col_map = {c: v for c, v in zip(self._cols, vals)}
        trade_id = col_map.get("trade_id", "")
        symbol   = col_map.get("symbol",   "?")
        status   = col_map.get("status",   "")
        hours    = col_map.get("hours_open", "")
        broker   = col_map.get("broker",   "")

        if status == "closed":
            messagebox.showinfo("Already closed",
                                f"{symbol} is already closed — nothing to flush.")
            return

        msg = (f"Flush ghost trade?\n\n"
               f"  Symbol:    {symbol}\n"
               f"  Trade ID:  {trade_id}\n"
               f"  Broker:    {broker}\n"
               f"  Hours open:{hours}\n\n"
               f"This marks the trade as CLOSED in the DB with\n"
               f"exit_reason='manual_flush_ghost' and pnl=0.\n\n"
               f"It does NOT cancel any real broker order.\n"
               f"Make sure the position is already gone at the broker first.")
        if not messagebox.askyesno("Confirm flush", msg):
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry_price = col_map.get("entry_price", "0")

        try:
            _execute_write(
                """UPDATE trades SET
                     status      = 'closed',
                     exit_time   = ?,
                     exit_price  = ?,
                     exit_reason = 'manual_flush_ghost',
                     pnl         = 0,
                     pnl_pct     = 0
                   WHERE trade_id = ?""",
                (now_str, entry_price, trade_id)
            )
            self._status.config(text=f"Flushed: {symbol} ({trade_id})", fg=GREEN)
            self._refresh()
        except Exception as e:
            messagebox.showerror("Flush failed", str(e))

    def _action_toggle_strategy(self, enable: bool):
        iid, rowid, vals = self._selected_values()
        if iid is None:
            return

        col_map = {c: v for c, v in zip(self._cols, vals)}
        strategy_col = col_map.get("strategy", "")
        # strategy col contains e.g. "grid_bot_enabled"
        # the bot_state key is "strategy_grid_bot_enabled"
        key = f"strategy_{strategy_col}"
        if not key.endswith("_enabled"):
            messagebox.showwarning("Select strategy row",
                                   "Click a row from the Strategies view.")
            return

        name = strategy_col.replace("_enabled", "")
        val  = "true" if enable else "false"
        action = "ENABLE" if enable else "DISABLE"
        color  = GREEN if enable else RED

        if not messagebox.askyesno(f"{action} strategy",
                                   f"{action} strategy '{name}'?\n\n"
                                   f"The bot reads this from bot_state — "
                                   f"change takes effect on the next bot cycle."):
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _execute_write(
                "UPDATE bot_state SET value=?, updated=? WHERE key=?",
                (val, now_str, key)
            )
            self._status.config(
                text=f"{action}D: {name}  (bot picks up on next cycle)", fg=color)
            # refresh the strategies view
            self._run_quick("Strategies", "_strategies",
                            QUICK_QUERIES[7][2])   # index of Strategies entry
        except Exception as e:
            messagebox.showerror("Failed", str(e))

    # ── double-click → edit ───────────────────────────────────────────────────

    def _lookup_rowid(self, table, vals):
        """Find rowid by matching trade_id, id, or key column."""
        col_map = {c: v for c, v in zip(self._cols, vals)}
        for key_col in ("trade_id", "id", "key"):
            key_val = col_map.get(key_col)
            if key_val:
                try:
                    cols, rows, _, _ = _run_query(
                        f"SELECT rowid FROM [{table}] WHERE [{key_col}]=? LIMIT 1",
                        (key_val,)
                    )
                    if rows:
                        return rows[0][0]
                except Exception:
                    pass
        return None

    def _on_dbl_click(self, event):
        if self._tree.identify("region", event.x, event.y) != "cell":
            return
        self._action_edit()

    # ── refresh ───────────────────────────────────────────────────────────────

    def _refresh(self):
        self._load_table_list()
        view  = self._current_view
        table = self._current_table
        if view:
            for label, tbl, sql in QUICK_QUERIES:
                if label == view:
                    self._run_quick(label, tbl, sql)
                    return
        if table:
            sql = f"SELECT rowid, * FROM [{table}] ORDER BY rowid DESC LIMIT 2000"
            self._exec_display(sql, title=table)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        raise SystemExit(1)
    DBPanel().mainloop()
