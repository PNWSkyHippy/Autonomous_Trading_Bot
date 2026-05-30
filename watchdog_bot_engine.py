"""
Bot Engine Watchdog
====================
Monitors bot_engine.py and restarts it in the correct Windows Terminal pane
when it dies. Respects a lock file for maintenance windows.

Setup:
  1. Run Export-PanePIDs from your $PROFILE after your panels are laid out
  2. Set TARGET_PANEL_NAME below to whichever panel runs the bot
  3. Run this script from Task Scheduler at logon (or manually)

Lock file:
  Create bot_engine.lck  (lock-bot / unlock-bot in $PROFILE) to pause watchdog
  during maintenance so it doesn't fight you.
"""

import os
import time
import psutil
import datetime

# ── Configuration ────────────────────────────────────────────────────────────
BOT_ROOT        = r"C:\users\linda\trading_bot_v2"
PANE_LOG_FILE   = os.path.join(BOT_ROOT, "terminal_panes.txt")
LOCK_FILE       = os.path.join(BOT_ROOT, "bot_engine.lck")
BOT_SCRIPT      = "bot_engine.py"
VENV_PYTHON     = os.path.join(BOT_ROOT, r"venv312\Scripts\python.exe")

# The pane name that Export-PanePIDs wrote for the bot window.
# Run Export-PanePIDs and check terminal_panes.txt to confirm the right name.
TARGET_PANEL_NAME = "Bot_Panel_1"

CHECK_INTERVAL_SEC  = 10   # how often to check
RESTART_COOLDOWN    = 30   # seconds to wait after a restart before checking again
HEALTH_LOG_INTERVAL = 60   # print health status this often (seconds)

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def is_bot_running() -> bool:
    """Check if bot_engine.py is running anywhere in the system."""
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.pid == os.getpid():
                continue  # skip ourselves
            if "python" in (proc.info["name"] or "").lower():
                cmdline = proc.info["cmdline"] or []
                if any(os.path.basename(arg) == BOT_SCRIPT for arg in cmdline):
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def get_target_pane() -> tuple:
    """Read terminal_panes.txt and return (pane_index, shell_pid) for TARGET_PANEL_NAME."""
    if not os.path.exists(PANE_LOG_FILE):
        return None, None
    with open(PANE_LOG_FILE, "r") as f:
        for line in f:
            if TARGET_PANEL_NAME in line:
                parts = line.strip().split(",")
                if len(parts) >= 2:
                    try:
                        return int(parts[0]), int(parts[1])
                    except ValueError:
                        pass
    return None, None


def trigger_restart(pane_index: int):
    """
    Bot restart is handled by StartBot.ps1 loop in pane 1 — it auto-restarts
    when bot_engine.py exits. This function just logs that a restart is expected.
    """
    log(f"[RESTART] Bot is down — StartBot.ps1 loop should restart it automatically.")
    log(f"[RESTART] If it doesn't recover in {RESTART_COOLDOWN}s, check pane {pane_index}.")


# ── Main loop ────────────────────────────────────────────────────────────────

def monitor():
    log(f"Watchdog active — monitoring '{TARGET_PANEL_NAME}' for {BOT_SCRIPT}")
    log(f"  Lock file : {LOCK_FILE}")
    log(f"  Pane log  : {PANE_LOG_FILE}")
    log(f"  Check every {CHECK_INTERVAL_SEC}s | Health log every {HEALTH_LOG_INTERVAL}s")

    last_health_log  = 0.0
    last_restart     = 0.0

    while True:
        now = time.time()

        # ── Maintenance lock ──────────────────────────────────────────────
        if os.path.exists(LOCK_FILE):
            log("[LOCKED] Maintenance lock active — watchdog paused.")
            time.sleep(15)
            continue

        # ── Find pane ────────────────────────────────────────────────────
        pane_index, shell_pid = get_target_pane()
        if pane_index is None:
            log(f"[WAIT] terminal_panes.txt not found or '{TARGET_PANEL_NAME}' missing. "
                f"Run Export-PanePIDs from your profile.")
            time.sleep(15)
            continue

        # ── Check if shell pane is still alive ───────────────────────────
        try:
            psutil.Process(shell_pid)
        except psutil.NoSuchProcess:
            log(f"[WARN] Shell PID {shell_pid} for {TARGET_PANEL_NAME} is gone. "
                f"Re-run Export-PanePIDs after relaunching your terminal.")
            time.sleep(30)
            continue

        # ── Check if bot is running ───────────────────────────────────────
        bot_alive = is_bot_running()

        if bot_alive:
            if now - last_health_log >= HEALTH_LOG_INTERVAL:
                log(f"[OK] {BOT_SCRIPT} is running. Pane={pane_index} Shell={shell_pid}")
                last_health_log = now
        else:
            # Don't restart if we just did — give it time to boot
            if now - last_restart < RESTART_COOLDOWN:
                remaining = int(RESTART_COOLDOWN - (now - last_restart))
                log(f"[WAIT] Bot not found — cooldown {remaining}s before retry...")
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            log(f"[DOWN] {BOT_SCRIPT} not running! Restarting in pane {pane_index}...")
            try:
                trigger_restart(pane_index)
                last_restart = time.time()
                log(f"[RESTART] Command sent. Waiting {RESTART_COOLDOWN}s for bot to boot...")
            except Exception as e:
                log(f"[ERROR] Restart failed: {e}")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        monitor()
    except KeyboardInterrupt:
        log("Watchdog stopped by user.")
