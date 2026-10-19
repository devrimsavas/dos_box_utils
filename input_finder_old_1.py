"""
DOSBox-X Input Finder

Finds where a DOS game keeps its keyboard state, without knowing where to look.

How it works:
  1. Learn noise: for a few seconds, with nobody touching the keyboard, every byte that changes
     on its own (timers, animations, music) is marked as noise and ignored from then on.
  2. Watch: the current memory becomes the "idle" baseline. Now tap a key in the game a few times.
     A key-state byte changes when the key goes down and returns to its idle value when the key is
     released. Each return counts as one press.
  3. Bytes whose press count matches the number of times you tapped the key are that key.
     Positions, counters and animation frames also change, but they rarely return exactly to
     their idle value, so they stay at 0 presses.
"""

import ctypes
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np

import base_finder as bf

DOS_MEMORY_SIZE = 0x110000        # conventional + upper memory + HMA
VIDEO_MEMORY = (0xA0000, 0xBFFFF)
READ_BLOCK = 0x10000
TICK_MS = 40                      # memory is sampled 25 times a second
TABLE_REFRESH_MS = 250
NOISE_SECONDS = 3
MAX_ROWS = 150


# =====================================================================
# Core logic (no GUI)
# =====================================================================

def area_name(offset):
    if offset < 0x400:
        return "Vectors"
    if offset < 0x500:
        return "BIOS data"
    if VIDEO_MEMORY[0] <= offset <= VIDEO_MEMORY[1]:
        return "Video"
    if offset >= 0xC0000:
        return "ROM / upper"
    return "Program"


def format_seg_off(offset):
    if offset >= 0x100000:
        return "-"
    return f"{offset >> 4:04X}:{offset & 0xF:04X}"


class InputTracker:
    """Tracks which bytes leave their idle value and come back, i.e. behave like key states."""

    def __init__(self, size=DOS_MEMORY_SIZE):
        self.size = size
        self.noise = np.zeros(size, dtype=bool)
        self.excluded = np.zeros(size, dtype=bool)
        self.baseline = None
        self.reset_results()

    def set_excluded_range(self, start, end, excluded):
        self.excluded[start:end + 1] = excluded

    # ----- Noise -----

    def clear_noise(self):
        self.noise[:] = False

    def add_noise(self, reference, current):
        """Marks every byte that differs between two idle samples as noise."""
        self.noise |= reference != current

    def ignore_offsets(self, offsets):
        self.noise[list(offsets)] = True

    # ----- Watching -----

    def reset_results(self):
        self.active_prev = np.zeros(self.size, dtype=bool)
        self.presses = np.zeros(self.size, dtype=np.uint16)
        self.touched = np.zeros(self.size, dtype=bool)
        self.last_value = np.zeros(self.size, dtype=np.uint8)

    def start(self, baseline):
        self.baseline = baseline.copy()
        self.reset_results()
        self.last_value = baseline.copy()

    def update(self, current):
        """Processes one memory sample. Returns the mask of bytes that are away from idle right now."""
        active = (current != self.baseline) & ~self.noise & ~self.excluded
        released = self.active_prev & ~active
        self.presses[released & (self.presses < 0xFFFF)] += 1
        self.touched |= active
        self.last_value[active] = current[active]
        self.active_prev = active
        return active

    def results(self, current, limit=MAX_ROWS, taps=None):
        """
        Rows sorted by: press count equal to `taps` first (if given), then press count,
        then 'active now', then offset.
        Each row: (offset, idle, last_active_value, current, presses, active_now).
        Bytes that were later marked as noise are left out.
        """
        idx = np.nonzero(self.touched & ~self.noise & ~self.excluded)[0]
        if len(idx) == 0:
            return [], 0
        presses = self.presses[idx].astype(np.int32)
        active_now = self.active_prev[idx]
        exact = presses == taps if taps else np.zeros(len(idx), dtype=bool)
        order = np.lexsort((idx, ~active_now, -presses, ~exact))
        idx = idx[order][:limit]
        rows = [(int(o), int(self.baseline[o]), int(self.last_value[o]), int(current[o]),
                 int(self.presses[o]), bool(self.active_prev[o])) for o in idx]
        return rows, int(np.count_nonzero(self.touched & ~self.noise & ~self.excluded))


def read_dos_memory(h_process, base, size=DOS_MEMORY_SIZE):
    """Reads DOS memory; unreadable blocks are returned as zeros so one bad page does not stop the scan."""
    data = bf.read_bytes(h_process, base, size)
    if data and len(data) == size:
        return np.frombuffer(data, dtype=np.uint8)
    parts = []
    for start in range(0, size, READ_BLOCK):
        n = min(READ_BLOCK, size - start)
        block = bf.read_bytes(h_process, base + start, n)
        parts.append(block if block and len(block) == n else bytes(n))
    return np.frombuffer(b"".join(parts), dtype=np.uint8)


# =====================================================================
# GUI
# =====================================================================

STATE_IDLE = "idle"
STATE_LEARNING = "learning"
STATE_WATCHING = "watching"


class InputFinderApp:
    COLUMNS = [
        ("offset", "Offset", 100, tk.W),
        ("segoff", "Seg:Off", 95, tk.CENTER),
        ("area", "Area", 90, tk.CENTER),
        ("idle", "Idle", 60, tk.CENTER),
        ("pressed", "Pressed", 70, tk.CENTER),
        ("now", "Now", 60, tk.CENTER),
        ("presses", "Presses", 70, tk.CENTER),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Input Finder")
        self.root.geometry("760x760")
        self.root.minsize(680, 560)

        self.tracker = InputTracker()
        self.tracker.set_excluded_range(*VIDEO_MEMORY, True)
        self.h_process = None
        self.base = None
        self.state = STATE_IDLE
        self.learn_until = 0
        self.noise_reference = None
        self.current = None
        self.after_tick = None
        self.after_table = None
        self.events = queue.Queue()
        self.worker = None

        self.setup_style()
        self.setup_ui()
        self.refresh_pid_list()
        self.poll_events()

    # ----- Layout -----

    def setup_style(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", font=("Consolas", 10), rowheight=22)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("Step.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 6))

    def setup_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Process and Base
        conn = ttk.Frame(main)
        conn.pack(fill=tk.X)
        ttk.Label(conn, text="PID:").pack(side=tk.LEFT)
        self.combo_pid = ttk.Combobox(conn, width=24)
        self.combo_pid.pack(side=tk.LEFT, padx=4)
        ttk.Button(conn, text="Refresh", width=8, command=self.refresh_pid_list).pack(side=tk.LEFT)
        ttk.Label(conn, text="Base (hex):").pack(side=tk.LEFT, padx=(14, 0))
        self.var_base = tk.StringVar()
        ttk.Entry(conn, textvariable=self.var_base, width=18).pack(side=tk.LEFT, padx=4)
        self.btn_find_base = ttk.Button(conn, text="Find Base", command=self.find_base)
        self.btn_find_base.pack(side=tk.LEFT)

        # Steps
        steps = ttk.LabelFrame(main, text="Steps", padding=8)
        steps.pack(fill=tk.X, pady=8)

        btns = ttk.Frame(steps)
        btns.pack(fill=tk.X)
        self.btn_learn = ttk.Button(btns, text=f"1. Learn noise ({NOISE_SECONDS} s)", style="Step.TButton",
                                    command=self.start_learning)
        self.btn_learn.pack(side=tk.LEFT)
        self.btn_watch = ttk.Button(btns, text="2. Start watching", style="Step.TButton",
                                    command=self.start_watching)
        self.btn_watch.pack(side=tk.LEFT, padx=6)
        self.btn_stop = ttk.Button(btns, text="Stop", command=self.stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)
        self.var_skip_video = tk.BooleanVar(value=True)
        ttk.Checkbutton(btns, text="Ignore video memory", variable=self.var_skip_video,
                        command=self.on_video_toggle).pack(side=tk.RIGHT)
        self.var_taps = tk.StringVar(value="5")
        e_taps = ttk.Entry(btns, textvariable=self.var_taps, width=4)
        e_taps.pack(side=tk.RIGHT, padx=(4, 16))
        e_taps.bind("<KeyRelease>", lambda _e: self.refresh_table())
        ttk.Label(btns, text="I tapped:").pack(side=tk.RIGHT)

        guide = (
            "1. In the game, stand still and do not touch the keyboard. Press 'Learn noise'.\n"
            "2. Press 'Start watching', switch to the game and tap ONE key exactly as many times as 'I tapped'.\n"
            "3. Come back here: rows marked ★ pressed exactly that often are the key. Green = held right now."
        )
        ttk.Label(steps, text=guide, foreground="gray", justify=tk.LEFT).pack(anchor=tk.W, pady=(8, 0))

        # Results
        table = ttk.Frame(main)
        table.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings",
                                 selectmode="extended")
        for col_id, heading, width, anchor in self.COLUMNS:
            self.tree.heading(col_id, text=heading)
            self.tree.column(col_id, width=width, anchor=anchor)
        self.tree.tag_configure("active", background="#bbf7d0")
        self.tree.tag_configure("match", background="#dbeafe", foreground="#1d4ed8")
        self.tree.tag_configure("pressed", foreground="#1d4ed8")
        self.tree.tag_configure("quiet", foreground="#9ca3af")
        vsb = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda _e: self.copy_for_trainer())

        # Actions
        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X, pady=(8, 0))
        self.lbl_status = ttk.Label(bottom, text="Ready", foreground="gray")
        self.lbl_status.pack(side=tk.LEFT)
        ttk.Button(bottom, text="Copy for Trainer", command=self.copy_for_trainer).pack(side=tk.RIGHT)
        ttk.Button(bottom, text="Ignore selected", command=self.ignore_selected).pack(side=tk.RIGHT, padx=6)
        ttk.Button(bottom, text="Clear results", command=self.clear_results).pack(side=tk.RIGHT)

    # ----- Process and Base -----

    def refresh_pid_list(self):
        processes = bf.find_dosbox_processes()
        items = [f"{pid} - {name}" for pid, name in processes]
        self.combo_pid["values"] = items
        self.combo_pid.set(items[0] if items else "")
        if not items:
            self.set_status("psutil not installed, enter PID manually" if bf.psutil is None
                            else "DOSBox is not running", "red")

    def get_selected_pid(self):
        return int(self.combo_pid.get().strip().split(" - ")[0].strip())

    def find_base(self, then=None):
        if self.worker and self.worker.is_alive():
            return
        try:
            pid = self.get_selected_pid()
        except ValueError:
            self.set_status("Select a DOSBox process or enter a valid PID", "red")
            return
        self.btn_find_base.config(state=tk.DISABLED)
        self.set_status("Finding Base...", "blue")

        def work():
            h = bf.kernel32.OpenProcess(bf.PROCESS_QUERY_INFORMATION | bf.PROCESS_VM_READ, False, pid)
            if not h:
                self.events.put(("base_done", None, f"Could not open PID {pid}", then))
                return
            try:
                bases, _, _ = bf.scan_for_bases(h, lambda *a: None, threading.Event())
            finally:
                bf.kernel32.CloseHandle(h)
            if len(bases) == 1:
                self.events.put(("base_done", bases[0], None, then))
            else:
                reason = f"{len(bases)} candidates, use the Base Finder" if bases else "no DOS memory found"
                self.events.put(("base_done", None, reason, then))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def poll_events(self):
        try:
            while True:
                kind, base, error, then = self.events.get_nowait()
                if kind == "base_done":
                    self.worker = None
                    self.btn_find_base.config(state=tk.NORMAL)
                    if base is None:
                        self.set_status(f"Base not found: {error}", "red")
                    else:
                        self.var_base.set(f"0x{base:X}")
                        self.set_status(f"Base found: 0x{base:X}", "green")
                        if then:
                            then()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def ensure_attached(self, then):
        """Opens the process and makes sure the Base is known. Calls `then` when ready."""
        try:
            pid = self.get_selected_pid()
        except ValueError:
            self.set_status("Select a DOSBox process or enter a valid PID", "red")
            return False
        text = self.var_base.get().strip()
        if not text:
            self.find_base(then=then)
            return False
        try:
            self.base = int(text, 16)
        except ValueError:
            self.set_status("Invalid Base (hex expected)", "red")
            return False
        if not self.h_process:
            self.h_process = bf.kernel32.OpenProcess(bf.PROCESS_QUERY_INFORMATION | bf.PROCESS_VM_READ, False, pid)
            if not self.h_process:
                self.set_status(f"Could not open PID {pid} (error {ctypes.get_last_error()})", "red")
                return False
        return True

    # ----- Steps -----

    def start_learning(self):
        if not self.ensure_attached(self.start_learning):
            return
        self.tracker.clear_noise()
        self.noise_reference = read_dos_memory(self.h_process, self.base)
        self.learn_until = time.monotonic() + NOISE_SECONDS
        self.state = STATE_LEARNING
        self.update_buttons()
        self.set_status(f"Learning noise... hands off the keyboard for {NOISE_SECONDS} seconds", "orange")
        self.schedule_tick()

    def start_watching(self):
        if not self.ensure_attached(self.start_watching):
            return
        baseline = read_dos_memory(self.h_process, self.base)
        self.tracker.start(baseline)
        self.current = baseline
        self.state = STATE_WATCHING
        self.update_buttons()
        noise = int(np.count_nonzero(self.tracker.noise))
        hint = "" if noise else "  (tip: run 'Learn noise' first)"
        self.set_status(f"Watching. Switch to the game and tap one key a few times.{hint}", "green")
        self.schedule_tick()
        self.schedule_table()

    def stop(self):
        self.state = STATE_IDLE
        for after_id in (self.after_tick, self.after_table):
            if after_id:
                self.root.after_cancel(after_id)
        self.after_tick = self.after_table = None
        self.update_buttons()
        self.refresh_table()
        self.set_status("Stopped. Results are kept until you clear them or watch again.", "gray")

    def update_buttons(self):
        busy = self.state != STATE_IDLE
        self.btn_learn.config(state=tk.DISABLED if busy else tk.NORMAL)
        self.btn_watch.config(state=tk.DISABLED if busy else tk.NORMAL)
        self.btn_stop.config(state=tk.NORMAL if self.state == STATE_WATCHING else tk.DISABLED)

    def on_video_toggle(self):
        self.tracker.set_excluded_range(*VIDEO_MEMORY, self.var_skip_video.get())
        self.refresh_table()

    # ----- Sampling -----

    def schedule_tick(self):
        self.after_tick = self.root.after(TICK_MS, self.tick)

    def tick(self):
        if self.state == STATE_IDLE or not self.h_process:
            return
        current = read_dos_memory(self.h_process, self.base)

        if self.state == STATE_LEARNING:
            self.tracker.add_noise(self.noise_reference, current)
            if time.monotonic() >= self.learn_until:
                self.state = STATE_IDLE
                self.update_buttons()
                noise = int(np.count_nonzero(self.tracker.noise))
                self.set_status(f"Noise learned: {noise:,} bytes change on their own and will be ignored. "
                                "Now press 'Start watching'.", "green")
                return
        elif self.state == STATE_WATCHING:
            self.tracker.update(current)
            self.current = current

        self.schedule_tick()

    def schedule_table(self):
        self.refresh_table()
        if self.state == STATE_WATCHING:
            self.after_table = self.root.after(TABLE_REFRESH_MS, self.schedule_table)

    def refresh_table(self):
        if self.tracker.baseline is None or self.current is None:
            return
        selected = set(self.tree.selection())
        taps = self.read_taps()
        rows, total = self.tracker.results(self.current, taps=taps)

        self.tree.delete(*self.tree.get_children())
        for offset, idle, pressed, now, presses, active in rows:
            match = bool(taps) and presses == taps
            if active:
                tag = "active"
            elif match:
                tag = "match"
            else:
                tag = "pressed" if presses else "quiet"
            iid = str(offset)
            self.tree.insert("", tk.END, iid=iid, tags=(tag,), values=(
                f"0x{offset:05X}", format_seg_off(offset), area_name(offset),
                f"{idle:02X}", f"{pressed:02X}", f"{now:02X}",
                f"★ {presses}" if match else presses))
            if iid in selected:
                self.tree.selection_add(iid)

        if self.state == STATE_WATCHING:
            shown = f" (showing {len(rows)})" if total > len(rows) else ""
            self.set_status(f"Watching. {total:,} bytes left their idle value{shown}.", "green")

    def read_taps(self):
        try:
            taps = int(self.var_taps.get().strip())
            return taps if taps > 0 else None
        except ValueError:
            return None

    # ----- Actions -----

    def selected_offsets(self):
        return [int(iid) for iid in self.tree.selection()]

    def copy_for_trainer(self):
        offsets = self.selected_offsets()
        if not offsets or self.base is None:
            self.set_status("Select a row first.", "orange")
            return
        text = "\n".join(f"0x{self.base:X} + 0x{o:X}" for o in offsets)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status(f"Copied {len(offsets)} trainer address(es).", "green")

    def ignore_selected(self):
        offsets = self.selected_offsets()
        if offsets:
            self.tracker.ignore_offsets(offsets)
            self.refresh_table()
            self.set_status(f"Ignoring {len(offsets)} more byte(s).", "gray")

    def clear_results(self):
        if self.current is not None and self.tracker.baseline is not None:
            self.tracker.start(self.current)
        self.tree.delete(*self.tree.get_children())
        self.set_status("Results cleared. The current memory is the new idle baseline.", "gray")

    def set_status(self, text, color="gray"):
        self.lbl_status.config(text=text, foreground=color)


if __name__ == "__main__":
    root = tk.Tk()
    InputFinderApp(root)
    root.mainloop()