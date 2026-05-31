// Main App — assembles top bar, taskbar, dock, and all windows
const { useState: $S, useEffect: $E, useRef: $R, useMemo: $M } = React;

const { TopOverview, OpenPositionsPanel, ManualTradePanel, TradeLogPanel, DailyPerfPanel, BacktesterPanel, OptimizerPanel, ChatPanel, ChartPanel, BrokerPanel } = window.Panels;
const { ManagePositionsPanel, InjectSymbolsPanel, QuickControlsPanel, TerminalPanel, DBBrowserPanel } = window.SidePanels;

// ── Theme system ──────────────────────────────────────────────────────────────
const THEME_KEY = 'tbot.theme.v1';
const DEFAULT_THEME = {
  '--bg-0':             '#0b0d10',
  '--bg-1':             '#111418',
  '--bg-2':             '#161a1f',
  '--bg-3':             '#1c2128',
  '--bg-4':             '#252b34',
  '--border':           '#232830',
  '--border-strong':    '#2d343f',
  '--text-0':           '#f3f5f8',
  '--text-1':           '#b8bfca',
  '--text-2':           '#7d8593',
  '--accent':           '#14b8a6',
  '--accent-bright':    '#2dd4bf',
  '--bevel-hi':         'rgba(255,255,255,0.10)',
  '--bevel-sh':         'rgba(0,0,0,0.62)',
  '--panel-tilt':        '2',
  '--panel-thickness':   '5',
  '--panel-perspective': '1400',
  '--panel-edge-color':  '#1a2535',
};
const THEME_LABELS = {
  '--bg-0':          'Workspace BG',
  '--bg-1':          'Window BG',
  '--bg-2':          'Panel BG',
  '--bg-3':          'Titlebar',
  '--bg-4':          'Hover / Active',
  '--border':        'Border',
  '--border-strong': 'Border Strong',
  '--text-0':        'Text Primary',
  '--text-1':        'Text Secondary',
  '--text-2':        'Text Muted',
  '--accent':            'Accent',
  '--accent-bright':     'Accent Bright',
  '--panel-edge-color':  'Edge Color',
};
// Slider controls — min/max/step instead of color picker
const THEME_SLIDERS = {
  '--panel-tilt':        { label: 'Panel Tilt',        min: 0,   max: 20,   step: 1   },
  '--panel-thickness':   { label: 'Edge Thickness',    min: 0,   max: 20,   step: 1   },
  '--panel-perspective': { label: 'Perspective Depth', min: 300, max: 2000, step: 50, note: '↓ lower = more dramatic' },
};

function _applyTheme(obj) {
  Object.entries(obj).forEach(([k, v]) => {
    document.documentElement.style.setProperty(k, v);
  });
}
async function _loadSavedTheme() {
  try {
    const r = await fetch('/api/theme');
    const d = await r.json();
    return (d.theme && Object.keys(d.theme).length) ? d.theme : null;
  } catch { return null; }
}
async function _saveTheme(obj) {
  try {
    await fetch('/api/theme', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: obj }),
    });
  } catch {}
}

// ColorRow — isolated component so each color input manages its own ref.
// Using key={value} on the color input caused React to unmount it on every
// drag step inside the native picker, closing it immediately.
// Instead: uncontrolled input + ref that only syncs when the picker is NOT active.
function ColorRow({ colorKey, label, value, apply }) {
  const inputRef  = React.useRef(null);
  const activeRef = React.useRef(false);  // true while native color picker is open

  React.useEffect(() => {
    // Sync swatch when value changes externally (e.g. text box typed a hex)
    // but skip if the picker is currently open — that would kill the drag.
    if (inputRef.current && !activeRef.current) {
      inputRef.current.value = value || '#000000';
    }
  }, [value]);

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
      <span style={{ color: 'var(--text-1)', fontSize: '12px', flex: 1, minWidth: 0 }}>{label}</span>
      <div style={{ display: 'flex', alignItems: 'center', gap: '7px', flexShrink: 0 }}>
        <div style={{
          width: '28px', height: '22px', borderRadius: '4px',
          background: value,
          border: '1px solid var(--border-strong)',
          boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.06)',
          position: 'relative', overflow: 'hidden', cursor: 'pointer',
        }}>
          <input type="color"
            ref={inputRef}
            defaultValue={value || '#000000'}
            onFocus={() => { activeRef.current = true; }}
            onBlur={()  => { activeRef.current = false; }}
            onChange={e => apply(colorKey, e.target.value)}
            style={{
              position: 'absolute', inset: '-4px', width: 'calc(100% + 8px)', height: 'calc(100% + 8px)',
              opacity: 0, cursor: 'pointer',
            }}
          />
        </div>
        <input
          type="text"
          value={value || ''}
          onChange={e => apply(colorKey, e.target.value)}
          style={{
            width: '72px', background: 'var(--bg-0)', border: '1px solid var(--border)',
            borderRadius: '4px', color: 'var(--text-2)', fontSize: '10px',
            fontFamily: 'var(--mono)', padding: '3px 5px', outline: 'none',
          }}
        />
      </div>
    </div>
  );
}

function ThemeManager({ children }) {
  React.useEffect(() => {
    _loadSavedTheme().then(saved => { if (saved) _applyTheme(saved); });
  }, []);
  return children;
}

function ThemeConfigPanel() {
  const [theme, setTheme] = $S({ ...DEFAULT_THEME });

  // Load from server on mount
  $E(() => {
    _loadSavedTheme().then(saved => {
      if (saved) setTheme({ ...DEFAULT_THEME, ...saved });
    });
  }, []);

  const apply = (key, val) => {
    const next = { ...theme, [key]: val };
    setTheme(next);
    document.documentElement.style.setProperty(key, val);
    _saveTheme(next);
  };

  const reset = () => {
    setTheme({ ...DEFAULT_THEME });
    _applyTheme(DEFAULT_THEME);
    _saveTheme(DEFAULT_THEME);
  };

  return (
    <div style={{ padding: '14px 16px', overflowY: 'auto', height: '100%', display: 'flex', flexDirection: 'column', gap: '0' }}>
      <div style={{ color: 'var(--text-2)', fontSize: '11px', marginBottom: '14px', lineHeight: 1.55 }}>
        Changes apply instantly and persist across sessions.
      </div>

      {/* ── 3D Effects sliders ── */}
      <div style={{ marginBottom: '14px', padding: '10px 12px', borderRadius: '7px',
        background: 'var(--bg-2)', border: '1px solid var(--border)',
        borderTop: '1px solid var(--bevel-hi)',
        boxShadow: 'inset 0 1px 0 var(--bevel-inner-hi)' }}>
        <div style={{ fontSize: '10px', fontWeight: 600, color: 'var(--accent-bright)',
          textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: '10px' }}>
          3D Effects
        </div>
        {Object.entries(THEME_SLIDERS).map(([key, cfg]) => (
          <div key={key} style={{ marginBottom: '10px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '3px' }}>
              <span style={{ color: 'var(--text-1)', fontSize: '12px' }}>{cfg.label}</span>
              <span style={{ color: 'var(--accent-bright)', fontSize: '11px', fontFamily: 'var(--mono)', fontWeight: 600 }}>
                {theme[key] ?? cfg.min}
              </span>
            </div>
            {cfg.note && <div style={{ fontSize: '9px', color: 'var(--text-3)', marginBottom: '4px' }}>{cfg.note}</div>}
            <input type="range" min={cfg.min} max={cfg.max} step={cfg.step}
              value={Number(theme[key] ?? cfg.min)}
              onChange={e => apply(key, e.target.value)}
              className="win-opacity-slider"
              style={{ width: '100%' }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between',
              fontSize: '9px', color: 'var(--text-3)', marginTop: '2px' }}>
              <span>{cfg.min}</span><span>{cfg.max}</span>
            </div>
          </div>
        ))}
      </div>

      {/* ── Color pickers ── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', flex: 1 }}>
        {Object.entries(THEME_LABELS).map(([key, label]) => (
          <ColorRow key={key} colorKey={key} label={label} value={theme[key]} apply={apply} />
        ))}
      </div>

      <div style={{ marginTop: '16px', display: 'flex', gap: '8px' }}>
        <button onClick={reset} style={{
          flex: 1, padding: '8px', borderRadius: '6px',
          background: 'rgba(244,63,94,0.10)', border: '1px solid rgba(244,63,94,0.28)',
          borderTop: '1px solid rgba(244,63,94,0.4)', color: '#f43f5e',
          cursor: 'pointer', fontSize: '12px', fontWeight: 500,
          boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.04)',
        }}>Reset to Defaults</button>
      </div>

      <div style={{ marginTop: '14px', padding: '10px', borderRadius: '6px',
        background: 'var(--bg-2)', border: '1px solid var(--border)',
        borderTop: '1px solid var(--bevel-hi)',
        boxShadow: 'inset 0 1px 0 var(--bevel-inner-hi)',
        fontSize: '10.5px', color: 'var(--text-2)', lineHeight: 1.6 }}>
        <strong style={{ color: 'var(--text-1)' }}>Tip:</strong> Use the <span style={{ color: 'var(--accent-bright)' }}>◧</span> button in any panel's title bar to set that panel's content opacity — useful for seeing through to charts beneath.
      </div>
    </div>
  );
}

// Window definitions
const WINDOW_DEFS = [
  { id: 'overview',         title: 'Overview · Capital & KPIs',  icon: 'activity',  defaultPos: { x: 20, y: 10 },    defaultSize: { w: 1180, h: 360 }, openByDefault: true },
  { id: 'open-positions',   title: 'Open Positions',              icon: 'pin',       defaultPos: { x: 20, y: 380 },   defaultSize: { w: 1180, h: 320 }, openByDefault: true },
  { id: 'manual-trade',     title: 'Manual Trade Entry',          icon: 'edit',      defaultPos: { x: 1210, y: 10 },  defaultSize: { w: 520, h: 420 },  openByDefault: true },
  { id: 'trade-log',        title: "Today's Trade Log",           icon: 'log',       defaultPos: { x: 1210, y: 440 }, defaultSize: { w: 560, h: 360 },  openByDefault: true },
  { id: 'daily-perf',       title: 'Recent Daily Performance',    icon: 'calendar',  defaultPos: { x: 20, y: 710 },   defaultSize: { w: 820, h: 290 },  openByDefault: true },
  { id: 'backtester',       title: 'Strategy Backtester',         icon: 'flask',     defaultPos: { x: 850, y: 810 },  defaultSize: { w: 680, h: 420 },  openByDefault: true },
  { id: 'optimizer',        title: 'Parameter Optimizer',         icon: 'zap',       defaultPos: { x: 870, y: 830 },  defaultSize: { w: 860, h: 600 },  openByDefault: false },
  { id: 'chat',             title: 'Chat with Bot',               icon: 'chat',      defaultPos: { x: 20, y: 1010 },  defaultSize: { w: 800, h: 300 },  openByDefault: true },
  { id: 'manage-positions', title: 'Manage Positions',            icon: 'sliders',   defaultPos: { x: 360, y: 80 },   defaultSize: { w: 760, h: 540 },  openByDefault: false },
  { id: 'inject-symbols',   title: 'Inject Symbols Into Watchlist',icon: 'telescope',defaultPos: { x: 420, y: 100 },  defaultSize: { w: 740, h: 540 },  openByDefault: false },
  { id: 'terminal',         title: 'PowerShell Workspace',         icon: 'terminal',  defaultPos: { x: 180, y: 140 },  defaultSize: { w: 520, h: 560 },  openByDefault: false },
  { id: 'quick-controls',   title: 'Quick Controls',              icon: 'settings',  defaultPos: { x: 100, y: 50 },   defaultSize: { w: 360, h: 720 },  openByDefault: false },
  { id: 'chart',            title: 'Chart',                       icon: 'activity',  defaultPos: { x: 300, y: 60 },   defaultSize: { w: 1000, h: 600 }, openByDefault: false },
  { id: 'db-browser',       title: 'Database Browser',            icon: 'log',       defaultPos: { x: 260, y: 80 },   defaultSize: { w: 1060, h: 660 }, openByDefault: false },
  { id: 'theme-config',     title: 'Theme & Color Config',        icon: 'settings',  defaultPos: { x: 460, y: 120 },  defaultSize: { w: 340, h: 560 },  openByDefault: false },
  { id: 'broker-panel',     title: 'Broker Account',              icon: 'activity',  defaultPos: { x: 500, y: 100 },  defaultSize: { w: 780, h: 520 },  openByDefault: false },
];

// =========== App Shell ===========
function AppShell() {
  return (
    <ThemeManager>
      <WindowProvider defs={WINDOW_DEFS}>
        <Inner />
      </WindowProvider>
    </ThemeManager>
  );
}

function Inner() {
  const [now, setNow] = $S(new Date());
  $E(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  const dateStr = now.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: '2-digit', year: 'numeric' });
  const timeStr = now.toLocaleTimeString('en-US', { hour12: false });

  return (
    <div className="app">
      <Topbar dateStr={dateStr} timeStr={timeStr} />
      <Workspace />
      <Taskbar />
    </div>
  );
}

function Topbar({ dateStr, timeStr }) {
  const { open } = useWindows();
  return (
    <div className="topbar">
      <div className="brand">
        <span className="brand-mark"></span>
        Autonomous Trading AI
      </div>
      <div className="menu">
        <button onClick={() => open('overview')}>Overview</button>
        <button onClick={() => open('open-positions')}>Positions</button>
        <button onClick={() => open('trade-log')}>Trade Log</button>
        <button onClick={() => open('backtester')}>Backtester</button>
        <button onClick={() => open('terminal')}>Shell</button>
        <button onClick={() => open('chart')}>Chart</button>
        <button onClick={() => open('chat')}>Chat</button>
        <button onClick={() => open('db-browser')}>Database</button>
        <button onClick={() => open('theme-config')}>Theme</button>
      </div>
      <div className="spacer"></div>
      <TradovateStatus />
      <div className="status-pill">
        <span className="dot"></span>
        Trading Active
      </div>
      <div className="clock">{dateStr} · {timeStr} PT</div>
    </div>
  );
}

function TradovateStatus() {
  const [info, setInfo] = $S({ connected: false, running: false, auth_failed: false, age: null });
  const [open, setOpen] = $S(false);
  const [token, setToken] = $S('');
  const [busy, setBusy] = $S(false);
  const [msg, setMsg] = $S('');
  const ref = $R(null);

  // Poll feed status every 30s
  $E(() => {
    const poll = async () => {
      try {
        const r = await fetch('/api/tradovate_debug', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
        const d = await r.json();
        let minAge = null;
        for (const k of Object.keys(d.cache || {})) {
          const lb = d.cache[k]?.last_bar;
          if (lb?.time) { const a = Math.floor(Date.now()/1000) - lb.time; if (minAge===null||a<minAge) minAge=a; }
        }
        setInfo({ connected: d.connected, running: d.running, auth_failed: d.auth_failed, age: minAge });
      } catch(_) {}
    };
    poll();
    const t = setInterval(poll, 30000);
    return () => clearInterval(t);
  }, []);

  // Close modal when clicking outside (check both button ref AND portalled dropdown ref)
  const dropRef = $R(null);
  $E(() => {
    if (!open) return;
    const h = (e) => {
      const inButton = ref.current && ref.current.contains(e.target);
      const inDrop   = dropRef.current && dropRef.current.contains(e.target);
      if (!inButton && !inDrop) setOpen(false);
    };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, [open]);

  const inject = async () => {
    setBusy(true); setMsg('');
    try {
      const r = await fetch('/api/tradovate_token', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ token: token.trim() }) });
      const d = await r.json();
      if (d.ok) {
        setMsg('✓ Feed connecting…');
        setToken('');
        setTimeout(() => { setOpen(false); setMsg(''); }, 2000);
        setTimeout(async () => {
          try {
            const r2 = await fetch('/api/tradovate_debug', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
            const d2 = await r2.json();
            setInfo({ connected: d2.connected, running: d2.running, auth_failed: d2.auth_failed, age: null });
          } catch(_) {}
        }, 4000);
      } else { setMsg('✗ ' + (d.error || 'Failed')); }
    } catch(e) { setMsg('✗ ' + e.message); }
    setBusy(false);
  };

  const live = info.connected && info.running;
  const color = live ? '#22c55e' : info.auth_failed ? '#ef4444' : '#f59e0b';
  const label = live ? 'RT LIVE' : info.auth_failed ? 'AUTH ERR' : 'FEED OFF';
  const ageStr = info.age !== null ? (info.age < 60 ? `${info.age}s` : `${Math.floor(info.age/60)}m`) + ' ago' : '';

  const btnRect = open && ref.current ? ref.current.getBoundingClientRect() : null;
  const dropTop   = btnRect ? btnRect.bottom + 8 : 60;
  const dropRight = btnRect ? window.innerWidth - btnRect.right : 10;

  const dropdown = open ? ReactDOM.createPortal(
    <div ref={dropRef} style={{
      position:'fixed', top: dropTop, right: dropRight,
      background:'#1e293b', border:'1px solid #334155', borderRadius:'10px',
      padding:'16px', width:'370px', zIndex:2147483647,
      boxShadow:'0 20px 60px rgba(0,0,0,0.7)',
    }}>
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:'10px' }}>
        <span style={{ color:'#f1f5f9', fontSize:'0.85rem', fontWeight:600 }}>Inject Tradovate Token</span>
        <span style={{ color: live?'#22c55e':'#f59e0b', fontSize:'0.75rem' }}>{label}{live && ageStr ? ` · ${ageStr}` : ''}</span>
      </div>
      {info.auth_failed && (
        <div style={{ background:'rgba(239,68,68,0.12)', border:'1px solid #ef444460', borderRadius:'6px',
          color:'#fca5a5', fontSize:'0.76rem', padding:'8px 10px', marginBottom:'10px' }}>
          ⚠ Token expired — paste a fresh one below to reconnect.
        </div>
      )}
      <div style={{ color:'#64748b', fontSize:'0.73rem', marginBottom:'10px', lineHeight:1.55 }}>
        DevTools → Network → any PATCH request → copy the full <strong style={{color:'#94a3b8'}}>Authorization</strong> header value
      </div>
      <textarea value={token} onChange={e => setToken(e.target.value)}
        placeholder="Bearer Ron Hensley eyJ…  (or just the eyJ… part)"
        rows={4} style={{
          width:'100%', boxSizing:'border-box',
          background:'#0f172a', border:'1px solid #334155', borderRadius:'6px',
          color:'#f1f5f9', fontSize:'0.75rem', padding:'8px',
          resize:'vertical', fontFamily:'monospace', outline:'none',
        }} />
      {msg && <div style={{ marginTop:'8px', fontSize:'0.8rem', color: msg.startsWith('✓')?'#22c55e':'#ef4444' }}>{msg}</div>}
      <div style={{ display:'flex', gap:'8px', marginTop:'10px' }}>
        <button onClick={inject} disabled={busy || !token.trim()} style={{
          flex:1, background:'#6366f1', border:'none', borderRadius:'6px',
          color:'#fff', cursor:'pointer', fontSize:'0.82rem', fontWeight:600,
          padding:'9px', opacity:(busy||!token.trim())?0.5:1, transition:'opacity 0.15s',
        }}>{busy ? 'Connecting…' : 'Inject & Connect'}</button>
        <button onClick={() => { setOpen(false); setMsg(''); setToken(''); }} style={{
          background:'#334155', border:'none', borderRadius:'6px',
          color:'#94a3b8', cursor:'pointer', fontSize:'0.82rem', padding:'9px 14px',
        }}>Cancel</button>
      </div>
    </div>,
    document.body
  ) : null;

  return (
    <div ref={ref} style={{ position:'relative', marginRight:'8px' }}>
      <button onClick={() => setOpen(v => !v)} title={live && ageStr ? `Last bar: ${ageStr}` : 'Click to inject Tradovate token'} style={{
        display:'flex', alignItems:'center', gap:'6px',
        background: live ? 'rgba(34,197,94,0.1)' : info.auth_failed ? 'rgba(239,68,68,0.1)' : 'rgba(245,158,11,0.1)',
        border:`1px solid ${color}40`, borderRadius:'999px',
        color, cursor:'pointer', padding:'4px 12px',
        fontSize:'11.5px', fontWeight:600, letterSpacing:'0.05em',
        transition:'all 0.2s',
      }}>
        <span style={{
          width:'7px', height:'7px', borderRadius:'50%', background:color, flexShrink:0,
          boxShadow: live ? `0 0 7px ${color}` : 'none',
          animation: live ? 'pulse 2s ease-in-out infinite' : 'none',
        }}/>
        Tradovate {label}
        {live && ageStr && <span style={{ opacity:0.65, fontSize:'10px' }}>{ageStr}</span>}
      </button>
      {dropdown}
    </div>
  );
}

function Workspace() {
  const { canvasSize } = useWindows();
  const wsRef = $R(null);
  const panRef = $R(null);

  // Shift+wheel → horizontal scroll; plain wheel → vertical scroll (panels handle their own)
  $E(() => {
    const el = wsRef.current;
    if (!el) return;
    const onWheel = (e) => {
      if (e.shiftKey) {
        e.preventDefault();
        el.scrollLeft += e.deltaY || e.deltaX;
      }
      // plain wheel: let workspace scroll vertically (default), panels stop propagation themselves
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  // Middle-mouse-button or alt+drag on workspace background → pan canvas
  $E(() => {
    const el = wsRef.current;
    if (!el) return;
    const onPointerDown = (e) => {
      // Only pan on workspace background (not on windows) with middle button or alt+left
      const isBackground = e.target === el || e.target.classList.contains('workspace-canvas') || e.target.classList.contains('dock');
      if (!isBackground) return;
      if (e.button !== 1 && !(e.button === 0 && e.altKey)) return;
      e.preventDefault();
      panRef.current = { x: e.clientX, y: e.clientY, sl: el.scrollLeft, st: el.scrollTop };
      el.style.cursor = 'grabbing';
    };
    const onPointerMove = (e) => {
      const d = panRef.current;
      if (!d) return;
      el.scrollLeft = d.sl - (e.clientX - d.x);
      el.scrollTop = d.st - (e.clientY - d.y);
    };
    const onPointerUp = () => {
      panRef.current = null;
      el.style.cursor = '';
    };
    el.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    return () => {
      el.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
    };
  }, []);

  return (
    <div
      ref={wsRef}
      className="workspace"
      style={{
        '--workspace-w': `${Math.ceil(canvasSize?.width || window.innerWidth)}px`,
        '--workspace-h': `${Math.ceil(canvasSize?.height || window.innerHeight)}px`,
      }}
    >
      <div className="workspace-canvas" />
      <Dock />
      <Window id="overview"><TopOverview live={{ capital: 99203.01, dailyPnl: -331.74, dailyPnlP: -0.33 }} /></Window>
      <Window id="open-positions"><OpenPositionsPanel /></Window>
      <Window id="manual-trade"><ManualTradePanel /></Window>
      <Window id="trade-log"><TradeLogPanel /></Window>
      <Window id="daily-perf"><DailyPerfPanel /></Window>
      <Window id="backtester"><BacktesterPanel /></Window>
      <Window id="optimizer"><OptimizerPanel /></Window>
      <Window id="chat"><ChatPanel /></Window>
      <Window id="manage-positions"><ManagePositionsPanel /></Window>
      <Window id="inject-symbols"><InjectSymbolsPanel /></Window>
      <Window id="terminal"><TerminalPanel /></Window>
      <Window id="quick-controls"><QuickControlsPanel /></Window>
      <Window id="chart"><ChartPanel /></Window>
      <Window id="db-browser"><DBBrowserPanel /></Window>
      <Window id="theme-config"><ThemeConfigPanel /></Window>
      <Window id="broker-panel"><BrokerPanel /></Window>
    </div>
  );
}

function Dock() {
  const { windows, open, focus } = useWindows();
  const resetLayout = () => {
    if (prompt('Type RESET to confirm wiping your window layout:') === 'RESET') {
      localStorage.removeItem('tbot.windows.v2');
      location.reload();
    }
  };
  const items = [
    { id: 'overview', label: 'Overview', icon: 'activity' },
    { id: 'open-positions', label: 'Positions', icon: 'pin' },
    { id: 'manage-positions', label: 'Manage Positions', icon: 'sliders' },
    { id: 'manual-trade', label: 'Manual Trade', icon: 'edit' },
    { id: 'inject-symbols', label: 'Inject Symbols', icon: 'telescope' },
    { id: 'terminal', label: 'PowerShell', icon: 'terminal' },
    { id: 'trade-log', label: 'Trade Log', icon: 'log' },
    { id: 'daily-perf', label: 'Daily Performance', icon: 'calendar' },
    { id: 'backtester', label: 'Backtester', icon: 'flask' },
    { id: 'optimizer',  label: 'Optimizer',  icon: 'zap' },
    { id: 'chat', label: 'Chat', icon: 'chat' },
    { id: 'chart',          label: 'Chart',           icon: 'activity' },
    { id: 'quick-controls', label: 'Quick Controls', icon: 'settings' },
    { id: 'db-browser',     label: 'Database Browser', icon: 'log' },
    { id: 'broker-panel',   label: 'Broker Account',   icon: 'activity' },
  ];
  return (
    <div className="dock">
      {items.map(it => {
        const w = windows[it.id];
        const active = w && w.open && !w.minimized;
        return (
          <button key={it.id} className={cls('dock-btn', active && 'active')} onClick={() => active ? focus(it.id) : open(it.id)} title={it.label}>
            <Icon name={it.icon} size={18} />
            <span className="tip">{it.label}</span>
          </button>
        );
      })}
      <button className="dock-btn" onClick={resetLayout} title="Reset Layout" style={{ marginLeft: 'auto', opacity: 0.5 }}>
        <Icon name="refresh" size={18} />
        <span className="tip">Reset Layout</span>
      </button>
    </div>
  );
}

function Taskbar() {
  const { windows, focus, toggleMin, open, focusedId } = useWindows();
  const wins = Object.values(windows).filter(w => w.open);
  const cpu = $M(() => 12 + Math.floor(Math.random() * 8), []);
  const resetLayout = () => {
    if (prompt('Type RESET to confirm wiping your window layout:') === 'RESET') {
      localStorage.removeItem('tbot.windows.v2');
      location.reload();
    }
  };
  return (
    <div className="taskbar">
      <button className="start-btn" onClick={() => open('quick-controls')}>
        <Icon name="grid" size={12} />
        Apps
      </button>
      <div className="divider"></div>
      {wins.map(w => {
        const isActive = !w.minimized && focusedId === w.id;
        return (
          <div key={w.id} className={cls('task', !w.minimized && 'active', w.minimized && 'minimized')} onClick={() => isActive ? toggleMin(w.id) : focus(w.id)}>
            <Icon name={w.icon} size={11} />
            <span className="label">{w.title}</span>
          </div>
        );
      })}
      <div className="tray">
        <button className="btn ghost sm" onClick={resetLayout} title="Reset all windows to default positions"><Icon name="refresh" size={11} />Reset Layout</button>
        <div className="item"><span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--green)', boxShadow: '0 0 6px var(--green)' }}></span> Bot · PID 11552</div>
        <div className="item"><Icon name="cpu" size={11} /> CPU {cpu}%</div>
        <div className="item"><Icon name="layers" size={11} /> ML 30%</div>
        <div className="item">$99,203.01</div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<AppShell />);
