const { useEffect, useState } = React;

function MetricCard({label, value}) {
  return <div className="card"><div className="muted">{label}</div><h2>{value}</h2></div>
}

function App() {
  const [snapshot, setSnapshot] = useState({ bot_status: "UNKNOWN", open_positions: 0, available_cash: 0 });
  const [positions, setPositions] = useState([]);
  const [msg, setMsg] = useState("Waiting for API...");

  const base = (window.DASHBOARD_API_BASE || "").replace(/\/$/, "");
  const api = (path) => `${base}${path}`;

  async function load() {
    try {
      const [snapRes, posRes] = await Promise.all([
        fetch(api('/api/snapshot')),
        fetch(api('/api/open_positions')),
      ]);
      if (!snapRes.ok) throw new Error(`snapshot failed: ${snapRes.status}`);
      setSnapshot(await snapRes.json());
      if (posRes.ok) {
        const p = await posRes.json();
        setPositions(Array.isArray(p.positions) ? p.positions : []);
      }
      setMsg("Connected");
    } catch (e) {
      setMsg("API unavailable (running static mode only)");
    }
  }

  async function postControl(action, extras = {}) {
    try {
      const res = await fetch(api('/api/control'), {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action, ...extras})
      });
      const data = await res.json();
      setMsg(data.message || data.error || 'done');
      await load();
    } catch {
      setMsg(`Control '${action}' failed (API offline in static mode)`);
    }
  }

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, []);

  return <div className="wrap">
    <h1>Trading Bot Dashboard (Stage 3 - WIP)</h1>
    <p className="muted">HTML migration: positions panel + close actions now moved from Streamlit.</p>
    <div className="grid">
      <MetricCard label="Bot Status" value={snapshot.bot_status} />
      <MetricCard label="Open Positions" value={snapshot.open_positions} />
      <MetricCard label="Available Cash" value={`$${Number(snapshot.available_cash || 0).toFixed(2)}`} />
      <MetricCard label="Bot Process" value={snapshot.bot_process_running ? 'RUNNING' : 'STOPPED'} />
    </div>

    <div className="card" style={{marginTop:'12px'}}>
      <h3>Controls (migrated)</h3>
      <button className="btn ok" onClick={() => postControl('refresh_scan')}>Run Market Scan</button>
      <button className="btn warn" onClick={() => postControl('pause_trading')}>Pause Trading</button>
      <button className="btn ok" onClick={() => postControl('resume_trading')}>Resume Trading</button>
      <button className="btn ok" onClick={() => postControl('start_bot')}>Start Bot</button>
      <button className="btn warn" onClick={() => postControl('stop_bot')}>Stop Bot</button>
      <button className="btn warn" onClick={() => postControl('restart_bot')}>Restart Bot</button>
      <button className="btn ok" onClick={() => postControl('open_db_panel')} style={{background:'#0f3460'}}>🗄 DB Panel</button>
      <div style={{marginTop:'8px'}}>{msg}</div>
    </div>

    <div className="card" style={{marginTop:'12px'}}>
      <h3>Open Positions (migrated)</h3>
      {positions.length === 0 ? <div className="muted">No open positions</div> : (
        <table style={{width:'100%', borderCollapse:'collapse'}}>
          <thead><tr><th align="left">ID</th><th align="left">Symbol</th><th align="left">Side</th><th align="right">Qty</th><th align="right">Entry</th><th></th></tr></thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.id || `${p.symbol}-${p.entry_time}`}>
                <td>{p.id ?? '-'}</td>
                <td>{p.symbol}</td>
                <td>{p.side || '-'}</td>
                <td align="right">{Number(p.qty || 0).toFixed(4)}</td>
                <td align="right">{Number(p.entry_price || 0).toFixed(2)}</td>
                <td align="right">
                  <button className="btn warn" onClick={() => postControl('close_position', { position_id: p.id })} disabled={!p.id}>Close</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  </div>
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);