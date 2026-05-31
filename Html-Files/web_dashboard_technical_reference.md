# Trading Dashboard — Technical Reference

A map of the HTML dashboard for engineers wiring it to a Python trading bot.

---

## File map

```
Trading Dashboard.html      Entry point. Loads CDN deps + all .jsx files in order.
styles.css                  All styling (CSS variables for the design tokens).
icons.jsx                   <Icon name="..."/> — inline SVG line icons.
data.jsx                    window.MOCK — all mock arrays. REPLACE with API calls.
window-manager.jsx          <WindowProvider>, <Window>, useWindows() hook.
panels.jsx                  Always-open panels (Overview, Positions, Manual Trade,
                            Trade Log, Daily Perf, Backtester, Chat).
side-panels.jsx             Detached/closable panels (Manage Positions, Inject
                            Symbols, Quick Controls).
tweaks-panel.jsx            Optional tweaks UI (unused by default).
app.jsx                     Main shell. WINDOW_DEFS, Topbar, Dock, Taskbar, mounts root.
```

Load order matters — `app.jsx` depends on globals exposed by every other file.

---

## Architecture in one paragraph

`app.jsx` mounts `<AppShell>`, which wraps everything in a `<WindowProvider>` (defined in `window-manager.jsx`). The provider holds an object of window state keyed by id (`x, y, w, h, open, minimized, maximized, z`). It persists to `localStorage` under `tbot.windows.v2`. Each `<Window id="...">` reads its state from the provider via `useWindows()`. The `WINDOW_DEFS` array in `app.jsx` is the single source of truth for window ids, titles, default geometry, and which open by default.

---

## Where to plug in real data

All mock data lives in `data.jsx` on `window.MOCK`. Replace it with fetched data — easiest pattern is a `DataProvider` context that polls your Python backend.

### Replace these arrays/objects

| MOCK key            | Used by                                     | Shape                                                        |
|---------------------|---------------------------------------------|--------------------------------------------------------------|
| `OPEN_POSITIONS`    | `OpenPositionsPanel`, `ManagePositionsPanel`| `{opened, symbol, type, dir, entry, current, sl, tp, pnlD, pnlP, toSL, toTP}` |
| `TRADE_LOG`         | `TradeLogPanel`                             | `{opened, closed, dur, symbol, dir, strategy, entry, exit, reason, pnlD, pnlP, result}` |
| `DAILY_PERF`        | `DailyPerfPanel`                            | `{date, trades, wins, losses, winRate, pnlD, pnlP, capital}` |
| `CAPITAL_HISTORY`   | `CapitalChart`                              | `{date: Date, capital: number}[]`                            |
| `INJECTED`          | `InjectSymbolsPanel`                        | `{stocks: string[], crypto: string[]}`                       |
| `STRATEGIES` / `TIMEFRAMES` / `DURATIONS` / `ASSET_CLASSES` / `BROKERS` / `DIRECTIONS` | Backtester, Manual Trade dropdowns | `string[]` |

### Top-level "live" props

`<TopOverview live={...}>` in `app.jsx` is currently passed `{ capital, dailyPnl, dailyPnlP }` as a literal. Replace with state populated from a polling hook:

```jsx
const live = useLiveStats();   // your hook -> GET /api/stats every Ns
<TopOverview live={live} />
```

The status banner (`Trading Active — PID 11552 — ML 30%`) and the KPI hard-codes (`16 open`, `25 trades today`, `4 consec losses`) are inside `TopOverview` in `panels.jsx` — feed them via props the same way.

---

## Action wiring (buttons → Python)

Replace these handlers/no-ops with `fetch('/api/...')` calls.

### `panels.jsx`
| Component             | Action                                 | Suggested endpoint                          |
|-----------------------|----------------------------------------|---------------------------------------------|
| `ManualTradePanel`    | Open Trade at Market Price             | `POST /api/trades/manual` with form body    |
| `OpenPositionsPanel`  | Refresh                                | `GET /api/positions`                        |
| `OpenPositionsPanel`  | Manage Positions                       | (UI only — opens window)                    |
| `BacktesterPanel`     | Run Backtest (`run()` is mocked w/ setTimeout) | `POST /api/backtest` → returns result object |
| `TradeLogPanel`       | Export CSV                             | `GET /api/tradelog/export.csv`              |
| `ChatPanel`           | Currently uses `window.claude.complete`| Replace with `POST /api/chat`               |

### `side-panels.jsx`
| Component             | Action                                 | Suggested endpoint                          |
|-----------------------|----------------------------------------|---------------------------------------------|
| `ManagePositionsPanel`| Close Now (single)                     | `POST /api/positions/{symbol}/close`        |
| `ManagePositionsPanel`| Close All                              | `POST /api/positions/close-all`             |
| `ManagePositionsPanel`| Adjust SL/TP                           | `PATCH /api/positions/{symbol}` body `{sl, tp}` |
| `InjectSymbolsPanel`  | Inject Stocks / Inject Crypto          | `POST /api/watchlist/inject`                |
| `InjectSymbolsPanel`  | Clear All Injected                     | `DELETE /api/watchlist/injected`            |
| `InjectSymbolsPanel`  | Remove individual chip                 | `DELETE /api/watchlist/injected/{symbol}`   |
| `QuickControlsPanel`  | Restart Bot / Stop Bot                 | `POST /api/bot/restart`, `POST /api/bot/stop` |
| `QuickControlsPanel`  | Pause/Resume Trading                   | `POST /api/bot/pause`, `POST /api/bot/resume` |
| `QuickControlsPanel`  | Max Position % slider                  | `PATCH /api/risk` body `{maxPosPct}`        |
| `QuickControlsPanel`  | Train ML Now                           | `POST /api/ml/train`                        |
| `QuickControlsPanel`  | Generate Today's Report                | `POST /api/reports/today`                   |
| `QuickControlsPanel`  | Export Form 8949 CSV                   | `GET /api/tax/8949.csv?year=...`            |
| `QuickControlsPanel`  | Process Withdrawal                     | `POST /api/withdrawal` body `{amount, reason}` |

---

## Window manager API (window-manager.jsx)

```jsx
const { windows, focus, open, close, toggleMin, toggleMax, updateGeometry, focusedId } = useWindows();
```

- `windows[id]` — `{ x, y, w, h, open, minimized, maximized, z, title, icon }`
- `open(id)` — open window (or restore if minimized) and bring to front
- `close(id)` — hide window (state preserved)
- `focus(id)` — bring to front, restore from minimized
- `toggleMin(id)`, `toggleMax(id)` — min/maximize
- `updateGeometry(id, {x, y, w, h})` — programmatic move/resize

**Adding a new window:**
1. Add an entry to `WINDOW_DEFS` in `app.jsx`.
2. Build a panel component (in `panels.jsx` or new file).
3. Add `<Window id="my-window"><MyPanel /></Window>` inside `<Workspace>` in `app.jsx`.
4. Add a dock entry in `Dock()` in `app.jsx`.

State auto-persists to `localStorage['tbot.windows.v2']`. Clear that key to reset positions.

---

## Live updates pattern

`OpenPositionsPanel` already shows the pattern — it uses `setInterval` to drift prices every 2.5s. Swap for WebSocket or polling:

```jsx
useEffect(() => {
  const ws = new WebSocket('ws://localhost:8000/positions');
  ws.onmessage = (e) => setPositions(JSON.parse(e.data));
  return () => ws.close();
}, []);
```

---

## Styling tokens (`styles.css`)

All colors/spacing are CSS variables on `:root`. Change once, propagates everywhere:

```css
--accent: #14b8a6;        /* teal — primary brand color */
--green: #10b981;         /* P&L positive */
--red:   #f43f5e;         /* P&L negative */
--bg-0..bg-4              /* darkest → lightest panel bg */
--text-0..text-3          /* primary → faintest text */
--mono                    /* JetBrains Mono — all numerics */
--sans                    /* Inter — UI */
```

Built-in utility classes: `.btn`, `.btn.primary`, `.btn.danger`, `.btn.ghost`, `.input`, `.select`, `.field`, `.tbl`, `.pill.long/.short/.win/.loss`, `.banner.success/.info/.warn`, `.kpi`, `.pos-card`, `.chip`, `.tag`.

---

## Charts

Uses Chart.js v4 (CDN). `CapitalChart` and `WinLossDonut` in `panels.jsx`. The mini per-position candle chart in `ManagePositionsPanel` is hand-drawn SVG (no dependency) — replace its candle generation with real OHLC arrays from `GET /api/candles?symbol=...&size=...`.

---

## Suggested integration order

1. Wire `OPEN_POSITIONS` (most visual, easy to verify).
2. Wire `TRADE_LOG` and `DAILY_PERF` (read-only tables).
3. Wire `CAPITAL_HISTORY` (chart).
4. Top KPIs + status banner.
5. Manual Trade form submission.
6. Manage Positions actions (close, adjust).
7. Inject Symbols CRUD.
8. Backtester run.
9. Quick Controls (bot/risk/ML/tax/withdrawal).
10. Chat (replace `window.claude.complete` with your LLM endpoint).

---

## Gotchas

- **Babel script collisions:** every `<script type="text/babel">` shares global scope after Babel transforms. Components are exposed via `window.X = X` at the bottom of each file. Keep new shared names unique.
- **localStorage cache:** if you change `WINDOW_DEFS` ids/defaults, increment the `STORAGE_KEY` in `window-manager.jsx` (currently `tbot.windows.v2`) so users get the new defaults.
- **CDN deps** are pinned by integrity hash — do not change versions without updating the SHA.
- **All money/percent formatting** goes through helpers in `panels.jsx` (`fmt$`, `fmt$Sign`, `fmtP`, `fmtN`) exposed on `window.fmt`.
