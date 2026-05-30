import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)

from data.database import db; from datetime import date; db.update_session(date.today().isoformat(), trading_halted=0, halt_reason=None, consecutive_losses=0); print('Halt cleared')
