import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
os.chdir(_project_root)

from data.database import db


if len(sys.argv) >= 2:
    if sys.argv[1] == "-":
        # Piped via stdin: echo grid_bot | python Scripts/enable_strat.py -
        plaintext = sys.stdin.read().strip()
    else:
        plaintext = sys.argv[1].strip()
else:
    plaintext = input("Strategy to Enable ").strip()

if not plaintext:
    print("ERROR: No strategy provided.", file=sys.stderr)
    sys.exit(1)

if plaintext.startswith("strategy_") and plaintext.endswith("_enabled"):
    key = plaintext
else:
    key = f"strategy_{plaintext}_enabled"

db.set_state(key, True)
print(f"Done {key} = true")
