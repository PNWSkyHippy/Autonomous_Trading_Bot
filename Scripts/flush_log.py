"""
flush_log.py
Clears bot.log without stopping the running server.
Keeps the last N lines so you don't lose recent context.
Run any time: python flush_log.py
"""
import os
import sys

# Add project root to Python path and change working directory to root.
# This allows scripts in Scripts/ to import from data/, core/, strategies/ etc.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)



LOG_FILE  = "logs/bot.log"
SCAN_LOG = "scanner.log"
KEEP_LINES = 50  # how many recent lines to keep after flush

if not os.path.exists(LOG_FILE):
    print(f"Log file not found: {LOG_FILE}")
else:
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    keep  = lines[-KEEP_LINES:] if len(lines) > KEEP_LINES else lines
    total = len(lines)

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.writelines(keep)

    print(f"Log flushed — kept last {len(keep)} of {total} lines.")
    print(f"Log file: {LOG_FILE}")

if not os.path.exists(SCAN_LOG):
    print(f"Scan Log file not found: {SCAN_LOG}")
else:
    with open(SCAN_LOG, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    keep  = lines[-KEEP_LINES:] if len(lines) > KEEP_LINES else lines
    total = len(lines)

    with open(SCAN_LOG, "w", encoding="utf-8") as f:
        f.writelines(keep)

    print(f"Scan Log flushed — kept last {len(keep)} of {total} lines.")
    print(f"Scan Log file: {SCAN_LOG}")


