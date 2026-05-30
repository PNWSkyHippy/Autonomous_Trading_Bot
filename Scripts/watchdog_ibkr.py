"""
watchdog_ibkr.py — IBKR TWS / IB Gateway process watchdog

Runs as a standalone process alongside the main bot.
Every CHECK_INTERVAL seconds it tries to open a TCP socket to the
IBKR API port.  If FAIL_THRESHOLD consecutive checks fail it kills
the TWS/Gateway process and restarts it, then waits for the port to
come back up before declaring success.

Works on Windows (TWS) and Linux (IB Gateway / TWS via IBC).

Usage:
    python Scripts/watchdog_ibkr.py              # paper (port 7497)
    python Scripts/watchdog_ibkr.py --live       # live  (port 7496)
    python Scripts/watchdog_ibkr.py --dry-run    # check only, never restart

Auto-login requirement:
    TWS: Settings → Lock and Exit → "Auto login" must be enabled,
         or use IBC (https://github.com/IbcAlpha/IBC) which handles
         headless login on Linux colocation.
    IB Gateway: supports auto-login natively via the config file.
"""

import argparse
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────
PAPER_PORT      = 7497
LIVE_PORT       = 7496
IBKR_HOST       = "127.0.0.1"

CHECK_INTERVAL  = 30     # seconds between health checks
FAIL_THRESHOLD  = 3      # consecutive failures before restart
RESTART_TIMEOUT = 180    # seconds to wait for port after restart (TWS cold start ~2-3 min)
SOCKET_TIMEOUT  = 5      # TCP connect timeout per check

# ── Process names / launch commands ─────────────────────────────────────────
# Adjust TWS_EXEC to match your installation path.
IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    TWS_PROCESS_NAME = "tws.exe"
    TWS_EXEC = r"C:\IBC\StartGateway.bat"        # IBC launcher — handles auto-login on unplanned restarts
    GATEWAY_PROCESS_NAME = "ibgateway.exe"
    GATEWAY_EXEC = r"C:\IBC\StartGateway.bat"    # same IBC bat for gateway
else:
    # Linux / macOS (colocation)
    TWS_PROCESS_NAME = "java"                     # TWS and Gateway both run as java
    TWS_EXEC = "/opt/ibc/scripts/DisplayBannerAndLaunch.sh"   # IBC launcher
    GATEWAY_PROCESS_NAME = "java"
    GATEWAY_EXEC = "/opt/ibc/scripts/DisplayBannerAndLaunch.sh"

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "watchdog_ibkr.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("watchdog_ibkr")


# ── Helpers ──────────────────────────────────────────────────────────────────

def port_alive(host: str, port: int, timeout: float = SOCKET_TIMEOUT) -> bool:
    """Return True if the TCP port accepts a connection."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError, TimeoutError):
        return False


def find_ibkr_pids() -> list[int]:
    """
    Return PIDs of running IBKR processes.
    Windows: tasklist + wmic for javaw.exe + netstat port owner; Linux: pgrep.
    """
    pids = []
    try:
        if IS_WINDOWS:
            import subprocess

            def _tasklist_pids(image_name: str) -> list[int]:
                r = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True
                )
                found = []
                for line in r.stdout.splitlines():
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        try:
                            found.append(int(parts[1]))
                        except ValueError:
                            pass
                return found

            # Check named executables (older TWS installs)
            for name in (TWS_PROCESS_NAME, GATEWAY_PROCESS_NAME):
                for pid in _tasklist_pids(name):
                    if pid not in pids:
                        pids.append(pid)

            # IBC on Windows runs as javaw.exe — check command line for IBC/gateway keywords
            try:
                wmic_result = subprocess.run(
                    ["wmic", "process", "where",
                     "name='javaw.exe'",
                     "get", "ProcessId,CommandLine", "/value"],
                    capture_output=True, text=True, timeout=10
                )
                current_pid = None
                keywords = ("ibgateway", "ibc", "tws", "trader workstation", "jts")
                for line in wmic_result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("ProcessId="):
                        try:
                            current_pid = int(line.split("=", 1)[1].strip())
                        except ValueError:
                            current_pid = None
                    elif line.startswith("CommandLine="):
                        cmd = line.lower()
                        if any(kw in cmd for kw in keywords):
                            if current_pid and current_pid not in pids:
                                pids.append(current_pid)
            except Exception as e:
                logger.warning(f"wmic javaw scan failed: {e}")

            # Fallback: find whatever process owns the IBKR port via netstat
            if not pids:
                try:
                    ns = subprocess.run(
                        ["netstat", "-ano", "-p", "TCP"],
                        capture_output=True, text=True, timeout=10
                    )
                    for line in ns.stdout.splitlines():
                        if f":{PAPER_PORT}" in line or f":{LIVE_PORT}" in line:
                            parts = line.split()
                            if parts:
                                try:
                                    pid = int(parts[-1])
                                    if pid not in pids:
                                        pids.append(pid)
                                        logger.info(f"Found port-owner PID via netstat: {pid}")
                                except ValueError:
                                    pass
                except Exception as e:
                    logger.warning(f"netstat port-owner scan failed: {e}")
        else:
            result = subprocess.run(
                ["pgrep", "-f", "ibgateway|tws|Trader Workstation"],
                capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                try:
                    pids.append(int(line.strip()))
                except ValueError:
                    pass
    except Exception as e:
        logger.warning(f"PID discovery error: {e}")
    return pids


def kill_ibkr(pids: list[int]) -> None:
    """Kill all IBKR PIDs."""
    for pid in pids:
        try:
            if IS_WINDOWS:
                # SIGTERM is a no-op for Java on Windows; use taskkill /F directly
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                logger.info(f"taskkill /F PID {pid}")
            else:
                os.kill(pid, signal.SIGTERM)
                logger.info(f"Sent SIGTERM to PID {pid}")
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"Kill PID {pid} failed: {e}")

    # Give processes 20s to fully release handles and ports before relaunch
    time.sleep(20)
    for pid in pids:
        try:
            os.kill(pid, 0)   # check still alive
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, signal.SIGKILL)
            logger.info(f"Force-killed PID {pid} (still alive after grace period)")
        except ProcessLookupError:
            pass   # already dead, good


def _kill_by_name() -> None:
    """
    Kill any running IBKR/IBC processes by image name rather than PID.

    This is the fallback for when find_ibkr_pids() misses processes — e.g.
    when wmic is slow, the process name varies, or IBC spawned extra child
    windows.  Running this unconditionally before every launch ensures we
    never stack multiple IBC copies even if PID detection is unreliable.

    Targets javaw.exe (IBC/TWS on Windows) with /T so child windows are
    included.  Safe: on a trading PC javaw.exe is virtually always IBC/TWS.
    On Linux kills any java process matching ibgateway/tws in its cmdline.
    """
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/IM", "javaw.exe"],
                capture_output=True, text=True
            )
            if "SUCCESS" in result.stdout or "javaw.exe" in result.stdout:
                logger.info(f"[NAME KILL] javaw.exe terminated: {result.stdout.strip()}")
            else:
                logger.debug(f"[NAME KILL] No javaw.exe processes found (already clean)")
            # Also try ibgateway.exe for older installs
            subprocess.run(
                ["taskkill", "/F", "/IM", "ibgateway.exe"],
                capture_output=True
            )
        else:
            subprocess.run(
                ["pkill", "-f", "ibgateway|Trader Workstation"],
                capture_output=True
            )
            logger.info("[NAME KILL] pkill ibgateway/TWS sent")
        time.sleep(5)   # give killed processes time to release the port
    except Exception as e:
        logger.warning(f"[NAME KILL] fallback kill error: {e}")


def launch_ibkr(exec_path: str) -> None:
    """Launch TWS or IB Gateway."""
    if not Path(exec_path).exists():
        logger.error(
            f"Launch path not found: {exec_path}  "
            f"— edit TWS_EXEC / GATEWAY_EXEC at the top of this script"
        )
        return
    try:
        if IS_WINDOWS:
            # CREATE_NEW_CONSOLE gives IBC Gateway its own visible window and
            # proper desktop access — Java GUI apps fail silently with DETACHED_PROCESS
            subprocess.Popen(
                ["cmd.exe", "/c", exec_path],
                shell=False,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            subprocess.Popen(["/bin/bash", exec_path], shell=False)
        logger.info(f"Launched: {exec_path}")
    except Exception as e:
        logger.error(f"Launch failed: {e}")


def wait_for_port(host: str, port: int, timeout: int = RESTART_TIMEOUT) -> bool:
    """Poll until port responds or timeout expires."""
    deadline = time.time() + timeout
    logger.info(f"Waiting up to {timeout}s for {host}:{port} ...")
    while time.time() < deadline:
        if port_alive(host, port):
            return True
        time.sleep(5)
    return False


# ── Main watchdog loop ───────────────────────────────────────────────────────

def run(port: int, dry_run: bool = False, exec_path: str = TWS_EXEC) -> None:
    logger.info(
        f"Watchdog started — monitoring {IBKR_HOST}:{port}  "
        f"interval={CHECK_INTERVAL}s  threshold={FAIL_THRESHOLD}  "
        f"{'DRY-RUN' if dry_run else 'LIVE'}"
    )

    consecutive_failures = 0
    restart_count = 0

    while True:
        alive = port_alive(IBKR_HOST, port)

        if alive:
            if consecutive_failures > 0:
                logger.info(
                    f"Port {port} responding again after "
                    f"{consecutive_failures} failed check(s)"
                )
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            logger.warning(
                f"Port {port} not responding — "
                f"failure {consecutive_failures}/{FAIL_THRESHOLD}"
            )

            if consecutive_failures >= FAIL_THRESHOLD:
                restart_count += 1
                logger.error(
                    f"IBKR unresponsive for {consecutive_failures} checks — "
                    f"initiating restart #{restart_count}"
                )

                if not dry_run:
                    pids = find_ibkr_pids()
                    if pids:
                        logger.info(f"Found IBKR PIDs: {pids}")
                        kill_ibkr(pids)
                    else:
                        logger.info("No IBKR PIDs found via detection — running name-based kill fallback")

                    # ── Name-based fallback kill ──────────────────────────────
                    # PID detection can miss IBC's javaw.exe if wmic is slow or
                    # the process name doesn't match exactly.  Always attempt a
                    # name-based kill before launching so we never stack copies.
                    _kill_by_name()

                    time.sleep(10)   # let old session fully clear before relaunch
                    launch_ibkr(exec_path)

                    if wait_for_port(IBKR_HOST, port, RESTART_TIMEOUT):
                        logger.info(
                            f"IBKR restart #{restart_count} successful — "
                            f"port {port} is up"
                        )
                        consecutive_failures = 0
                    else:
                        logger.error(
                            f"Port {port} still not responding after "
                            f"{RESTART_TIMEOUT}s — will retry next cycle"
                        )
                        # Reset so we try again after FAIL_THRESHOLD more checks
                        consecutive_failures = 0
                else:
                    logger.info("[DRY-RUN] Would have restarted here")
                    consecutive_failures = 0

        time.sleep(CHECK_INTERVAL)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR TWS/Gateway watchdog")
    parser.add_argument("--live",    action="store_true", help="Monitor live port 7496 (default: paper 7497)")
    parser.add_argument("--dry-run", action="store_true", help="Check only — never kill or restart")
    parser.add_argument("--once",    action="store_true", help="Run a single check and exit (useful for testing)")
    parser.add_argument("--exec",    default="",          help="Override TWS/Gateway launch path")
    args = parser.parse_args()

    target_port = LIVE_PORT if args.live else PAPER_PORT
    exec_path   = args.exec if args.exec else (TWS_EXEC if not args.live else GATEWAY_EXEC)

    if args.once:
        alive = port_alive(IBKR_HOST, target_port)
        status = "UP" if alive else "DOWN"
        logger.info(f"One-shot check — {IBKR_HOST}:{target_port} is {status}")
        sys.exit(0 if alive else 1)

    try:
        run(port=target_port, dry_run=args.dry_run, exec_path=exec_path)
    except KeyboardInterrupt:
        logger.info("Watchdog stopped by user")
