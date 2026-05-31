// Window manager — full OS-style draggable, resizable, minimizable, maximizable windows
const { useState, useEffect, useRef, useCallback, useMemo, createContext, useContext } = React;

const STORAGE_KEY = 'tbot.windows.v2';

const WindowContext = createContext(null);
const useWindows = () => useContext(WindowContext);

function loadState() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
  catch { return {}; }
}
function saveState(s) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch {}
}

function WindowProvider({ children, defs }) {
  // defs: { id, title, icon, defaultPos, defaultSize, content, openByDefault }
  const persisted = useMemo(() => loadState(), []);

  const [windows, setWindows] = useState(() => {
    const obj = {};
    defs.forEach(d => {
      const p = persisted[d.id] || {};
      obj[d.id] = {
        id: d.id,
        title: d.title,
        icon: d.icon,
        x: p.x ?? d.defaultPos.x,
        y: p.y ?? d.defaultPos.y,
        w: p.w ?? d.defaultSize.w,
        h: p.h ?? d.defaultSize.h,
        open: p.open ?? !!d.openByDefault,
        minimized: p.minimized ?? false,
        maximized: p.maximized ?? false,
        z: p.z ?? 1,
        opacity: p.opacity ?? 1,
      };
    });
    return obj;
  });
  const [focusedId, setFocusedId] = useState(null);
  const zCounter = useRef(
    Math.max(100, ...Object.values(persisted).map(w => Number(w?.z) || 0))
  );

  // Persist
  useEffect(() => {
    const toSave = {};
    Object.entries(windows).forEach(([id, w]) => {
      toSave[id] = { x: w.x, y: w.y, w: w.w, h: w.h, open: w.open, minimized: w.minimized, maximized: w.maximized, z: w.z, opacity: w.opacity };
    });
    saveState(toSave);
  }, [windows]);

  const focus = useCallback((id) => {
    zCounter.current += 1;
    const z = zCounter.current;
    setWindows(prev => ({ ...prev, [id]: { ...prev[id], z, minimized: false, open: true } }));
    setFocusedId(id);
  }, []);

  const open = useCallback((id) => {
    zCounter.current += 1;
    const z = zCounter.current;
    setWindows(prev => ({ ...prev, [id]: { ...prev[id], open: true, minimized: false, z } }));
    setFocusedId(id);
  }, []);

  const close = useCallback((id) => {
    setWindows(prev => ({ ...prev, [id]: { ...prev[id], open: false, minimized: false } }));
    setFocusedId(prev => prev === id ? null : prev);
  }, []);

  const toggleMin = useCallback((id) => {
    setWindows(prev => ({ ...prev, [id]: { ...prev[id], minimized: !prev[id].minimized } }));
  }, []);

  const toggleMax = useCallback((id) => {
    zCounter.current += 1;
    const z = zCounter.current;
    setWindows(prev => {
      const wasMaximized = prev[id]?.maximized;
      if (!wasMaximized) {
        // About to maximize — snap workspace scroll to top-left so the window is fully visible
        requestAnimationFrame(() => {
          const workspace = document.querySelector('.workspace');
          if (workspace) { workspace.scrollLeft = 0; workspace.scrollTop = 0; }
        });
      }
      return { ...prev, [id]: { ...prev[id], maximized: !wasMaximized, minimized: false, z } };
    });
    setFocusedId(id);
  }, []);

  const updateGeometry = useCallback((id, geom) => {
    setWindows(prev => ({ ...prev, [id]: { ...prev[id], ...geom } }));
  }, []);

  const setOpacity = useCallback((id, opacity) => {
    setWindows(prev => ({ ...prev, [id]: { ...prev[id], opacity: Math.min(1, Math.max(0.08, opacity)) } }));
  }, []);

  const canvasSize = useMemo(() => {
    const margin = 220;
    const openWindows = Object.values(windows).filter(w => w.open && !w.minimized && !w.maximized);
    const maxRight = Math.max(window.innerWidth, ...openWindows.map(w => Number(w.x || 0) + Number(w.w || 0) + margin));
    const maxBottom = Math.max(window.innerHeight, ...openWindows.map(w => Number(w.y || 0) + Number(w.h || 0) + margin));
    return { width: maxRight, height: maxBottom };
  }, [windows]);

  const ctx = { windows, focus, open, close, toggleMin, toggleMax, updateGeometry, setOpacity, focusedId, canvasSize };

  return (
    <WindowContext.Provider value={ctx}>{children}</WindowContext.Provider>
  );
}

function Window({ id, children }) {
  const { windows, focus, close, toggleMin, toggleMax, updateGeometry, setOpacity, focusedId } = useWindows();
  const w = windows[id];
  const ref = useRef(null);
  const drag = useRef(null);
  const wRef = useRef(w);
  const [showOpacity, setShowOpacity] = useState(false);
  const opacityRef = useRef(null);
  useEffect(() => { wRef.current = w; }, [w]);

  // Close opacity popup on outside click
  useEffect(() => {
    if (!showOpacity) return;
    const h = (e) => {
      if (opacityRef.current && !opacityRef.current.contains(e.target)) setShowOpacity(false);
    };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, [showOpacity]);

  useEffect(() => {
    const onMove = (e) => {
      const d = drag.current; if (!d) return;
      const cur = wRef.current; if (!cur) return;
      const workspace = document.querySelector('.workspace');
      if (workspace) {
        const rect = workspace.getBoundingClientRect();
        if (e.clientX > rect.right - 90) workspace.scrollLeft += 24;
        if (e.clientY > rect.bottom - 90) workspace.scrollTop += 24;
        if (e.clientX < rect.left + 50) workspace.scrollLeft -= 24;
        if (e.clientY < rect.top + 50) workspace.scrollTop -= 24;
      }
      const scrollDx = workspace ? workspace.scrollLeft - d.startScrollLeft : 0;
      const scrollDy = workspace ? workspace.scrollTop - d.startScrollTop : 0;
      const dx = e.clientX - d.startX + scrollDx;
      const dy = e.clientY - d.startY + scrollDy;
      if (d.type === 'move') {
        const newX = Math.max(-cur.w + 80, d.origX + dx);
        const newY = Math.max(0, d.origY + dy);
        updateGeometry(id, { x: newX, y: newY });
      } else if (d.type === 'resize') {
        let nx = d.origX, ny = d.origY, nw = d.origW, nh = d.origH;
        if (d.dir.includes('e')) nw = Math.max(320, d.origW + dx);
        if (d.dir.includes('s')) nh = Math.max(160, d.origH + dy);
        if (d.dir.includes('w')) {
          nw = Math.max(320, d.origW - dx);
          nx = d.origX + (d.origW - nw);
        }
        if (d.dir.includes('n')) {
          nh = Math.max(160, d.origH - dy);
          ny = Math.max(0, d.origY + (d.origH - nh));
        }
        updateGeometry(id, { x: nx, y: ny, w: nw, h: nh });
      }
    };
    const onUp = () => { drag.current = null; };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [id, updateGeometry]);

  if (!w || !w.open || w.minimized) return null;

  const onTitleMouseDown = (e) => {
    if (e.target.closest('.win-controls')) return;
    if (w.maximized) return;
    focus(id);
    const workspace = document.querySelector('.workspace');
    drag.current = {
      type: 'move',
      startX: e.clientX, startY: e.clientY,
      origX: w.x, origY: w.y,
      startScrollLeft: workspace?.scrollLeft || 0,
      startScrollTop: workspace?.scrollTop || 0,
    };
  };

  const onResizeStart = (e, dir) => {
    e.stopPropagation();
    focus(id);
    const workspace = document.querySelector('.workspace');
    drag.current = {
      type: 'resize', dir,
      startX: e.clientX, startY: e.clientY,
      origX: w.x, origY: w.y, origW: w.w, origH: w.h,
      startScrollLeft: workspace?.scrollLeft || 0,
      startScrollTop: workspace?.scrollTop || 0,
    };
  };

  const bgAlpha = `${Math.round((w.opacity ?? 1) * 100)}%`;
  const alpha = w.opacity ?? 1;

  // When opacity < 1, override the --bg-* CSS variables ON this window element.
  // Since custom properties cascade, any descendant using var(--bg-2) etc —
  // including inline styles — will resolve to the transparent version automatically.
  const bgOverrides = alpha < 1 ? {
    '--bg-0': `color-mix(in srgb, #0b0d10 ${bgAlpha}, transparent)`,
    '--bg-1': `color-mix(in srgb, #111418 ${bgAlpha}, transparent)`,
    '--bg-2': `color-mix(in srgb, #161a1f ${bgAlpha}, transparent)`,
    '--bg-3': `color-mix(in srgb, #1c2128 ${bgAlpha}, transparent)`,
    '--bg-4': `color-mix(in srgb, #252b34 ${bgAlpha}, transparent)`,
    '--border': `color-mix(in srgb, #232830 ${bgAlpha}, transparent)`,
    '--border-strong': `color-mix(in srgb, #2d343f ${bgAlpha}, transparent)`,
  } : {};

  const style = w.maximized
    ? { left: 0, top: 0, width: '100%', height: '100%', zIndex: w.z, '--win-bg-alpha': bgAlpha, ...bgOverrides }
    : { left: w.x, top: w.y, width: w.w, height: w.h, zIndex: w.z, '--win-bg-alpha': bgAlpha, ...bgOverrides };

  const focused = focusedId === id;

  return (
    <div ref={ref}
      className={`win ${focused ? 'focused' : ''} ${w.maximized ? 'maximized' : ''}`}
      style={style}
      onMouseDown={() => focus(id)}>
      <div className="win-titlebar" onMouseDown={onTitleMouseDown} onDoubleClick={() => toggleMax(id)}>
        <div className="win-title">
          <Icon name={w.icon} size={13} />
          <span className="label">{w.title}</span>
        </div>
        <div className="win-controls">
          {/* Opacity button */}
          <div ref={opacityRef} style={{ position: 'relative' }}>
            <button
              title={`Opacity: ${Math.round((w.opacity ?? 1) * 100)}%`}
              onMouseDown={e => e.stopPropagation()}
              onClick={(e) => { e.stopPropagation(); setShowOpacity(v => !v); }}
              style={{ opacity: 0.55, fontSize: '10px', letterSpacing: '-0.03em' }}
            >◧</button>
            {showOpacity && (
              <div onMouseDown={e => e.stopPropagation()} style={{
                position: 'absolute', right: 0, top: '32px',
                background: '#1c2128', border: '1px solid #2d343f',
                borderTop: '1px solid rgba(255,255,255,0.09)',
                borderLeft: '1px solid rgba(255,255,255,0.06)',
                borderRadius: '7px', padding: '10px 14px',
                zIndex: 99999, width: '160px',
                boxShadow: '0 12px 32px rgba(0,0,0,0.7), inset 0 1px 0 rgba(255,255,255,0.05)',
              }}>
                <div style={{ fontSize: '10px', color: '#7d8593', marginBottom: '8px', fontWeight: 500 }}>
                  Background · <span style={{ color: '#b8bfca' }}>{Math.round((w.opacity ?? 1) * 100)}%</span>
                </div>
                <input
                  type="range" min={8} max={100}
                  value={Math.round((w.opacity ?? 1) * 100)}
                  onChange={e => setOpacity(id, Number(e.target.value) / 100)}
                  className="win-opacity-slider"
                />
              </div>
            )}
          </div>
          <button onClick={() => toggleMin(id)} title="Minimize"><Icon name="minus" size={12} /></button>
          <button onClick={() => toggleMax(id)} title="Maximize"><Icon name="square" size={11} /></button>
          <button className="close" onClick={() => close(id)} title="Close"><Icon name="x" size={12} /></button>
        </div>
      </div>
      <div className="win-body" onWheel={e => e.stopPropagation()}>{children}</div>
      {!w.maximized && (
        <>
          <div className="win-resize-edge n" onMouseDown={(e) => onResizeStart(e, 'n')} />
          <div className="win-resize-edge s" onMouseDown={(e) => onResizeStart(e, 's')} />
          <div className="win-resize-edge e" onMouseDown={(e) => onResizeStart(e, 'e')} />
          <div className="win-resize-edge w" onMouseDown={(e) => onResizeStart(e, 'w')} />
          <div className="win-resize-edge ne" onMouseDown={(e) => onResizeStart(e, 'ne')} />
          <div className="win-resize-edge nw" onMouseDown={(e) => onResizeStart(e, 'nw')} />
          <div className="win-resize-edge se" onMouseDown={(e) => onResizeStart(e, 'se')} />
          <div className="win-resize-edge sw" onMouseDown={(e) => onResizeStart(e, 'sw')} />
        </>
      )}
    </div>
  );
}

window.WindowProvider = WindowProvider;
window.Window = Window;
window.useWindows = useWindows;
