// Side-panel windows: Manage Positions, Inject Symbols, Quick Controls (Tax/Reports/Withdrawal)
const { useState: uS, useEffect: uE, useRef: uR } = React;

// ---------- Shared API helpers ----------
const apiBase = () => {
  const configured = (window.DASHBOARD_API_BASE || '').replace(/\/$/, '');
  if (configured) return configured;
  if (window.location.protocol === 'file:') return 'http://localhost:8125';
  return '';
};

const apiCall = async (path, opts = {}) => {
  const res = await fetch(`${apiBase()}${path}`, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.message || data?.error || `HTTP ${res.status}`);
  return data;
};

const normalizePos = (p) => ({
  id: p.trade_id || p.id,
  dbId: p.id,
  symbol: p.symbol || '?',
  type: p.asset_class || p.type || 'LIVE',
  assetClass: p.asset_class || 'crypto',
  dir: (p.direction || p.side || p.dir || 'LONG').toString().toUpperCase(),
  entry: Number(p.entry_price ?? p.entry ?? 0),
  current: Number(p.current_price ?? p.current ?? p.entry_price ?? p.entry ?? 0),
  sl: Number(p.stop_loss ?? p.sl ?? p.entry_price ?? p.entry ?? 0),
  tp: Number(p.take_profit ?? p.tp ?? p.entry_price ?? p.entry ?? 0),
  pnlD: Number(p.unrealized_pnl ?? p.pnlD ?? 0),
  pnlP: Number(p.pnl_pct ?? p.pnlP ?? 0),
  opened: p.entry_time || p.opened || '—',
  toSL: Number(p.distance_to_sl ?? p.toSL ?? 0),
  toTP: Number(p.distance_to_tp ?? p.toTP ?? 0),
});

// ===== Manage Positions =====
function ManagePositionsPanel() {
  const [positions, setPositions] = uS([]);
  const [chartSym, setChartSym] = uS(null);
  const [msg, setMsg] = uS('');
  const [refreshing, setRefreshing] = uS(false);
  const [lastRefresh, setLastRefresh] = uS('');
  const [closingIds, setClosingIds] = uS({});
  const [closingAll, setClosingAll] = uS(false);
  const [closeAllConfirm, setCloseAllConfirm] = uS(0);
  const loadingRef = uR(false);

  const markClosing = (id, active) => {
    if (id == null) return;
    const key = String(id);
    setClosingIds(prev => {
      const next = { ...prev };
      if (active) next[key] = true;
      else delete next[key];
      return next;
    });
  };

  const loadPositions = async ({ manual = false, quiet = false } = {}) => {
    if (loadingRef.current && !manual) return;
    loadingRef.current = true;
    if (manual) {
      setRefreshing(true);
      setTimeout(() => setRefreshing(false), 650);
      setMsg('Refreshing live positions...');
    }
    try {
      const data = await apiCall('/api/open_positions');
      const rows = Array.isArray(data.positions) ? data.positions.map(normalizePos) : [];
      setPositions(rows);
      setLastRefresh(new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
      if (!quiet) setMsg(`Loaded ${rows.length} live position(s)`);
    } catch (e) {
      setMsg(`Load failed: ${e.message}`);
    } finally {
      loadingRef.current = false;
    }
  };

  uE(() => {
    loadPositions();
    const t = setInterval(loadPositions, 10000);
    return () => clearInterval(t);
  }, []);

  const closeNow = async (p) => {
    if (!p?.id) {
      setMsg(`Cannot close ${p?.symbol || '?'}: missing position id`);
      return;
    }
    markClosing(p.id, true);
    setMsg(`Close requested for ${p.symbol}; waiting for broker handoff.`);
    try {
      const data = await apiCall('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'close_position', position_id: p.id }),
      });
      setMsg(data.message || `Close queued for ${p.symbol}`);
      setTimeout(() => loadPositions({ quiet: true }), 1500);
      setTimeout(() => loadPositions({ quiet: true }), 8000);
    } catch (e) {
      setMsg(`Close failed: ${e.message}`);
    } finally {
      setTimeout(() => markClosing(p.id, false), 1200);
    }
  };

  const closablePositions = () => positions.filter(p => p.id != null);

  const requestCloseAll = () => {
    const closable = closablePositions();
    if (!closable.length || closingAll) return;
    setCloseAllConfirm(1);
    setMsg(`Close-all confirmation required for ${closable.length} position(s).`);
  };

  const cancelCloseAll = () => {
    setCloseAllConfirm(0);
    setMsg('Close-all aborted. No positions were touched.');
  };

  const confirmCloseAll = async () => {
    if (closeAllConfirm === 1) {
      setCloseAllConfirm(2);
      return;
    }
    const closable = positions.filter(p => p.id != null);
    if (!closable.length || closingAll) {
      setCloseAllConfirm(0);
      return;
    }
    setCloseAllConfirm(0);
    setClosingAll(true);
    closable.forEach(p => markClosing(p.id, true));
    setMsg(`Close-all requested for ${closable.length} position(s).`);
    try {
      const results = await Promise.allSettled(
        closable.map(p => apiCall('/api/control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'close_position', position_id: p.id }),
          }))
      );
      const failed = results.filter(r => r.status === 'rejected').length;
      setMsg(failed ? `Close-all queued with ${failed} request failure(s).` : `Close-all queued for ${closable.length} position(s).`);
      setTimeout(() => loadPositions({ quiet: true }), 1500);
      setTimeout(() => loadPositions({ quiet: true }), 8000);
    } catch (e) {
      setMsg(`Close-all failed: ${e.message}`);
    } finally {
      setClosingAll(false);
      setTimeout(() => closable.forEach(p => markClosing(p.id, false)), 1200);
    }
  };

  return (
    <div style={{ padding: 14 }}>
      <div className="banner info" style={{ marginBottom: 12 }}>
        <Icon name="alert" size={14} />
        <span>Live backend mode: Close actions hit /api/control. Refresh pulls /api/open_positions.</span>
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
        <button className="btn ghost sm" onClick={() => loadPositions({ manual: true })}><Icon name="refresh" size={12} />{refreshing ? 'Refreshing...' : 'Refresh'}</button>
        <button className="btn ghost sm" onClick={() => window.exportCsv?.('manage-positions.csv', [
          { label: 'ID', value: 'id' },
          { label: 'DB ID', value: 'dbId' },
          { label: 'Symbol', value: 'symbol' },
          { label: 'Asset Class', value: 'assetClass' },
          { label: 'Type', value: 'type' },
          { label: 'Dir', value: 'dir' },
          { label: 'Entry', value: r => fmtN(r.entry) },
          { label: 'Current', value: r => fmtN(r.current) },
          { label: 'Stop Loss', value: r => fmtN(r.sl) },
          { label: 'Take Profit', value: r => fmtN(r.tp) },
          { label: 'Unrealized P&L', value: r => r.pnlD.toFixed(2) },
          { label: 'P&L %', value: r => r.pnlP.toFixed(2) },
          { label: 'Opened', value: 'opened' },
          { label: 'To SL %', value: r => r.toSL.toFixed(2) },
          { label: 'To TP %', value: r => r.toTP.toFixed(2) },
        ], positions)}><Icon name="download" size={12} />CSV</button>
        <button className="btn danger sm" onClick={requestCloseAll} disabled={closingAll || positions.length === 0}><Icon name="x" size={12} />{closingAll ? 'Queueing...' : 'Close All'}</button>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-2)' }}>{positions.length} open{lastRefresh ? ` · refreshed ${lastRefresh}` : ''}</span>
      </div>

      {msg && <div style={{ marginBottom: 10, fontSize: 11, color: 'var(--text-2)' }}>{msg}</div>}

      {closeAllConfirm > 0 && (
        <div style={{ position: 'sticky', top: 8, zIndex: 20, marginBottom: 12, padding: 14, border: '1px solid rgba(244, 63, 94, 0.65)', borderRadius: 'var(--radius)', background: 'rgba(69, 10, 10, 0.96)', boxShadow: '0 12px 30px rgba(0,0,0,0.35)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, color: '#fecaca', fontWeight: 800, fontSize: 13 }}>
            <Icon name="alert" size={15} />
            {closeAllConfirm === 1
              ? `You're about to close ${closablePositions().length} trade(s)! Are you sure?`
              : `Last chance: are you SURE you want to close ${closablePositions().length} trade(s)?`}
          </div>
          <div style={{ fontSize: 11, color: '#fecaca', marginBottom: 12 }}>
            {closeAllConfirm === 1
              ? 'This will submit close requests for every open position with an ID.'
              : 'Clicking Yes now will hand close orders to the backend immediately.'}
          </div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button className="btn ghost sm" onClick={cancelCloseAll}>No, abort</button>
            <button className="btn danger sm" onClick={confirmCloseAll}>
              {closeAllConfirm === 1 ? 'Yes, continue' : 'Yes, close all'}
            </button>
          </div>
        </div>
      )}

      {positions.map((p, i) => {
        const slPct = Math.abs((p.current - p.sl) / (p.entry || 1) * 100);
        const tpPct = Math.abs((p.current - p.tp) / (p.entry || 1) * 100);
        const total = Math.max(0.0001, slPct + tpPct);
        const slBar = (slPct / total) * 100;
        const isChart = chartSym === p.symbol;
        const rowClosing = !!closingIds[String(p.id)];
        return (
          <div key={p.id ?? `${p.symbol}-${i}`} className="pos-card">
            <div className="row1">
              <span className="sym">{p.symbol}</span>
              <span className={cls('pill', p.dir.toLowerCase())} style={{ fontSize: 9.5 }}>{p.dir}</span>
              <span className="meta">ID {p.id ?? '—'} · Entry {fmtN(p.entry)} · Cur {fmtN(p.current)} · SL {fmtN(p.sl)} · TP {fmtN(p.tp)}</span>
              <span className={cls('pnl', p.pnlD >= 0 ? 'pos' : 'neg')} style={{ color: p.pnlD >= 0 ? 'var(--green)' : 'var(--red)' }}>
                {fmt$Sign(p.pnlD)} ({fmtP(p.pnlP)})
              </span>
            </div>

            <div className="progress"><div style={{ width: slBar + '%', background: 'linear-gradient(90deg, var(--red), var(--yellow), var(--green))' }} /></div>

            <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
              <button className="btn ghost sm" onClick={() => setChartSym(isChart ? null : p.symbol)}>
                <Icon name="candles" size={12} /> {isChart ? 'Hide chart' : 'View chart'}
              </button>
              <button className="btn ghost sm"><Icon name="edit" size={12} />Adjust SL/TP</button>
              <span className="spacer" />
              <button className="btn danger sm" onClick={() => closeNow(p)} disabled={rowClosing || p.id == null}>
                <Icon name="x" size={12} />{rowClosing ? 'Closing...' : 'Close Now'}
              </button>
            </div>

            {isChart && (
              <div style={{ marginTop: 10 }}>
                <MiniLWChart symbol={p.symbol} assetClass={p.assetClass} entry={p.entry} sl={p.sl} tp={p.tp} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function MiniLWChart({ symbol, assetClass, entry, sl, tp }) {
  const containerRef = uR(null);
  const [status, setStatus] = uS('Loading chart...');

  // Single effect: create chart, load data, wire resize — all in guaranteed order.
  // Re-runs if symbol or assetClass change (destroys old chart, builds new one).
  uE(() => {
    const LC = window.LightweightCharts;
    const el  = containerRef.current;
    if (!LC || !el) { setStatus('Chart library unavailable.'); return; }

    let cancelled = false;

    // ── Build chart ──────────────────────────────────────────────────
    const chart = LC.createChart(el, {
      width:  el.offsetWidth  || 500,
      height: 240,
      layout: { background: { type: 'solid', color: '#0d1117' }, textColor: '#8b949e', fontSize: 11 },
      grid:   { vertLines: { color: '#1a2035' }, horzLines: { color: '#1a2035' } },
      crosshair: { mode: LC.CrosshairMode.Normal },
      handleScale:  { axisPressedMouseMove: { time: true, price: true }, mouseWheel: true, pinch: true },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
      rightPriceScale: { borderColor: '#2a3347', autoScale: true },
      timeScale: {
        borderColor: '#2a3347', timeVisible: true, secondsVisible: false,
        tickMarkFormatter: (ts, type) => {
          const d = new Date(ts * 1000), tz = { timeZone: 'America/Los_Angeles' };
          if (type <= 1) return d.toLocaleDateString('en-US', { ...tz, month: 'short', year: 'numeric' });
          if (type === 2) return d.toLocaleDateString('en-US', { ...tz, month: 'numeric', day: 'numeric' });
          return d.toLocaleTimeString('en-US', { ...tz, hour: 'numeric', minute: '2-digit', hour12: true });
        },
      },
    });

    const series = chart.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    });

    // Entry / SL / TP price lines
    const LS = LC.LineStyle;
    if (entry) series.createPriceLine({ price: +entry, color: '#60a5fa', lineWidth: 1, lineStyle: LS.Dashed, title: 'Entry', axisLabelVisible: true });
    if (sl)    series.createPriceLine({ price: +sl,    color: '#f43f5e', lineWidth: 1, lineStyle: LS.Dashed, title: 'SL',    axisLabelVisible: true });
    if (tp)    series.createPriceLine({ price: +tp,    color: '#10b981', lineWidth: 1, lineStyle: LS.Dashed, title: 'TP',    axisLabelVisible: true });

    // Keep width in sync
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect.width;
      if (w > 0) chart.resize(w, 240);
    });
    ro.observe(el);

    // ── Fetch candles (after chart exists, no ref needed) ────────────
    const toSec = t => {
      const n = Number(t);
      if (!isNaN(n)) return n > 1e10 ? Math.floor(n / 1000) : Math.floor(n);
      return Math.floor(new Date(String(t).replace(' ', 'T')).getTime() / 1000);
    };

    (async () => {
      try {
        const qs = new URLSearchParams({
          symbol,
          asset_class: assetClass || 'stock',
          timeframe: '5 Min',
          limit: '300',
        });
        const data = await apiCall(`/api/candles?${qs}`);
        if (cancelled) return;

        const raw = data.candles || [];
        const rows = raw.map(c => ({
          time:  toSec(c.time),
          open:  +c.open, high: +c.high, low: +c.low, close: +c.close,
        })).filter(c => c.time > 0 && c.open > 0);


        if (!rows.length) {
          setStatus(`No data — API returned ${raw.length} candle(s), 0 valid`);
          return;
        }
        series.setData(rows);
        chart.timeScale().fitContent();
        setStatus('');
      } catch(e) {
        if (!cancelled) setStatus(`Fetch error: ${e.message}`);
      }
    })();

    return () => {
      cancelled = true;
      ro.disconnect();
      chart.remove();
    };
  }, [symbol, assetClass]);

  return (
    <div style={{ position: 'relative', borderRadius: 4, overflow: 'hidden', border: '1px solid var(--border)' }}>
      {status && (
        <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center',
          background: '#0d1117', color: '#8b949e', fontSize: 11, zIndex: 2, pointerEvents: 'none' }}>
          {status}
        </div>
      )}
      <div ref={containerRef} style={{ width: '100%', height: 240 }} />
    </div>
  );
}

// ===== Inject Symbols =====
function InjectSymbolsPanel() {
  const [stocks, setStocks] = uS([]);
  const [crypto, setCrypto] = uS([]);
  const [stockInput, setStockInput] = uS('AAPL\nMSFT\nNVDA');
  const [cryptoInput, setCryptoInput] = uS('BTC/USD\nETH/USD\nSOL/USD');
  const [msg, setMsg] = uS('');
  const [busy, setBusy] = uS(false);

  const loadInjected = async () => {
    try {
      const data = await apiCall('/api/injected_symbols');
      setStocks(Array.isArray(data.stocks) ? data.stocks : []);
      setCrypto(Array.isArray(data.crypto) ? data.crypto : []);
    } catch (e) {
      setMsg(`Load failed: ${e.message}`);
    }
  };

  uE(() => { loadInjected(); }, []);

  const inject = async (kind, replace = false) => {
    const raw = (kind === 'stock' ? stockInput : cryptoInput).split(/[\n,]/).map(s => s.trim()).filter(Boolean);
    setBusy(true);
    try {
      const data = await apiCall('/api/injected_symbols', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, symbols: raw, replace }),
      });
      setStocks(Array.isArray(data.stocks) ? data.stocks : []);
      setCrypto(Array.isArray(data.crypto) ? data.crypto : []);
      setMsg(data.message || 'Injected symbols updated.');
    } catch (e) {
      setMsg(`Inject failed: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const loadFromDb = (kind) => {
    if (kind === 'stock') {
      setStockInput(stocks.join(', '));
      setMsg(`Loaded ${stocks.length} stock(s) from database into editor.`);
    } else {
      setCryptoInput(crypto.join(', '));
      setMsg(`Loaded ${crypto.length} crypto pair(s) from database into editor.`);
    }
  };

  const clearInjected = async () => {
    setBusy(true);
    try {
      const data = await apiCall('/api/injected_symbols/clear', { method: 'POST' });
      setStocks(Array.isArray(data.stocks) ? data.stocks : []);
      setCrypto(Array.isArray(data.crypto) ? data.crypto : []);
      setMsg(data.message || 'Injected symbols cleared.');
    } catch (e) {
      setMsg(`Clear failed: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ padding: 16 }}>
      <div className="panel-sub" style={{ marginBottom: 14 }}>
        Spotted something? Add it here — temporary for today only. Resets on next scan or reboot.
      </div>
      {msg && <div style={{ marginBottom: 10, fontSize: 11, color: 'var(--text-2)' }}>{msg}</div>}
      <div className="grid-2" style={{ marginBottom: 16 }}>
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 14 }}>
          <h3 style={{ fontSize: 11, fontWeight: 600, color: 'var(--blue)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Icon name="trending" size={13} />Stocks
          </h3>
          <div className="field" style={{ marginBottom: 6 }}>
            <label style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span>Tickers</span>
              <button
                className="btn ghost sm"
                style={{ fontSize: 10, padding: '2px 7px', opacity: stocks.length ? 1 : 0.4 }}
                onClick={() => loadFromDb('stock')}
                disabled={busy || !stocks.length}
                title="Fill editor with current database contents"
              >↙ Load from DB ({stocks.length})</button>
            </label>
            <textarea className="input textarea" rows={4} value={stockInput} onChange={e => setStockInput(e.target.value)}
              placeholder="AAPL, MSFT, NVDA&#10;One per line or comma-separated" />
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="btn primary" style={{ flex: 1 }} onClick={() => inject('stock', true)} disabled={busy} title="Replace database with exactly these symbols — removes anything not listed">
              <Icon name="refresh" size={12} />Replace Stocks
            </button>
            <button className="btn ghost" style={{ flex: 1 }} onClick={() => inject('stock', false)} disabled={busy} title="Add these symbols to the database without removing existing ones">
              <Icon name="plus" size={12} />Add Stocks
            </button>
          </div>
        </div>
        <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 14 }}>
          <h3 style={{ fontSize: 11, fontWeight: 600, color: 'var(--yellow)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Icon name="coins" size={13} />Crypto
          </h3>
          <div className="field" style={{ marginBottom: 6 }}>
            <label style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span>Pairs</span>
              <button
                className="btn ghost sm"
                style={{ fontSize: 10, padding: '2px 7px', opacity: crypto.length ? 1 : 0.4 }}
                onClick={() => loadFromDb('crypto')}
                disabled={busy || !crypto.length}
                title="Fill editor with current database contents"
              >↙ Load from DB ({crypto.length})</button>
            </label>
            <textarea className="input textarea" rows={4} value={cryptoInput} onChange={e => setCryptoInput(e.target.value)}
              placeholder="BTC/USD, ETH/USD&#10;One per line or comma-separated" />
            <div style={{ fontSize: 10.5, color: 'var(--text-2)', marginTop: 6 }}>
              Bare symbols auto-get /USD. Replace removes anything not in the list.
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="btn primary" style={{ flex: 1 }} onClick={() => inject('crypto', true)} disabled={busy} title="Replace database with exactly these pairs — removes anything not listed">
              <Icon name="refresh" size={12} />Replace Crypto
            </button>
            <button className="btn ghost" style={{ flex: 1 }} onClick={() => inject('crypto', false)} disabled={busy} title="Add these pairs to the database without removing existing ones">
              <Icon name="plus" size={12} />Add Crypto
            </button>
          </div>
        </div>
      </div>
      {(stocks.length > 0 || crypto.length > 0) && (
        <div style={{ marginBottom: 12, display: 'grid', gap: 8 }}>
          {stocks.length > 0 && <div className="banner info"><Icon name="trending" size={13} /><span>Stocks ({stocks.length}): {stocks.join(', ')}</span></div>}
          {crypto.length > 0 && <div className="banner info"><Icon name="coins" size={13} /><span>Crypto ({crypto.length}): {crypto.join(', ')}</span></div>}
        </div>
      )}
      <button className="btn ghost full" onClick={clearInjected} disabled={busy}><Icon name="trash" size={12} />Clear All Injected Symbols</button>
    </div>
  );
}

// ===== Quick Controls (Bot, Risk, Tax, Withdrawal) =====
function QuickControlsPanel() {
  const [running, setRunning] = uS(true);
  const [paused, setPaused] = uS(false);
  const [maxPos, setMaxPos] = uS(2);
  const [taxYear, setTaxYear] = uS(2026);
  const [wAmount, setWAmount] = uS('0.00');
  const [wReason, setWReason] = uS('Living expenses');
  const [mlTraining, setMlTraining] = uS(false);
  const [quickStatus, setQuickStatus] = uS({ ml: {}, tax: {}, market_condition: {} });
  const [busy, setBusy] = uS(false);
  const [msg, setMsg] = uS('');

  const loadQuickStatus = async () => {
    try {
      const data = await apiCall(`/api/quick_status?year=${taxYear}`);
      setQuickStatus(data);
    } catch {}
  };

  const refreshSnapshot = async () => {
    try {
      const snap = await apiCall('/api/snapshot');
      setRunning(!!snap.bot_process_running);
      if (typeof snap.trading_active === 'boolean') {
        setPaused(!snap.trading_active);
      } else if (snap.bot_status === 'PAUSED') {
        setPaused(true);
      } else if (snap.bot_status === 'ACTIVE') {
        setPaused(false);
      }
    } catch {}
  };

  uE(() => {
    refreshSnapshot();
    loadQuickStatus();
    const t = setInterval(refreshSnapshot, 5000);
    const q = setInterval(loadQuickStatus, 30000);
    return () => { clearInterval(t); clearInterval(q); };
  }, []);

  uE(() => { loadQuickStatus(); }, [taxYear]);

  const callControl = async (action, extras = {}) => {
    setBusy(true);
    try {
      const data = await apiCall('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, ...extras })
      });
      setMsg(data.message || data.error || 'Done');
      if (action === 'pause_trading') setPaused(true);
      if (action === 'resume_trading') setPaused(false);
      await refreshSnapshot();
    } catch (e) {
      setMsg(`Control failed: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const postAction = async (path, body = {}) => {
    setBusy(true);
    try {
      const data = await apiCall(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setMsg(data.message || 'Done');
      await loadQuickStatus();
      await refreshSnapshot();
      return data;
    } catch (e) {
      setMsg(`Action failed: ${e.message}`);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const triggerML = async () => {
    setMlTraining(true);
    await postAction('/api/ml/retrain');
    setMlTraining(false);
  };

  const ml = quickStatus.ml || {};
  const tax = quickStatus.tax || {};
  const mlStage = String(ml.stage || 'unknown').replace('_', ' ');
  const mlProgress = Math.min(100, Math.max(0, Number(ml.progress_pct || 0)));
  const mlActive = ml.stage === 'active';

  return (
    <div style={{ padding: 14 }}>
      {/* Bot Process Control */}
      <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 14, marginBottom: 12 }}>
        <h3 style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-1)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="cpu" size={13} />Bot Process Control
        </h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <span className="dot" style={{ width: 8, height: 8, borderRadius: '50%', background: running ? 'var(--green)' : 'var(--red)', boxShadow: running ? '0 0 8px var(--green)' : 'none' }}></span>
          <span style={{ fontSize: 12, fontWeight: 500 }}>{running ? 'Bot Running' : 'Bot Stopped'}</span>
          <span className="tag" style={{ marginLeft: 'auto' }}>{busy ? 'Working…' : 'Live'}</span>
        </div>
        <div className="grid-2">
          <button className="btn ghost" onClick={() => callControl('restart_bot')} disabled={busy}>
            <Icon name="refresh" size={12} />Restart Bot
          </button>
          <button className="btn danger" onClick={() => callControl('stop_bot')} disabled={busy}>
            <Icon name="stop" size={12} />Stop Bot
          </button>
        </div>
        <div style={{ marginTop: 8, display:'flex', gap:6 }}>
          <button className="btn full" onClick={() => callControl('open_db_panel')} disabled={busy}
            style={{ background: '#0f3460', color: '#e0e0e0', border: '1px solid #2a4a7f', flex:1 }}>
            🗄 DB Panel (desktop)
          </button>
          <a href="/db_browser.html" target="_blank"
            style={{ background: '#0f3460', color: '#e0e0e0', border: '1px solid #2a4a7f',
                     padding:'5px 10px', borderRadius:'var(--radius)', fontSize:11,
                     textDecoration:'none', display:'flex', alignItems:'center' }}>
            🌐 Web
          </a>
        </div>
        {msg && <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-2)' }}>{msg}</div>}
      </div>

      {/* Trading Control */}
      <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 14, marginBottom: 12 }}>
        <h3 style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-1)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="activity" size={13} />Trading Control
        </h3>
        <button
          className={cls('btn full', paused ? 'primary' : 'ghost')}
          onClick={() => callControl(paused ? 'resume_trading' : 'pause_trading')}
          disabled={busy}
        >
          {paused ? <><Icon name="play" size={12} />Resume Trading</> : <><Icon name="pause" size={12} />Pause Trading</>}
        </button>
      </div>

      {/* Risk Settings */}
      <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 14, marginBottom: 12 }}>
        <h3 style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-1)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="sliders" size={13} />Risk Settings
        </h3>
        <div className="field">
          <label style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span>Max Position %</span>
            <span style={{ fontFamily: 'var(--mono)', color: 'var(--accent-bright)' }}>{maxPos}%</span>
          </label>
          <input type="range" min="0.5" max="10" step="0.5" value={maxPos} onChange={e => setMaxPos(+e.target.value)} />
        </div>
      </div>

      {/* Tax & Reports */}
      <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 14, marginBottom: 12 }}>
        <h3 style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-1)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="file" size={13} />Tax & Reports
        </h3>
        <button className="btn ghost full" style={{ marginBottom: 10 }} onClick={() => postAction('/api/report/daily')} disabled={busy}><Icon name="file" size={12} />Generate Today's Report</button>
        <div className="field" style={{ marginBottom: 10 }}>
          <label>Tax Year</label>
          <input className="input" type="number" value={taxYear} onChange={e => setTaxYear(+e.target.value)} />
        </div>
        <button className="btn ghost full" onClick={() => postAction('/api/tax/export', { year: taxYear })} disabled={busy}><Icon name="download" size={12} />Export Form 8949 CSV</button>
      </div>

      {/* Withdrawal */}
      <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 14, marginBottom: 16 }}>
        <h3 style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-1)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="wallet" size={13} />Withdrawal
        </h3>
        <div className="field" style={{ marginBottom: 10 }}>
          <label>Amount ($)</label>
          <input className="input" value={wAmount} onChange={e => setWAmount(e.target.value)} />
        </div>
        <div className="field" style={{ marginBottom: 10 }}>
          <label>Reason</label>
          <input className="input" value={wReason} onChange={e => setWReason(e.target.value)} />
        </div>
        <button className="btn primary full" disabled={busy || parseFloat(wAmount) <= 0} onClick={() => postAction('/api/withdrawal', { amount: parseFloat(wAmount), reason: wReason })}><Icon name="arrow" size={12} />Process Withdrawal</button>
      </div>

      {/* YTD Tax Summary */}
      <div style={{ borderTop: '1px solid var(--border)', paddingTop: 16, marginBottom: 18 }}>
        <h3 style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)', marginBottom: 18 }}>
          YTD Tax Summary ({taxYear})
        </h3>
        <div style={{ marginBottom: 18 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-1)', marginBottom: 6 }}>Total Gains/Losses</div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 30, lineHeight: 1, color: 'var(--text-1)' }}>{fmt$(Number(tax.total_gains || 0))}</div>
        </div>
        <div style={{ marginBottom: 18 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-1)', marginBottom: 6 }}>Short-Term</div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 30, lineHeight: 1, color: 'var(--text-1)' }}>{fmt$(Number(tax.short_term_gains || 0))}</div>
        </div>
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-1)', marginBottom: 6 }}>Long-Term</div>
          <div style={{ fontFamily: 'var(--mono)', fontSize: 30, lineHeight: 1, color: 'var(--text-1)' }}>{fmt$(Number(tax.long_term_gains || 0))}</div>
        </div>
      </div>

      {/* ML Scorer */}
      <div style={{ borderTop: '1px solid var(--border)', paddingTop: 16 }}>
        <h3 style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-1)', marginBottom: 16, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="layers" size={13} />ML Scorer
        </h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, fontWeight: 700, marginBottom: 18 }}>
          <span style={{ width: 14, height: 14, borderRadius: '50%', background: mlActive ? 'var(--green)' : 'var(--yellow)' }}></span>
          <span>{mlActive ? 'Active' : mlStage}</span>
        </div>
        <div style={{ height: 8, background: 'var(--bg-0)', borderRadius: 4, overflow: 'hidden', marginBottom: 10 }}>
          <div style={{ width: `${mlProgress}%`, height: '100%', background: 'var(--red)' }} />
        </div>
        <div style={{ fontSize: 11, lineHeight: 1.6, color: 'var(--text-2)', marginBottom: 16, fontWeight: 600 }}>{ml.message || 'ML scorer initializing.'}</div>
        <button className="btn ghost full" onClick={triggerML} disabled={mlTraining || busy}>{mlTraining ? <><Icon name="cpu" size={12} />Training…</> : <><Icon name="cpu" size={12} />Retrain ML Now</>}</button>
      </div>
    </div>
  );
}

// ===== PowerShell Workspace =====
function TerminalPanel() {
  const [busy, setBusy] = uS(false);
  const [msg, setMsg] = uS('');
  const [copied, setCopied] = uS('');
  const commands = [
    { label: 'Watch bot log', cmd: 'Get-Content .\\logs\\bot.log -Tail 80 -Wait' },
    { label: 'Find errors', cmd: 'Select-String -Path .\\logs\\*.log -Pattern "ERROR|Traceback|Exception|DEGEN|FOREST|BOBBOB|PTB|ASSET|MATH|SPY"' },
    { label: 'Find scanner errors', cmd: 'Select-String -Path *.log -Pattern "SPY|ESCALATE 2"' },
    { label: 'Recent scanner log', cmd: 'Get-Content .\\scanner.log -Tail 120' },
    { label: 'Compile dashboard bridge', cmd: 'python -m py_compile .\\web_dashboard.py' },
    { label: 'Run dashboard bridge', cmd: 'python .\\web_dashboard.py' },
    { label: 'Run bot engine', cmd: 'python .\\bot_engine.py' },
  ];
  const openShell = async () => {
    setBusy(true);
    setMsg('Opening PowerShell...');
    try {
      const data = await apiCall('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'open_terminal' }),
      });
      setMsg(data.message || 'PowerShell opened.');
    } catch (e) {
      setMsg(`PowerShell failed: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const copyCommand = async (cmd, label) => {
    try {
      await navigator.clipboard.writeText(cmd);
      setCopied(label);
      setTimeout(() => setCopied(''), 1400);
    } catch {
      setCopied('Copy failed');
    }
  };

  return (
    <div style={{ padding: 14 }}>
      <div className="banner info" style={{ marginBottom: 12 }}>
        <Icon name="terminal" size={14} />
        <span>Opens a normal PowerShell in the trading bot folder. Your $PROFILE can handle the venv.</span>
      </div>

      <button className="btn primary full" onClick={openShell} disabled={busy} style={{ marginBottom: 12 }}>
        <Icon name="terminal" size={13} />{busy ? 'Opening...' : 'Open PowerShell'}
      </button>
      {msg && <div style={{ marginBottom: 14, fontSize: 11, color: 'var(--text-2)' }}>{msg}</div>}

      <div style={{ background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', padding: 12, marginBottom: 12 }}>
        <h3 style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-1)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="copy" size={13} />Command Shortcuts
        </h3>
        <div style={{ display: 'grid', gap: 8 }}>
          {commands.map(item => (
            <div key={item.label} style={{ display: 'grid', gridTemplateColumns: '1fr auto', gap: 8, alignItems: 'center', padding: 8, border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg-1)' }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 11, color: 'var(--text-1)', fontWeight: 700, marginBottom: 3 }}>{item.label}</div>
                <code style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 11, color: 'var(--text-2)' }}>{item.cmd}</code>
              </div>
              <button className="btn ghost sm" onClick={() => copyCommand(item.cmd, item.label)} title={`Copy ${item.label}`}>
                <Icon name="copy" size={12} />Copy
              </button>
            </div>
          ))}
        </div>
        {copied && <div style={{ marginTop: 10, fontSize: 11, color: copied === 'Copy failed' ? 'var(--red)' : 'var(--green)' }}>{copied === 'Copy failed' ? copied : `${copied} copied`}</div>}
      </div>

      <div style={{ fontSize: 11, lineHeight: 1.6, color: 'var(--text-2)' }}>
        This panel only launches a local shell and copies helper commands; commands still run inside PowerShell where you can see and stop them.
      </div>
    </div>
  );
}

// ===== DB Browser Panel =====
const QUICK_QUERIES_DB = [
  { label: 'Open Today',       view: 'trades' },
  { label: 'Closed Today',     view: 'trades' },
  { label: 'All Open',         view: 'trades' },
  { label: '☠ Ghost Trades',   view: 'trades' },
  { label: 'Capital Today',    view: 'capital' },
  { label: 'Settlement Queue', view: 'settlement_queue' },
  { label: 'By Strategy',      view: 'trades' },
  { label: 'Strategies',       view: '_strategies' },
  { label: 'Daily P&L',        view: 'daily_summaries' },
  { label: 'Yesterday Closed', view: 'trades' },
  { label: 'This Week Trades', view: 'trades' },
  { label: 'Strategy P&L',    view: 'trades' },
  { label: '💰 Financials',   view: 'capital' },
  { label: 'Signal Reviews',   view: 'ai_signal_reviews' },
];

function DBBrowserPanel() {
  const [tables,      setTables]      = uS([]);
  const [cols,        setCols]        = uS([]);
  const [rows,        setRows]        = uS([]);
  const [rowids,      setRowids]      = uS([]);
  const [curTable,    setCurTable]    = uS(null);
  const [curView,     setCurView]     = uS(null);
  const [selIdx,      setSelIdx]      = uS(null);
  const [sortCol,     setSortCol]     = uS(null);
  const [sortAsc,     setSortAsc]     = uS(true);
  const [gridTitle,   setGridTitle]   = uS('Select a table or click a query');
  const [status,      setStatus]      = uS({ msg: 'Ready', type: '' });
  const [editOpen,     setEditOpen]    = uS(false);
  const [editFields,   setEditFields]  = uS({});
  const [editOriginal, setEditOriginal]= uS({});
  const [editMeta,     setEditMeta]    = uS({});
  // search fields
  const [fStrat,  setFStrat]  = uS('');
  const [fStatus, setFStatus] = uS('any');
  const [fTime,   setFTime]   = uS('any');
  const [fSym,    setFSym]    = uS('');
  const [fBroker, setFBroker] = uS('any');

  const st = (msg, type='') => setStatus({ msg, type });

  uE(() => { loadTables(); }, []);

  async function loadTables() {
    try {
      const d = await apiCall('/api/db/tables');
      setTables(d.tables || []);
    } catch(e) { st(e.message, 'err'); }
  }

  function applyResult(data, title) {
    if (data.error) { st(data.error, 'err'); return; }
    if (!data.rows || !data.cols) { st('No data returned', 'err'); return; }
    const hasRowid = data.cols[0] === 'rowid';
    setRowids(hasRowid ? data.rows.map(r => r[0]) : data.rows.map(() => null));
    setCols(hasRowid ? data.cols.slice(1) : data.cols);
    setRows(hasRowid ? data.rows.map(r => r.slice(1)) : data.rows);
    setSelIdx(null); setSortCol(null);
    setGridTitle(title);
    st(`${data.rows.length.toLocaleString()} rows · ${title}`);
  }

  async function browseTable(table) {
    setCurTable(table); setCurView(null);
    st(`Loading ${table}…`);
    try {
      const d = await apiCall(`/api/db/browse?table=${encodeURIComponent(table)}&limit=500`);
      applyResult(d, table);
      loadTables();
    } catch(e) { st(e.message, 'err'); }
  }

  async function runQuick(label) {
    const cleanLabel = label.replace('☠ ','');
    setCurView(cleanLabel);
    const tbl = QUICK_QUERIES_DB.find(q => q.label === label)?.view;
    setCurTable(tbl?.startsWith('_') ? null : tbl);
    st(`Running: ${cleanLabel}…`);
    try {
      const d = await apiCall(`/api/db/quick?q=${encodeURIComponent(cleanLabel)}`);
      applyResult(d, cleanLabel);
    } catch(e) { st(e.message, 'err'); }
  }

  async function runSearch() {
    const STRAT_TABLES = ['trades','ai_signal_reviews','strategy_results'];
    const wantsStrat   = fStrat.trim() !== '';
    let table = curTable || 'trades';
    if (wantsStrat && !STRAT_TABLES.includes(table)) table = 'trades';
    st('Searching…');
    try {
      const d = await apiCall('/api/db/search', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ table, strategy: fStrat, status: fStatus,
                               time: fTime, symbol: fSym, broker: fBroker })
      });
      setCurTable(table); setCurView(null);
      applyResult(d, `Search → ${table}`);
    } catch(e) { st(e.message, 'err'); }
  }

  async function refresh() {
    if (curView) { await runQuick(QUICK_QUERIES_DB.find(q => q.label.includes(curView))?.label || curView); }
    else if (curTable) { await browseTable(curTable); }
    await loadTables();
  }

  function selectRow(idx) { setSelIdx(idx); }

  function sortBy(ci) {
    const asc = sortCol === ci ? !sortAsc : true;
    setSortCol(ci); setSortAsc(asc);
    const paired = rows.map((r, i) => [r, rowids[i]]);
    paired.sort((a, b) => {
      const av = a[0][ci], bv = b[0][ci];
      const an = parseFloat(av), bn = parseFloat(bv);
      const c = (!isNaN(an) && !isNaN(bn)) ? an - bn : String(av||'').localeCompare(String(bv||''));
      return asc ? c : -c;
    });
    setRows(paired.map(p => p[0]));
    setRowids(paired.map(p => p[1]));
    setSelIdx(null);
  }

  function openEdit() {
    if (selIdx === null) { st('Select a row first', 'err'); return; }
    if (!curTable || curTable.startsWith('_')) { st('Browse a real table first', 'err'); return; }
    const row = rows[selIdx];
    const fields = {};
    cols.forEach((c, i) => { fields[c] = row[i] ?? ''; });
    setEditFields(fields);
    setEditOriginal({...fields});
    setEditMeta({ table: curTable, rowid: rowids[selIdx] });
    setEditOpen(true);
  }

  async function saveEdit() {
    const { table, rowid } = editMeta;
    const tradeId = editFields['trade_id'] || null;
    if (rowid === null && !tradeId) { st('Cannot determine row ID — browse from sidebar', 'err'); return; }
    const changed = Object.entries(editFields).filter(([col, val]) => String(val) !== String(editOriginal[col] ?? ''));
    if (changed.length === 0) { setEditOpen(false); st('No changes made', ''); return; }
    let saved = 0, errors = [];
    for (const [col, value] of changed) {
      try {
        await apiCall('/api/db/edit_row', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ table, rowid, trade_id: tradeId, col, value })
        });
        saved++;
      } catch(e) { errors.push(`${col}: ${e.message}`); }
    }
    setEditOpen(false);
    if (errors.length === 0) {
      st(`Saved: ${changed.map(([c])=>c).join(', ')}`, 'ok');
    } else {
      st(`${saved} saved, ${errors.length} failed: ${errors[0]}`, 'err');
    }
    refresh();
  }

  async function flushGhost() {
    if (selIdx === null) { st('Select a row first', 'err'); return; }
    const row = rows[selIdx];
    const m = {}; cols.forEach((c, i) => m[c] = row[i]);
    const { trade_id, symbol, hours_open, status: ts } = m;
    if (!trade_id) { st('No trade_id in row', 'err'); return; }
    if (ts === 'closed') { st(`${symbol} is already closed`, 'err'); return; }
    if (!confirm(`Flush ghost trade?\n\n  ${symbol}  (${trade_id})\n  Hours open: ${hours_open||'?'}\n\nMarks CLOSED in DB. Ensure broker position is already gone.`)) return;
    try {
      const d = await apiCall('/api/db/flush_ghost', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trade_id, entry_price: m.entry_price || 0 })
      });
      st(d.ok ? `Flushed: ${symbol}` : d.message, d.ok ? 'ok' : 'err');
      if (d.ok) refresh();
    } catch(e) { st(e.message, 'err'); }
  }

  async function toggleStrategy(enable) {
    if (selIdx === null) { st('Select a strategy row first', 'err'); return; }
    const row = rows[selIdx];
    const m = {}; cols.forEach((c, i) => m[c] = row[i]);
    const strategy = m.strategy || '';
    const key = `strategy_${strategy}`;
    if (!key.endsWith('_enabled')) { st('Select a row from the Strategies view', 'err'); return; }
    const name = strategy.replace('_enabled', '');
    if (!confirm(`${enable ? 'ENABLE' : 'DISABLE'} strategy '${name}'?`)) return;
    try {
      const d = await apiCall('/api/db/strategy_toggle', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, enabled: enable })
      });
      st(d.ok ? d.message : d.message, d.ok ? 'ok' : 'err');
      if (d.ok) runQuick('Strategies');
    } catch(e) { st(e.message, 'err'); }
  }

  const isStrat  = curView === 'Strategies';
  const isTrades = curTable === 'trades' ||
    ['Open Today','Closed Today','All Open','Ghost Trades','By Strategy','Yesterday Closed'].includes(curView);

  const S = { // styles
    wrap:    { display:'flex', flexDirection:'column', height:'100%', background:'var(--bg-1)', overflow:'hidden' },
    qbar:    { display:'flex', gap:4, flexWrap:'wrap', padding:'6px 8px', borderBottom:'1px solid var(--border)', flexShrink:0 },
    qbtn:    { background:'var(--bg-3)', border:'1px solid var(--border)', color:'var(--text-1)', padding:'3px 9px', borderRadius:4, cursor:'pointer', fontSize:11, fontFamily:'inherit' },
    qbtnG:   { background:'#1a3a2a', border:'1px solid #16a34a', color:'#4ade80', padding:'3px 9px', borderRadius:4, cursor:'pointer', fontSize:11 },
    qbtnR:   { background:'#3a1a1a', border:'1px solid #dc2626', color:'#f87171', padding:'3px 9px', borderRadius:4, cursor:'pointer', fontSize:11 },
    sbar:    { display:'flex', gap:6, flexWrap:'wrap', padding:'5px 8px', borderBottom:'1px solid var(--border)', flexShrink:0, alignItems:'center' },
    lbl:     { color:'var(--text-2)', fontSize:10 },
    inp:     { background:'var(--bg-2)', border:'1px solid var(--border)', color:'var(--text-1)', padding:'3px 7px', borderRadius:4, fontSize:11, fontFamily:'inherit', width:90 },
    sel:     { background:'var(--bg-2)', border:'1px solid var(--border)', color:'var(--text-1)', padding:'3px 5px', borderRadius:4, fontSize:11, fontFamily:'inherit' },
    sbtnG:   { background:'#16a34a', border:'none', color:'#fff', padding:'3px 10px', borderRadius:4, cursor:'pointer', fontSize:11, fontWeight:700 },
    sbtnD:   { background:'var(--bg-3)', border:'1px solid var(--border)', color:'var(--text-1)', padding:'3px 8px', borderRadius:4, cursor:'pointer', fontSize:11 },
    main:    { display:'flex', flex:1, overflow:'hidden' },
    sidebar: { width:170, background:'var(--bg-2)', borderRight:'1px solid var(--border)', overflowY:'auto', flexShrink:0 },
    tblItem: (active) => ({ padding:'5px 10px', cursor:'pointer', borderBottom:'1px solid var(--bg-1)', display:'flex', justifyContent:'space-between', background: active ? '#1e3a5f' : 'transparent', borderLeft: active ? '2px solid #2563eb' : '2px solid transparent' }),
    grid:    { flex:1, display:'flex', flexDirection:'column', overflow:'hidden' },
    gtitle:  { padding:'5px 10px', borderBottom:'1px solid var(--border)', flexShrink:0, display:'flex', alignItems:'center', gap:8 },
    tcon:    { flex:1, overflow:'auto' },
    th:      (sorted) => ({ background:'#1e2d40', color: sorted ? '#58a6ff' : 'var(--text-2)', padding:'5px 8px', textAlign:'left', fontSize:10, fontWeight:700, position:'sticky', top:0, cursor:'pointer', whiteSpace:'nowrap', borderBottom:'1px solid var(--border)' }),
    td:      { padding:'4px 8px', fontSize:11, whiteSpace:'nowrap', maxWidth:200, overflow:'hidden', textOverflow:'ellipsis' },
    abar:    { display:'flex', gap:6, padding:'5px 8px', borderTop:'1px solid var(--border)', flexShrink:0, alignItems:'center' },
    abtnE:   { background:'#2563eb', border:'none', color:'#fff', padding:'4px 10px', borderRadius:4, cursor:'pointer', fontSize:11 },
    abtnF:   { background:'#dc2626', border:'none', color:'#fff', padding:'4px 10px', borderRadius:4, cursor:'pointer', fontSize:11 },
    abtnEn:  { background:'#16a34a', border:'none', color:'#fff', padding:'4px 10px', borderRadius:4, cursor:'pointer', fontSize:11 },
    abtnDis: { background:'#9f1239', border:'none', color:'#fff', padding:'4px 10px', borderRadius:4, cursor:'pointer', fontSize:11 },
    stsbar:  (type) => ({ padding:'3px 10px', fontSize:10, borderTop:'1px solid var(--border)', color: type==='ok' ? '#4ade80' : type==='err' ? '#f87171' : 'var(--text-2)', flexShrink:0 }),
  };

  return (
    <div style={S.wrap}>
      {/* Quick buttons */}
      <div style={S.qbar}>
        {QUICK_QUERIES_DB.map(q => (
          <button key={q.label} style={q.label.includes('Ghost') ? S.qbtnR : q.label==='Strategies' ? S.qbtnG : S.qbtn}
            onClick={() => runQuick(q.label)}>{q.label}</button>
        ))}
      </div>

      {/* Search bar */}
      <div style={S.sbar}>
        <span style={S.lbl}>Strategy</span>
        <input style={{...S.inp,width:100}} value={fStrat} onChange={e=>setFStrat(e.target.value)} onKeyDown={e=>e.key==='Enter'&&runSearch()} placeholder="e.g. grid_bot" />
        <span style={S.lbl}>Status</span>
        <select style={S.sel} value={fStatus} onChange={e=>setFStatus(e.target.value)}>
          {['any','open','closed','pending','cancelled'].map(v=><option key={v}>{v}</option>)}
        </select>
        <span style={S.lbl}>Time</span>
        <select style={S.sel} value={fTime} onChange={e=>setFTime(e.target.value)}>
          {['any','last 1h','last 4h','last 8h','today','yesterday','this week'].map(v=><option key={v}>{v}</option>)}
        </select>
        <span style={S.lbl}>Symbol</span>
        <input style={{...S.inp,width:75}} value={fSym} onChange={e=>setFSym(e.target.value)} onKeyDown={e=>e.key==='Enter'&&runSearch()} placeholder="AAPL" />
        <span style={S.lbl}>Broker</span>
        <select style={S.sel} value={fBroker} onChange={e=>setFBroker(e.target.value)}>
          {['any','alpaca','coinbase','kraken','ibkr'].map(v=><option key={v}>{v}</option>)}
        </select>
        <button style={S.sbtnG} onClick={runSearch}>Search</button>
        <button style={S.sbtnD} onClick={refresh}>⟳</button>
      </div>

      {/* Main split */}
      <div style={S.main}>
        {/* Sidebar */}
        <div style={S.sidebar}>
          <div style={{padding:'6px 8px',fontSize:10,fontWeight:700,color:'var(--text-2)',textTransform:'uppercase',borderBottom:'1px solid var(--border)'}}>Tables</div>
          {tables.map(t => (
            <div key={t.name} style={S.tblItem(curTable===t.name && !curView)} onClick={()=>browseTable(t.name)}>
              <span style={{fontSize:11,color:'var(--text-1)'}}>{t.name}</span>
              <span style={{fontSize:10,color:'var(--text-2)'}}>{t.rows.toLocaleString()}</span>
            </div>
          ))}
        </div>

        {/* Grid */}
        <div style={S.grid}>
          <div style={S.gtitle}>
            <span style={{color:'#f0c040',fontSize:12,fontWeight:700}}>{gridTitle}</span>
            <span style={{color:'var(--text-2)',fontSize:11}}>{rows.length.toLocaleString()} rows</span>
            {rows.length > 0 && (
              <button style={{...S.sbtnG,marginLeft:'auto',padding:'2px 8px',fontSize:10}} onClick={() => {
                const filename = `db-${curTable||'export'}-${new Date().toISOString().slice(0,19).replace(/[T:]/g,'-')}.csv`;
                window.exportCsv?.(filename, cols.map((c, i) => ({ label: c, value: (r) => r[i] })), rows);
              }}>⬇ CSV</button>
            )}
          </div>
          <div style={S.tcon}>
            <table style={{width:'100%',borderCollapse:'collapse'}}>
              <thead>
                <tr>
                  {cols.map((c,i) => (
                    <th key={c} style={S.th(sortCol===i)} onClick={()=>sortBy(i)}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 && cols.length > 0 && (
                  <tr><td colSpan={cols.length} style={{padding:'20px',textAlign:'center',color:'var(--text-2)',fontSize:12}}>No results — try different filters</td></tr>
                )}
                {rows.map((row, ri) => {
                  const isSel = ri === selIdx;
                  return (
                    <tr key={ri}
                      style={{background: isSel ? '#1e3a5f' : ri%2===0 ? 'var(--bg-1)' : 'var(--bg-2)', cursor:'pointer', outline: isSel ? '1px solid #2563eb' : 'none'}}
                      onClick={()=>selectRow(ri)}
                      onDoubleClick={openEdit}>
                      {row.map((v,ci) => {
                        const col = cols[ci]||'';
                        const s = v===null?'':String(v);
                        let color = 'var(--text-1)';
                        if ((col==='pnl'||col==='daily_pnl'||col==='gain_loss') && s) color = parseFloat(s)>=0?'#4ade80':'#f87171';
                        if (col==='enabled') color = s==='TRUE'?'#4ade80':'#f87171';
                        return <td key={ci} style={{...S.td,color}} title={s}>{s}</td>;
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Action bar */}
      <div style={S.abar}>
        <span style={S.lbl}>Actions:</span>
        <button style={S.abtnE} onClick={openEdit}>✏ Edit Row</button>
        {isTrades  && <button style={S.abtnF}   onClick={flushGhost}>☠ Flush Ghost</button>}
        {isStrat   && <button style={S.abtnEn}  onClick={()=>toggleStrategy(true)}>✔ Enable</button>}
        {isStrat   && <button style={S.abtnDis} onClick={()=>toggleStrategy(false)}>✖ Disable</button>}
        <span style={{...S.lbl,marginLeft:6}}>{selIdx===null?'← select a row first':`row ${selIdx+1} selected`}</span>
      </div>

      {/* Status bar */}
      <div style={S.stsbar(status.type)}>{status.msg}</div>

      {/* Edit modal */}
      {editOpen && (
        <div style={{position:'absolute',inset:0,background:'rgba(0,0,0,.7)',display:'flex',alignItems:'center',justifyContent:'center',zIndex:999}} onClick={e=>{if(e.target===e.currentTarget)setEditOpen(false)}}>
          <div style={{background:'var(--bg-2)',border:'1px solid var(--border)',borderRadius:8,width:600,maxHeight:'80%',display:'flex',flexDirection:'column'}}>
            <div style={{padding:'10px 14px',borderBottom:'1px solid var(--border)',display:'flex',justifyContent:'space-between',alignItems:'center'}}>
              <span style={{fontWeight:700,fontSize:13}}>Edit — {editMeta.table}  rowid={editMeta.rowid}</span>
              <button style={{background:'none',border:'none',color:'var(--text-2)',cursor:'pointer',fontSize:16}} onClick={()=>setEditOpen(false)}>✕</button>
            </div>
            <div style={{overflowY:'auto',padding:'10px 14px',flex:1}}>
              {Object.entries(editFields).map(([col, val]) => (
                <div key={col} style={{display:'flex',alignItems:'center',marginBottom:6,gap:8}}>
                  <label style={{width:180,textAlign:'right',fontSize:11,color:'var(--text-2)',flexShrink:0}}>{col}</label>
                  <input value={val===null?'':String(val)} onChange={e=>setEditFields(p=>({...p,[col]:e.target.value}))}
                    style={{flex:1,background:'var(--bg-1)',border:'1px solid var(--border)',color:'var(--text-1)',padding:'4px 8px',borderRadius:4,fontSize:11,fontFamily:'inherit'}} />
                </div>
              ))}
            </div>
            <div style={{padding:'8px 14px',borderTop:'1px solid var(--border)',display:'flex',gap:8}}>
              <button style={{background:'#16a34a',border:'none',color:'#fff',padding:'6px 16px',borderRadius:4,cursor:'pointer',fontWeight:700}} onClick={saveEdit}>Save</button>
              <button style={{background:'var(--bg-3)',border:'none',color:'var(--text-1)',padding:'6px 14px',borderRadius:4,cursor:'pointer'}} onClick={()=>setEditOpen(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

window.SidePanels = { ManagePositionsPanel, InjectSymbolsPanel, QuickControlsPanel, TerminalPanel, DBBrowserPanel };
