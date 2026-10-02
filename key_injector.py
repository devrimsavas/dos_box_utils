"""
DOSBox-X Key Injector

A small on-screen keypad that "presses" keys in a DOS game by writing to memory.
Each of the 10 rows is one key. Hold its button (or the number key 1-0 while this window is
focused) to keep the key pressed, release to let it go.

Two modes per row:
  * Memory:   for games with their own key-state table (found with the Input Finder).
              While held, the Pressed bytes are written to the address again and again;
              on release, the Idle bytes are written back.
  * BIOS key: for games that read keys through the BIOS (INT 16h).
              The key is pushed into the BIOS keyboard buffer at 0040:001E, exactly as if it
              had been typed. Held keys repeat like a real keyboard.

Rows can be saved as a profile per game (profiles/<name>.json) and loaded later.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import base_finder as bf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "profiles")

ROWS = 10
HOLD_INTERVAL_MS = 20       # how often a held Memory key is written again
TYPEMATIC_DELAY_MS = 300    # BIOS key: delay before a held key starts repeating
TYPEMATIC_RATE_MS = 100     # BIOS key: repeat interval

MODE_MEMORY = "Memory"
MODE_BIOS = "BIOS key"
MODES = [MODE_MEMORY, MODE_BIOS]

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020

# BIOS data area (offsets from the start of DOS memory)
BDA_KBD_HEAD = 0x41A
BDA_KBD_TAIL = 0x41C
BDA_KBD_BUFFER_START = 0x480
BDA_KBD_BUFFER_END = 0x482
BDA_SEGMENT_BASE = 0x400        # head/tail/start/end are offsets inside segment 0040
DEFAULT_BUFFER = (0x1E, 0x3E)

kernel32 = bf.kernel32
kernel32.WriteProcessMemory.restype = wt.BOOL
kernel32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

KEY_CODES_HELP = """Common scan codes (BIOS key mode)

F1 3B   F2 3C   F3 3D   F4 3E   F5 3F
F6 40   F7 41   F8 42   F9 43   F10 44

Up 48   Down 50   Left 4B   Right 4D
Space 39   Enter 1C   Esc 01   Tab 0F
Ctrl 1D   Alt 38   Left Shift 2A

A 1E  S 1F  D 20  W 11  Z 2C  X 2D

Enter one byte for keys without a character (e.g. 42 for F8),
or ASCII + scan code for letters (e.g. 61 1E for 'a')."""


# =====================================================================
# Core logic (no GUI)
# =====================================================================

def parse_address(text):
    """Hex offset ('11C48', '0x11C48') or segment:offset as the debugger shows it ('1226:27D4')."""
    text = text.strip().replace(" ", "")
    if not text:
        raise ValueError("Address is empty")
    if ":" in text:
        seg, off = text.split(":", 1)
        return int(seg, 16) * 16 + int(off, 16)
    return int(text, 16)


def parse_bytes(text):
    """Hex bytes: '01', '1', '00 42' or '0042'. Empty -> b''."""
    text = text.strip()
    if not text:
        return b""
    tokens = text.split()
    if len(tokens) > 1:
        return bytes(int(t, 16) for t in tokens)
    token = tokens[0].lower()
    if token.startswith("0x"):
        token = token[2:]
    if len(token) % 2:
        token = "0" + token
    return bytes.fromhex(token)


def parse_bios_key(text):
    """'42' -> (ascii 0x00, scan 0x42); '61 1E' -> (ascii 0x61, scan 0x1E)."""
    data = parse_bytes(text)
    if len(data) == 1:
        return 0x00, data[0]
    if len(data) == 2:
        return data[0], data[1]
    raise ValueError("Use one byte (scan code) or two bytes (ASCII, scan code)")


class DosMemory:
    """Reads and writes DOS memory inside the DOSBox process, addressed by DOS offset."""

    def __init__(self, h_process, base):
        self.h = h_process
        self.base = base

    def read(self, offset, size):
        return bf.read_bytes(self.h, self.base + offset, size)

    def write(self, offset, data):
        if not data:
            return True
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        written = ctypes.c_size_t()
        ok = kernel32.WriteProcessMemory(self.h, ctypes.c_void_p(self.base + offset), buf, len(data),
                                         ctypes.byref(written))
        return bool(ok) and written.value == len(data)


def word(data, i=0):
    return data[i] | (data[i + 1] << 8)


def push_bios_key(mem, ascii_code, scan_code):
    """
    Puts one key into the BIOS keyboard buffer, the same way the BIOS does when a key is typed.
    Returns True on success, False if the buffer is full or cannot be read.
    """
    pointers = mem.read(BDA_KBD_HEAD, 4)
    limits = mem.read(BDA_KBD_BUFFER_START, 4)
    if not pointers or len(pointers) < 4:
        return False
    head, tail = word(pointers, 0), word(pointers, 2)

    start, end = DEFAULT_BUFFER
    if limits and len(limits) == 4:
        s, e = word(limits, 0), word(limits, 2)
        if s < e <= 0x100 and s % 2 == 0 and e % 2 == 0:
            start, end = s, e

    next_tail = tail + 2
    if next_tail >= end:
        next_tail = start
    if next_tail == head:
        return False  # buffer full

    return (mem.write(BDA_SEGMENT_BASE + tail, bytes([ascii_code, scan_code]))
            and mem.write(BDA_KBD_TAIL, next_tail.to_bytes(2, "little")))


# =====================================================================
# GUI
# =====================================================================

class KeyRow:
    """One key of the keypad."""

    def __init__(self, parent, index, app):
        self.index = index
        self.app = app
        self.held = False
        self.after_id = None

        r = index + 1
        hotkey = str((index + 1) % 10)
        ttk.Label(parent, text=hotkey, width=2, anchor=tk.CENTER).grid(row=r, column=0, padx=2, pady=2)

        self.var_name = tk.StringVar()
        ttk.Entry(parent, textvariable=self.var_name, width=12).grid(row=r, column=1, padx=2)

        self.var_mode = tk.StringVar(value=MODE_MEMORY)
        combo = ttk.Combobox(parent, textvariable=self.var_mode, values=MODES, state="readonly", width=9)
        combo.grid(row=r, column=2, padx=2)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.on_mode_change())

        self.var_address = tk.StringVar()
        self.e_address = ttk.Entry(parent, textvariable=self.var_address, width=13)
        self.e_address.grid(row=r, column=3, padx=2)

        self.var_idle = tk.StringVar()
        self.e_idle = ttk.Entry(parent, textvariable=self.var_idle, width=8)
        self.e_idle.grid(row=r, column=4, padx=2)

        self.var_pressed = tk.StringVar()
        ttk.Entry(parent, textvariable=self.var_pressed, width=8).grid(row=r, column=5, padx=2)

        self.btn_read = ttk.Button(parent, text="Read", width=5, command=self.read_idle)
        self.btn_read.grid(row=r, column=6, padx=2)

        self.btn_press = tk.Button(parent, text="Press", width=8, relief=tk.RAISED, bg="#e5e7eb",
                                   activebackground="#e5e7eb", cursor="hand2")
        self.btn_press.grid(row=r, column=7, padx=(6, 2))
        self.btn_press.bind("<ButtonPress-1>", lambda _e: self.press())
        self.btn_press.bind("<ButtonRelease-1>", lambda _e: self.release())

    # ----- Settings -----

    def on_mode_change(self):
        bios = self.var_mode.get() == MODE_BIOS
        state = tk.DISABLED if bios else tk.NORMAL
        for widget in (self.e_address, self.e_idle, self.btn_read):
            widget.config(state=state)

    def to_dict(self):
        return {
            "name": self.var_name.get(), "mode": self.var_mode.get(), "address": self.var_address.get(),
            "idle": self.var_idle.get(), "pressed": self.var_pressed.get(),
        }

    def from_dict(self, d):
        self.var_name.set(d.get("name", ""))
        self.var_mode.set(d.get("mode", MODE_MEMORY) if d.get("mode") in MODES else MODE_MEMORY)
        self.var_address.set(d.get("address", ""))
        self.var_idle.set(d.get("idle", ""))
        self.var_pressed.set(d.get("pressed", ""))
        self.on_mode_change()

    def label(self):
        return self.var_name.get().strip() or f"Key {(self.index + 1) % 10}"

    def read_idle(self):
        """Fills Idle with the current value at the address (as many bytes as Pressed has, at least 1)."""
        mem = self.app.memory()
        if not mem:
            return
        try:
            address = parse_address(self.var_address.get())
            size = max(1, len(parse_bytes(self.var_pressed.get())))
        except ValueError as e:
            self.app.set_status(f"{self.label()}: {e}", "red")
            return
        data = mem.read(address, size)
        if data:
            self.var_idle.set(" ".join(f"{b:02X}" for b in data))
            self.app.set_status(f"{self.label()}: idle value read from 0x{address:X}", "green")

    # ----- Press / release -----

    def press(self):
        if self.held:
            return
        mem = self.app.memory()
        if not mem:
            return
        try:
            if self.var_mode.get() == MODE_BIOS:
                key = parse_bios_key(self.var_pressed.get())
                self.held = True
                self.set_look(True)
                self.bios_repeat(mem, key, first=True)
            else:
                address = parse_address(self.var_address.get())
                data = parse_bytes(self.var_pressed.get())
                if not data:
                    raise ValueError("Pressed value is empty")
                self.held = True
                self.set_look(True)
                self.memory_hold(mem, address, data)
        except ValueError as e:
            self.app.set_status(f"{self.label()}: {e}", "red")

    def release(self):
        if not self.held:
            return
        self.held = False
        self.set_look(False)
        if self.after_id:
            self.app.root.after_cancel(self.after_id)
            self.after_id = None

        if self.var_mode.get() == MODE_MEMORY:
            mem = self.app.memory(quiet=True)
            try:
                idle = parse_bytes(self.var_idle.get())
                address = parse_address(self.var_address.get())
            except ValueError:
                return
            if mem and idle:
                mem.write(address, idle)
        self.app.set_status(f"{self.label()} released", "gray")

    def memory_hold(self, mem, address, data):
        if not self.held:
            return
        if not mem.write(address, data):
            self.app.set_status(f"{self.label()}: write failed at 0x{address:X}", "red")
        else:
            self.app.set_status(f"{self.label()} held: writing {data.hex(' ').upper()} to 0x{address:X}", "green")
        self.after_id = self.app.root.after(HOLD_INTERVAL_MS, lambda: self.memory_hold(mem, address, data))

    def bios_repeat(self, mem, key, first=False):
        if not self.held:
            return
        if push_bios_key(mem, *key):
            self.app.set_status(f"{self.label()}: key {key[1]:02X} sent to the BIOS buffer", "green")
        else:
            self.app.set_status(f"{self.label()}: BIOS keyboard buffer is full", "orange")
        delay = TYPEMATIC_DELAY_MS if first else TYPEMATIC_RATE_MS
        self.after_id = self.app.root.after(delay, lambda: self.bios_repeat(mem, key))

    def set_look(self, pressed):
        if pressed:
            self.btn_press.config(relief=tk.SUNKEN, bg="#86efac", activebackground="#86efac", text="HELD")
        else:
            self.btn_press.config(relief=tk.RAISED, bg="#e5e7eb", activebackground="#e5e7eb", text="Press")


class KeyInjectorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Key Injector")
        self.root.geometry("880x600")
        self.root.minsize(840, 560)

        self.h_process = None
        self.mem = None
        self.events = queue.Queue()
        self.worker = None

        self.setup_style()
        self.setup_ui()
        self.refresh_pid_list()
        self.poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----- Layout -----

    def setup_style(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

    def setup_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        conn = ttk.Frame(main)
        conn.pack(fill=tk.X)
        ttk.Label(conn, text="PID:").pack(side=tk.LEFT)
        self.combo_pid = ttk.Combobox(conn, width=22)
        self.combo_pid.pack(side=tk.LEFT, padx=4)
        ttk.Button(conn, text="Refresh", width=8, command=self.refresh_pid_list).pack(side=tk.LEFT)
        ttk.Label(conn, text="Base (hex):").pack(side=tk.LEFT, padx=(12, 0))
        self.var_base = tk.StringVar()
        ttk.Entry(conn, textvariable=self.var_base, width=17).pack(side=tk.LEFT, padx=4)
        self.btn_find_base = ttk.Button(conn, text="Find Base", command=self.find_base)
        self.btn_find_base.pack(side=tk.LEFT)
        self.btn_attach = ttk.Button(conn, text="Attach", command=self.toggle_attach)
        self.btn_attach.pack(side=tk.LEFT, padx=6)

        pad = ttk.LabelFrame(main, text="Keypad", padding=8)
        pad.pack(fill=tk.BOTH, expand=True, pady=8)
        headers = ["#", "Name", "Mode", "Address", "Idle", "Pressed", "", ""]
        for col, text in enumerate(headers):
            ttk.Label(pad, text=text, font=("Segoe UI", 9, "bold")).grid(row=0, column=col, padx=2, sticky=tk.W)
        self.rows = [KeyRow(pad, i, self) for i in range(ROWS)]

        hint = ("Hold a Press button, or keys 1-0 while this window is focused.\n"
                "Memory: writes Pressed while held and Idle on release. Address: hex offset (11C48) or seg:off (1226:27D4).\n"
                "BIOS key: Pressed is a scan code (42 = F8) or ASCII + scan code (61 1E = a). See 'Key codes'.")
        ttk.Label(main, text=hint, foreground="gray", justify=tk.LEFT).pack(anchor=tk.W)

        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X, pady=(8, 0))
        self.lbl_status = ttk.Label(bottom, text="Ready", foreground="gray")
        self.lbl_status.pack(side=tk.LEFT)
        ttk.Button(bottom, text="Key codes", command=self.show_key_codes).pack(side=tk.RIGHT)
        ttk.Button(bottom, text="Load profile", command=self.load_profile).pack(side=tk.RIGHT, padx=4)
        ttk.Button(bottom, text="Save profile", command=self.save_profile).pack(side=tk.RIGHT)

        for i in range(ROWS):
            key = str((i + 1) % 10)
            self.root.bind(f"<KeyPress-{key}>", lambda e, idx=i: self.on_hotkey(e, idx, True))
            self.root.bind(f"<KeyRelease-{key}>", lambda e, idx=i: self.on_hotkey(e, idx, False))

    def on_hotkey(self, event, index, down):
        if isinstance(event.widget, (ttk.Entry, tk.Entry)):
            return  # typing a number into a field
        if down:
            self.rows[index].press()
        else:
            self.rows[index].release()

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

    def find_base(self):
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
            h = kernel32.OpenProcess(bf.PROCESS_QUERY_INFORMATION | bf.PROCESS_VM_READ, False, pid)
            if not h:
                self.events.put((None, f"Could not open PID {pid}"))
                return
            try:
                bases, _, _ = bf.scan_for_bases(h, lambda *a: None, threading.Event())
            finally:
                kernel32.CloseHandle(h)
            if len(bases) == 1:
                self.events.put((bases[0], None))
            else:
                self.events.put((None, f"{len(bases)} candidates, use the Base Finder" if bases
                                 else "no DOS memory found"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def poll_events(self):
        try:
            while True:
                base, error = self.events.get_nowait()
                self.worker = None
                self.btn_find_base.config(state=tk.NORMAL)
                if base is None:
                    self.set_status(f"Base not found: {error}", "red")
                else:
                    self.var_base.set(f"0x{base:X}")
                    self.set_status(f"Base found: 0x{base:X}. Press Attach.", "green")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def toggle_attach(self):
        if self.h_process:
            self.detach()
            return
        try:
            pid = self.get_selected_pid()
        except ValueError:
            self.set_status("Select a DOSBox process or enter a valid PID", "red")
            return
        try:
            base = int(self.var_base.get().strip(), 16)
        except ValueError:
            self.set_status("Enter the Base or press Find Base first", "red")
            return

        access = bf.PROCESS_QUERY_INFORMATION | bf.PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
        self.h_process = kernel32.OpenProcess(access, False, pid)
        if not self.h_process:
            self.set_status(f"Could not open PID {pid} (error {ctypes.get_last_error()})", "red")
            return
        self.mem = DosMemory(self.h_process, base)
        self.btn_attach.config(text="Detach")
        self.set_status(f"Attached to PID {pid}, Base 0x{base:X}", "green")

    def detach(self):
        for row in self.rows:
            row.release()
        if self.h_process:
            kernel32.CloseHandle(self.h_process)
        self.h_process = None
        self.mem = None
        self.btn_attach.config(text="Attach")
        self.set_status("Detached", "gray")

    def memory(self, quiet=False):
        if not self.mem and not quiet:
            self.set_status("Attach to DOSBox first", "red")
        return self.mem

    # ----- Profiles -----

    def save_profile(self):
        os.makedirs(PROFILE_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(title="Save profile", initialdir=PROFILE_DIR,
                                            defaultextension=".json", filetypes=[("Profiles", "*.json")])
        if not path:
            return
        data = {"keys": [row.to_dict() for row in self.rows]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.set_status(f"Profile saved: {os.path.basename(path)}", "green")

    def load_profile(self):
        path = filedialog.askopenfilename(title="Load profile",
                                          initialdir=PROFILE_DIR if os.path.isdir(PROFILE_DIR) else BASE_DIR,
                                          filetypes=[("Profiles", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            messagebox.showerror("Load failed", str(e))
            return
        keys = data.get("keys", [])
        for row, d in zip(self.rows, keys + [{}] * ROWS):
            row.from_dict(d)
        self.set_status(f"Profile loaded: {os.path.basename(path)}", "green")

    # ----- Misc -----

    def show_key_codes(self):
        win = tk.Toplevel(self.root)
        win.title("Key codes")
        win.resizable(False, False)
        tk.Label(win, text=KEY_CODES_HELP, font=("Consolas", 10), justify=tk.LEFT, padx=16, pady=12).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))

    def set_status(self, text, color="gray"):
        self.lbl_status.config(text=text, foreground=color)

    def on_close(self):
        self.detach()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    KeyInjectorApp(root)
    root.mainloop()