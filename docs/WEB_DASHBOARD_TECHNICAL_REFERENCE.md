# HTML Dashboard Technical Reference

Current reference for the live HTML dashboard served by `web_dashboard.py`.

---

## Purpose

The HTML dashboard is the newer browser UI for the trading bot. It replaces the
old Streamlit workflow for day-to-day use, while still talking to the same bot
database, config files, watchlists, backtester, trade executor, and chat command
logic.

Run it from the repo root:

```powershell
python web_dashboard.py
```

Then open:

```text
http://localhost:8125
```

`web_dashboard.py` serves static files from `Html-Files/` and exposes the local
JSON API used by the browser.

---

## Runtime Flow

```text
Browser
  -> Trading Dashboard.html
      -> icons.jsx
      -> data.jsx
      -> window-manager.jsx
      -> panels.jsx
      -> side-panels.jsx
      -> tweaks-panel.jsx
      -> app.jsx

User action or polling
  -> fetch("/api/...")
      -> web_dashboard.py Handler
          -> config.py / SQLite / bot modules / watchlist files / backtester
          -> JSON response
              -> React panel state updates
```

The dashboard and the bot engine are separate processes. The dashboard can start
or stop `bot_engine.py`, but it is not the trading engine itself.

---

## Key Files

| File | Role |
|---|---|
| `web_dashboard.py` | Local HTTP bridge on port `8125`; serves HTML/JS/CSS and implements `/api/...` endpoints. |
| `Html-Files/Trading Dashboard.html` | Entry HTML file. Loads React, Chart.js, Babel, CSS, and the JSX files in dependency order. |
| `Html-Files/app.jsx` | Main shell. Owns `WINDOW_DEFS`, top bar, dock, taskbar, and mounts each panel inside a draggable window. |
| `Html-Files/window-manager.jsx` | Window system: open, close, focus, drag, resize, maximize, minimize, persisted layout, expanding canvas. |
| `Html-Files/panels.jsx` | Main content panels: overview, open positions, manual trade, trade log, daily performance, backtester, chat. |
| `Html-Files/side-panels.jsx` | Utility panels: manage positions, inject symbols, quick controls, PowerShell workspace. |
| `Html-Files/data.jsx` | Fallback/static lists and mock data. Live panels should prefer API data when available. |
| `Html-Files/icons.jsx` | Inline SVG icon registry used by `<Icon name="..." />`. |
| `Html-Files/styles.css` | Dashboard styling, table styling, panels, charts, buttons, run log, window chrome. |
| `CHAT_COMMANDS.md` | User-facing chat command reference. |

There is also `Html-Files/technical_reference.md`, but this root document is the
current live reference for the migrated dashboard.

---

## Frontend Architecture

`app.jsx` is the dashboard entrypoint after all supporting JSX files load. It
pulls components from globals:

```javascript
window.Panels
window.SidePanels
window.Icon
window.MOCK
```

`WINDOW_DEFS` in `app.jsx` is the source of truth for window IDs, titles, icons,
default positions, default sizes, and whether a window opens by default.

To add a panel:

1. Build the panel component in `panels.jsx` or `side-panels.jsx`.
2. Export it through `window.Panels` or `window.SidePanels`.
3. Add a `WINDOW_DEFS` entry in `app.jsx`.
4. Add a `<Window id="..."><Panel /></Window>` in `Workspace`.
5. Add a dock or topbar launcher if wanted.
6. Cache-bust the script URL in `Trading Dashboard.html`.

The window manager persists layout in:

```text
localStorage["tbot.windows.v2"]
```

If layout becomes strange after changing window defaults, use the dashboard
`Reset Layout` button or clear that localStorage key.

---

## API Base Rules

The frontend helper uses this rule:

```javascript
if (window.DASHBOARD_API_BASE) use it
else if page is opened as file: use http://localhost:8125
else use relative paths
```

That lets the dashboard work whether opened through `http://localhost:8125` or
as a local file, as long as `web_dashboard.py` is running.

If the UI says `Failed to fetch`, first check:

1. Is `web_dashboard.py` still running?
2. Is the page talking to `http://localhost:8125`?
3. Did `web_dashboard.py` recently change and need a restart?
4. Did the browser cache an older JSX file and need a hard refresh?

---

## Important API Endpoints

### Read-only or mostly read-only

| Endpoint | Used by | Notes |
|---|---|---|
| `GET /api/snapshot` | Overview, quick controls | Bot status, process truth, KPIs, high-level state. |
| `GET /api/overview` | Overview, daily performance | Capital history, daily performance, win/loss data. |
| `GET /api/open_positions` | Open Positions, Manage Positions | Reads open trades and live-ish pricing fields. Supports `sort_by` and `sort_dir`. |
| `GET /api/trade_log/today` | Today's Trade Log | Closed trades for the current day. |
| `GET /api/candles` | Manage Positions chart | Returns candles for symbol/timeframe charting. |
| `GET /api/injected_symbols` | Inject Symbols | Reads temporary injected stock and crypto watchlists. |
| `GET /api/manual_trade/config` | Manual Trade | Dropdown options and default values. |
| `GET /api/manual_trade/preview` | Manual Trade | Calculates preview price, stop, take profit, and quantity. |
| `GET /api/backtest/config` | Backtester | Strategy lists, duration/timeframe options, and merged stock/crypto symbol lists. |
| `GET /api/quick_status` | Quick Controls | ML, tax, and market-condition summaries. |

### Mutating endpoints

| Endpoint | Used by | Notes |
|---|---|---|
| `POST /api/control` | Quick controls, manage positions, PowerShell panel | Action bridge for bot control, close position, pause/resume, terminal launch. |
| `POST /api/manual_trade` | Manual Trade | Opens a manually requested trade through backend validation/execution. |
| `POST /api/backtest` | Backtester | Runs historical backtests through `intelligence.backtester.Backtester`. |
| `POST /api/injected_symbols` | Inject Symbols | Adds temporary stock or crypto symbols. Bare crypto symbols become `/USD`. |
| `POST /api/injected_symbols/clear` | Inject Symbols | Clears temporary injected symbols. |
| `POST /api/chat` | Chat with Bot | Handles local deterministic commands first, then optional AI chat. |
| `POST /api/ml/retrain` | Quick Controls | Requests ML retraining. |
| `POST /api/report/daily` | Quick Controls | Generates today's report. |
| `POST /api/tax/export` | Quick Controls | Exports tax CSV. |
| `POST /api/withdrawal` | Quick Controls | Records/processes withdrawal workflow. |

`POST /api/control` actions currently include:

```text
pause_trading
resume_trading
start_bot
stop_bot
restart_bot
refresh_scan
open_terminal
close_position
```

---

## Panel Data Flow

### Overview

`TopOverview` polls `/api/snapshot` and `/api/overview`. It renders:

- account/capital KPIs
- capital growth chart
- win/loss donut
- status information

The win/loss donut uses Chart.js. Legend clicks are disabled so clicking the
green/red legend dots cannot hide a segment and repaint the donut incorrectly.

### Open Positions

`OpenPositionsPanel` calls `/api/open_positions`. Sorting is kept client-side
after refresh so header clicks do not revert when polling runs again.

Actions:

- `Refresh` reloads `/api/open_positions`
- `Manage Positions` opens the manage panel
- `CSV` exports the current visible rows in the browser

### Manage Positions

`ManagePositionsPanel` also reads `/api/open_positions`, but its purpose is
actions:

- close one position through `/api/control` action `close_position`
- close all positions only after two confirmation steps
- open a mini chart through `/api/candles`
- export CSV

Manual close requests are handed to the backend in the background so the UI
becomes usable again quickly instead of waiting for broker execution.

### Today's Trade Log

`TradeLogPanel` calls `/api/trade_log/today`, supports stable header sorting,
and exports CSV. Result cells use text-only pills to avoid stale painted
background artifacts during scroll/sort.

### Recent Daily Performance

`DailyPerfPanel` reads daily summaries from `/api/overview`, then filters them
client-side by `7D`, `30D`, `90D`, and `YTD`. It supports header sorting and CSV.

### Backtester

`BacktesterPanel` loads dropdown config from `/api/backtest/config`.

Symbols come from the same backend sources used by `ALL` runs:

- `config.STOCK_WATCHLIST`
- `config.CRYPTO_WATCHLIST`
- `watchlists/stocks.txt`
- `watchlists/crypto.txt`
- `watchlist/scanned_stocks.txt`
- `watchlist/scanned_crypto.txt`

`POST /api/backtest` runs the backtester. The response includes summary stats,
detailed per-run rows, sampled equity curves, drawdown, returns, and skipped run
counts.

The status log is intentionally below the controls so long tests do not shove
the form fields around while messages update.

Backtest result row selection is styled with a row class, not a raw `<tr>`
outline, to avoid scroll paint artifacts.

### Inject Symbols

`InjectSymbolsPanel` writes temporary watchlist files through:

```text
watchlist/scanned_stocks.txt
watchlist/scanned_crypto.txt
```

Crypto input accepts either `FOREST/USD` or `FOREST`; bare symbols are saved as
`FOREST/USD`.

### Quick Controls

`QuickControlsPanel` wraps bot lifecycle, trading pause/resume, reports, tax,
withdrawal, and ML controls. Bot process truth should come from
`bot_process_running`, not stale status text.

### PowerShell Workspace

`TerminalPanel` calls `/api/control` action `open_terminal`, which opens a real
PowerShell rooted at the repo. It does not execute arbitrary shell commands from
the browser. The panel provides copyable command shortcuts for logs, compile
checks, and common scripts.

The launcher intentionally allows the user's `$PROFILE` to run, so local venv
auto-entry still works.

### Chat

`ChatPanel` posts to `/api/chat`. The backend handles low-cost deterministic
commands when possible, such as listing strategies or injected symbols, and only
needs an AI provider for open-ended chat.

---

## Backtester Backend Flow

`run_backtest_api()` in `web_dashboard.py`:

1. Imports `Backtester` from `intelligence.backtester`.
2. Parses asset class, symbol, strategy, timeframe, duration, capital, and stop mode.
3. Expands `ALL` into the merged symbol list from `get_backtest_config()`.
4. Expands `ALL strategies` into `BACKTEST_STRATEGIES`.
5. Runs each symbol/strategy pair.
6. Converts each result with `_backtest_result_payload()`.
7. Returns:
   - summary stats
   - `results` table rows
   - sampled `equity_curve`
   - skipped count
   - requested run count

If no run returns trades, the API returns a 400 with a useful message such as:

```text
No results; N run(s) had insufficient data or no trades.
```

The frontend keeps the run log visible on this error path.

---

## Restart And Cache Rules

Frontend-only changes:

- `Html-Files/*.jsx`
- `Html-Files/styles.css`
- `Trading Dashboard.html`

Usually need only a browser hard refresh. Cache-bust script or CSS URLs in
`Trading Dashboard.html` when changing JSX/CSS.

Backend changes:

- `web_dashboard.py`
- Python modules imported by `web_dashboard.py`
- new API endpoint
- changed API response shape

Need a `web_dashboard.py` restart and a browser hard refresh.

Bot-engine changes:

- `bot_engine.py`
- scanner, strategy, risk, executor, monitor internals

Need a bot restart. The dashboard bridge may not need a restart unless its API
imports or response shape changed too.

---

## Safety Rules

- Keep dangerous actions behind confirmation. `Close All` requires two explicit
  confirmations.
- Prefer background handoff for slow broker calls, then refresh the UI later.
- Do not execute arbitrary shell commands from browser text input.
- Keep `web_dashboard.py` separate from `bot_engine.py`; the bridge should
  control the bot process, not become the bot.
- When debugging Start/Stop, match the actual `bot_engine.py` process, not just
  any Python process.

---

## Troubleshooting

| Symptom | Likely cause | First check |
|---|---|---|
| `Failed to fetch` | Bridge not running or wrong origin | Confirm `web_dashboard.py` is running on port `8125`. |
| New button does nothing | Browser cached old JSX | Hard refresh; check cache-busted script URL. |
| New endpoint 404s | Bridge not restarted | Restart `web_dashboard.py`. |
| Bot status looks wrong | Status text stale | Trust `bot_process_running`, then inspect `logs/bot.log`. |
| Backtester dropdown too short | Config endpoint failed or old cached JS | Check `/api/backtest/config`, hard refresh. |
| Sorting reverts after seconds | Poll refresh overwrote sorted state | Keep sort in a ref and re-apply after data reload. |
| Table background sticks while scrolling | CSS paint artifact | Avoid sticky/outline/large background layers on scrolling table rows. |
| Dashboard layout feels trapped | Workspace canvas too small or old localStorage | Drag/resize to expand, or use Reset Layout. |

