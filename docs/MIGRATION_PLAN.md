# Streamlit → HTML Migration Plan

## Stage 1 (done in this commit)
1. Introduce a lightweight Python web server (`web_dashboard.py`) that serves `Html-Files/` and exposes JSON APIs.
2. Keep `dashboard.py` in place while we validate data and control flow.
3. Define the contract for migrated panels:
   - `GET /api/snapshot`
   - `POST /api/control`

## Stage 2 (done in this commit, initial panels)
Migrated panels:
- Bot status
- Open positions count
- Available cash
- Control buttons: pause trading + scan refresh request

Validation checklist:
- Snapshot values match `dashboard.py` and DB values.
- Button inputs reach Python backend and produce expected state changes/messages.

## Stage 3 (next)
1. Port remaining Streamlit modules panel-by-panel:
   - Positions table + close actions
   - P&L charts
   - Strategy config toggles
   - Chat/assistant controls
2. Replace Streamlit-specific workflows with JSON endpoints.
3. Regression test all controls and database writes.
4. Decommission `dashboard.py` entrypoint once feature parity is complete.
