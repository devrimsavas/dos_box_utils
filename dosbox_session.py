"""
Shared DOSBox session info.

The Base Finder saves the Base it found to session.json; the other tools load it so the
Base does not have to be copied and pasted.

The Base changes every time DOSBox starts, so a saved Base is only returned for the same
DOSBox process: same PID and same process start time (a new process can reuse an old PID).
"""

import json
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

SESSION_FILE = Path(__file__).with_name("session.json")


def process_start_time(pid):
    """Start time of the process in seconds, or None if it cannot be read."""
    if psutil is None:
        return None
    try:
        return round(psutil.Process(pid).create_time(), 1)
    except (psutil.Error, OSError):
        return None


def save_base(pid, base):
    """Saves the Base for this DOSBox process. Returns True on success."""
    data = {
        "pid": pid,
        "process_started": process_start_time(pid),
        "base": f"0x{base:X}",
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def load_base(pid):
    """Returns the saved Base for this DOSBox process, or None if there is none or it is outdated."""
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        if int(data["pid"]) != pid:
            return None
        saved_start = data.get("process_started")
        current_start = process_start_time(pid)
        if saved_start is not None and current_start is not None and abs(saved_start - current_start) > 1:
            return None  # same PID, but a different (newer) DOSBox process
        return int(data["base"], 16)
    except (OSError, ValueError, KeyError, TypeError):
        return None