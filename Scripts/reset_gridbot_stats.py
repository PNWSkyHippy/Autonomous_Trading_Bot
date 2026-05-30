"""
reset_gridbot_stats.py
=======================
Emergency reset for grid_bot when it gets auto-disabled during testing.
Clears win/loss history and forces grid_bot enabled.

Safe to run while bot is stopped. Restart bot after running.

Usage:
    python Scripts\reset_gridbot_stats.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3

conn = sqlite3.connect("data/trading_bot.db")

# Clear grid_bot strategy results (win/loss history that triggered auto-disable)
deleted = conn.execute(
    "DELETE FROM strategy_results WHERE strategy_name = 'grid_bot'"
).rowcount
print(f"Cleared {deleted} grid_bot win/loss records")

# Force enable in bot_state
conn.execute(
    "INSERT OR REPLACE INTO bot_state (key, value, updated) "
    "VALUES ('strategy_grid_bot_enabled', 'true', datetime('now'))"
)
print("Set strategy_grid_bot_enabled = true")

# Clear any other grid_bot state flags that might interfere
conn.execute(
    "DELETE FROM bot_state WHERE key LIKE '%grid_bot%' "
    "AND key != 'strategy_grid_bot_enabled'"
)
print("Cleared all other grid_bot state flags")

conn.commit()

# Verify
print("\nCurrent grid_bot state in DB:")
for row in conn.execute(
    "SELECT key, value FROM bot_state WHERE key LIKE '%grid_bot%'"
).fetchall():
    print(f"  {row[0]} = {row[1]}")

count = conn.execute(
    "SELECT COUNT(*) FROM strategy_results WHERE strategy_name = 'grid_bot'"
).fetchone()[0]
print(f"grid_bot strategy_results records remaining: {count}")

conn.close()
print("\nDone — restart bot to apply.")
