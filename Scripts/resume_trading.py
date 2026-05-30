
import sys
import os

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)


#Flush a bad/stuck trade from the database.
#Usage: python flush_trade.py

from data.database import db
import sqlite3
import config
import datetime
#session_date= datetime.now().isoformat()
db.update_session("2026/05/25",
        trading_halted     = 0,
        halt_reason        = None,
        consecutive_losses = 0
    )



