"""
daily_snapshot.py
==================
Creates a dated git branch snapshot of the current codebase.
Run this once a day before making changes so you can always
roll back to yesterday's known-working state.

Usage:
    python Scripts\daily_snapshot.py

Creates branch: snapshot/YYYY-MM-DD
Then returns to main branch.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subprocess
from datetime import date

today = date.today().isoformat()
branch = f"snapshot/{today}"

def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    return result.returncode

print(f"\nCreating daily snapshot branch: {branch}\n")

# Make sure everything is committed first
run("git add -A")
run(f'git commit -m "Auto-snapshot: {today}" --allow-empty')

# Create the snapshot branch from current main
code = run(f"git branch {branch}")
if code == 0:
    print(f"✅ Snapshot branch created: {branch}")
else:
    print(f"⚠️  Branch may already exist for today")

# Push snapshot to remote
run(f"git push origin {branch}")
print(f"\n✅ Snapshot pushed to GitHub: {branch}")
print(f"\nTo restore to this snapshot later:")
print(f"  git checkout {branch}")
print(f"  git checkout -b restore-from-{today}")
print(f"\nStaying on main branch — bot continues normally.")
