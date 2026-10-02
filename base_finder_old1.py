"""
DOSBox-X Base Finder

Finds where DOS memory starts inside the DOSBox process: the "Base" address the other tools need.

Three ways to find it:
  * Auto Find Base: scans the process for the start of DOS memory directly. No pattern needed.
  * Search a text or byte pattern and leave "Location in DOS" empty: for each hit, the tool looks
    backwards for the start of DOS memory and reports where the hit sits inside DOS.
  * Search a pattern and enter its DOS location (segment:offset from the DOSBox-X debugger):
    the Base is calculated directly.

The start of DOS memory is recognised by its fingerprint: the interrupt vector table at 0000:0000
(most vectors point to the same BIOS default handler in ROM) followed by the BIOS data area, where
the word at 0040:0013 holds the conventional memory size (640 KB).
"""

import ctypes
import ctypes.wintypes as wt
import queue
import re
import threading
import tkinter as tk
from collections import Counter
from tkinter import ttk

try:
    import psutil
except ImportError:
    psutil = None  # Auto PID detection disabled; manual PID entry still works

# ---------------------------------------------------------------------
# Windows API
# ---------------------------------------------------------------------

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
MAX_ADDRESS = 0x7FFFFFFFFFFF


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                    ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

# ---------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------

CHUNK_SIZE = 4 * 1024 * 1024    # bytes read per call while scanning
MAX_HITS = 500
MAX_AUTO_LOCATE = 50             # hits for which the Base is searched automatically
LOOKBACK = 32 * 1024 * 1024      # how far back from a hit the start of DOS memory is searched
PREVIEW_BYTES = 32

# DOS memory fingerprint
BIOS_MEMSIZE_OFFSET = 0x413          # 0040:0013, conventional memory size in KB
BIOS_MEMSIZE_EXPECTED = 640
IVT_SIZE = 0x400                     # 256 vectors x 4 bytes (offset, segment)
MIN_SAME_SEGMENT = 100               # vectors that must point into the same segment
ROM_SEGMENT_START = 0xC000           # that segment must be in ROM (C000-FFFF)
FINGERPRINT_LEN = 0x420
MEMSIZE_SIGNATURE = re.compile(b"(?=\x80\x02)")  # 640 as a little-endian word

MODE_HEX = "hex"
MODE_TEXT = "text"


# =====================================================================
# Core logic (no GUI)
# =====================================================================

def find_dosbox_processes():
    """Returns a list of (pid, name) for every running process whose name contains 'dosbox'."""
    if psutil is None:
        return []
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info["name"] or ""
        if "dosbox" in name.lower():
            found.append((proc.info["pid"], name))
    return found


def parse_hex_pattern(text):
    """
    Parses a hex byte pattern such as '0A 27 23 08', '0A272308' or '0A ?? 23 08'.
    '??' is a wildcard that matches any byte. Returns a list of ints (None = wildcard).
    """
    compact = re.sub(r"\s+", "", text).upper()
    if compact.startswith("0X"):
        compact = compact[2:]
    if not compact:
        raise ValueError("Pattern is empty")
    if len(compact) % 2:
        raise ValueError("Hex pattern must have an even number of digits")

    result = []
    for i in range(0, len(compact), 2):
        pair = compact[i:i + 2]
        if pair == "??":
            result.append(None)
        else:
            try:
                result.append(int(pair, 16))
            except ValueError:
                raise ValueError(f"'{pair}' is not a hex byte")
    if all(b is None for b in result):
        raise ValueError("Pattern cannot be only wildcards")
    return result


def build_regex(pattern, ignore_case=False):
    """Builds a bytes regex that also finds overlapping matches."""
    body = b"".join(b"." if b is None else re.escape(bytes([b])) for b in pattern)
    flags = re.DOTALL | (re.IGNORECASE if ignore_case else 0)
    return re.compile(b"(?=(" + body + b"))", flags)


def parse_dos_location(text):
    """
    Parses where the pattern sits in DOS memory.
    '0823:0010' -> segment * 16 + offset, '8240' or '0x8240' -> linear hex address. Empty -> None.
    """
    text = text.strip()
    if not text:
        return None
    if ":" in text:
        seg, off = text.split(":", 1)
        return int(seg.strip(), 16) * 16 + int(off.strip(), 16)
    return int(text, 16)


def format_dos_address(linear):
    """Linear DOS address as both linear hex and segment:offset (segment = linear // 16)."""
    if linear < 0x100000:
        return f"0x{linear:05X} ({linear >> 4:04X}:{linear & 0xF:04X})"
    return f"0x{linear:X}"


def iter_readable_regions(h_process):
    """Yields (address, size) for every committed, readable memory region of the process."""
    mbi = MEMORY_BASIC_INFORMATION()
    address = 0
    while address < MAX_ADDRESS:
        if not kernel32.VirtualQueryEx(h_process, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize
        if size == 0:
            break
        readable = mbi.Protect not in (0, PAGE_NOACCESS) and not (mbi.Protect & PAGE_GUARD)
        if mbi.State == MEM_COMMIT and readable:
            yield base, size
        address = base + size


def read_bytes(h_process, address, size):
    """Reads `size` bytes or returns None if the memory cannot be read."""
    buf = (ctypes.c_char * size)()
    read = ctypes.c_size_t()
    if kernel32.ReadProcessMemory(h_process, ctypes.c_void_p(address), buf, size, ctypes.byref(read)):
        return buf.raw[:read.value]
    return None


# ----- DOS memory fingerprint -----

def looks_like_dos_start(block):
    """
    True if `block` (bytes starting at a candidate Base) looks like the start of DOS memory:
    the BIOS data area reports 640 KB, and most interrupt vectors share one ROM segment
    (the BIOS default handler that DOSBox installs for every unused vector).
    """
    if len(block) < BIOS_MEMSIZE_OFFSET + 2:
        return False
    if block[BIOS_MEMSIZE_OFFSET] | (block[BIOS_MEMSIZE_OFFSET + 1] << 8) != BIOS_MEMSIZE_EXPECTED:
        return False
    # Quick reject using only the high bytes of the segments (most candidates fail here)
    high_bytes = block[3:IVT_SIZE:4]
    if not any(high_bytes.count(x) >= MIN_SAME_SEGMENT for x in set(high_bytes) if x >= ROM_SEGMENT_START >> 8):
        return False
    segments = [block[i] | (block[i + 1] << 8) for i in range(2, IVT_SIZE, 4)]
    segment, count = Counter(segments).most_common(1)[0]
    return count >= MIN_SAME_SEGMENT and segment >= ROM_SEGMENT_START


def fingerprint_ok(h_process, base):
    data = read_bytes(h_process, base, FINGERPRINT_LEN)
    return bool(data) and looks_like_dos_start(data)


def find_bases_in_range(h_process, start, length, stop_event, report=None):
    """Returns every address in [start, start + length) that looks like the start of DOS memory."""
    bases = []
    pos = 0
    while pos < length:
        if stop_event.is_set():
            break
        step = min(CHUNK_SIZE, length - pos)
        read_len = min(step + FINGERPRINT_LEN, length - pos)  # overlap so candidates near the edge are complete
        data = read_bytes(h_process, start + pos, read_len)
        if data:
            for m in MEMSIZE_SIGNATURE.finditer(data):
                candidate = m.start() - BIOS_MEMSIZE_OFFSET
                if candidate < 0:
                    continue  # already checked by the previous chunk
                if candidate >= step:
                    break  # belongs to the next chunk
                if looks_like_dos_start(data[candidate:candidate + FINGERPRINT_LEN]):
                    bases.append(start + pos + candidate)
        pos += step
        if report:
            report(step)
    return bases


def locate_base_for_hit(h_process, hit, region_addr, stop_event):
    """Looks backwards from a hit for the nearest start of DOS memory in the same memory region."""
    start = max(region_addr, hit - LOOKBACK)
    bases = find_bases_in_range(h_process, start, hit - start + 1, stop_event)
    return max(bases) if bases else None


def scan_for_bases(h_process, report, stop_event):
    """Scans every readable region for the start of DOS memory."""
    bases = []
    scanned = 0
    for region_addr, region_size in iter_readable_regions(h_process):
        def progress(n):
            nonlocal scanned
            scanned += n
            report(scanned, len(bases))

        bases.extend(find_bases_in_range(h_process, region_addr, region_size, stop_event, progress))
        if stop_event.is_set():
            return bases, scanned, True
    return bases, scanned, False


# ----- Pattern search -----

def scan_process(h_process, regex, pattern_len, report, stop_event):
    """
    Searches every readable region in chunks. Chunks overlap by pattern_len - 1 bytes
    so matches across chunk borders are not missed.
    Returns (hits, scanned, cancelled) where hits are (address, region_address) pairs.
    """
    hits = []
    scanned = 0
    for region_addr, region_size in iter_readable_regions(h_process):
        pos = 0
        while pos < region_size:
            if stop_event.is_set():
                return hits, scanned, True

            step = min(CHUNK_SIZE, region_size - pos)
            read_len = min(step + pattern_len - 1, region_size - pos)
            data = read_bytes(h_process, region_addr + pos, read_len)
            if data:
                for m in regex.finditer(data):
                    if m.start() >= step:
                        break  # belongs to the next chunk
                    hits.append((region_addr + pos + m.start(), region_addr))
                    if len(hits) >= MAX_HITS:
                        return hits, scanned + step, False

            pos += step
            scanned += step
            report(scanned, len(hits))
    return hits, scanned, False


# =====================================================================
# GUI
# =====================================================================

class BaseFinderApp:
    COLUMNS = [
        ("address", "Found at (process address)", 180),
        ("dos", "In DOS", 170),
        ("base", "Calculated Base", 160),
        ("check", "Check", 110),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Base Finder")
        self.root.geometry("820x700")
        self.root.minsize(720, 580)

        self.h_process = None
        self.worker = None
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.results = []  # dicts: address, base, ok, method

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

    def setup_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Process
        conn = ttk.Frame(main)
        conn.pack(fill=tk.X)
        ttk.Label(conn, text="DOSBox PID:").pack(side=tk.LEFT)
        self.combo_pid = ttk.Combobox(conn, width=28)
        self.combo_pid.pack(side=tk.LEFT, padx=4)
        ttk.Button(conn, text="Refresh", width=8, command=self.refresh_pid_list).pack(side=tk.LEFT)

        # Search options
        search = ttk.LabelFrame(main, text="Search", padding=8)
        search.pack(fill=tk.X, pady=8)
        search.columnconfigure(1, weight=1)

        self.var_mode = tk.StringVar(value=MODE_TEXT)
        mode_row = ttk.Frame(search)
        mode_row.grid(row=0, column=0, columnspan=3, sticky=tk.W)
        ttk.Radiobutton(mode_row, text="Text", value=MODE_TEXT, variable=self.var_mode,
                        command=self.on_mode_change).pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="Hex bytes", value=MODE_HEX, variable=self.var_mode,
                        command=self.on_mode_change).pack(side=tk.LEFT, padx=12)
        self.var_case = tk.BooleanVar(value=False)
        self.chk_case = ttk.Checkbutton(mode_row, text="Match case", variable=self.var_case)
        self.chk_case.pack(side=tk.LEFT, padx=12)

        ttk.Label(search, text="Pattern:").grid(row=1, column=0, sticky=tk.W, pady=(8, 2))
        self.var_pattern = tk.StringVar()
        e_pattern = ttk.Entry(search, textvariable=self.var_pattern, font=("Consolas", 10))
        e_pattern.grid(row=1, column=1, columnspan=2, sticky=tk.EW, padx=(6, 0), pady=(8, 2))

        ttk.Label(search, text="Location in DOS:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.var_location = tk.StringVar()
        e_loc = ttk.Entry(search, textvariable=self.var_location, width=18, font=("Consolas", 10))
        e_loc.grid(row=2, column=1, sticky=tk.W, padx=(6, 0), pady=2)
        ttk.Label(search, text="leave empty to find the Base automatically",
                  foreground="gray").grid(row=2, column=2, sticky=tk.W, padx=8)

        for e in (e_pattern, e_loc):
            e.bind("<Return>", lambda _e: self.start_search())

        hint = (
            "Text or hex search: leave 'Location in DOS' empty and the Base is found automatically.\n"
            "If you know where the pattern is in DOS (segment:offset from the DOSBox-X debugger), enter it.\n"
            "No pattern at all? Press 'Auto Find Base' to scan for the start of DOS memory directly."
        )
        ttk.Label(search, text=hint, foreground="gray", justify=tk.LEFT).grid(
            row=3, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

        btns = ttk.Frame(search)
        btns.grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(10, 0))
        self.btn_search = ttk.Button(btns, text="Search", command=self.start_search)
        self.btn_search.pack(side=tk.LEFT)
        self.btn_auto = ttk.Button(btns, text="Auto Find Base", command=self.start_auto_find)
        self.btn_auto.pack(side=tk.LEFT, padx=6)
        self.btn_stop = ttk.Button(btns, text="Stop", command=self.stop_search, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)
        self.lbl_progress = ttk.Label(btns, text="")
        self.lbl_progress.pack(side=tk.LEFT, padx=10)

        # Results
        table = ttk.Frame(main)
        table.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings",
                                 selectmode="browse")
        for col_id, heading, width in self.COLUMNS:
            self.tree.heading(col_id, text=heading)
            self.tree.column(col_id, width=width, anchor=tk.W)
        self.tree.tag_configure("ok", foreground="#15803d")
        self.tree.tag_configure("bad", foreground="#9ca3af")
        vsb = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.update_preview())
        self.tree.bind("<Double-1>", lambda _e: self.copy_base())

        # Preview and actions
        self.lbl_preview = ttk.Label(main, text="Select a result to preview the bytes there.",
                                     font=("Consolas", 9))
        self.lbl_preview.pack(fill=tk.X, pady=(8, 0))

        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X, pady=(6, 0))
        self.lbl_status = ttk.Label(bottom, text="Ready", foreground="gray")
        self.lbl_status.pack(side=tk.LEFT)
        ttk.Button(bottom, text="Copy Base", command=self.copy_base).pack(side=tk.RIGHT)
        ttk.Button(bottom, text="Copy Address", command=self.copy_address).pack(side=tk.RIGHT, padx=6)

        self.on_mode_change()

    # ----- Process selection -----

    def refresh_pid_list(self):
        if psutil is None:
            self.combo_pid["values"] = []
            self.set_status("psutil not installed, enter PID manually", "orange")
            return

        processes = find_dosbox_processes()
        items = [f"{pid} - {name}" for pid, name in processes]
        self.combo_pid["values"] = items
        if len(items) == 1:
            self.combo_pid.set(items[0])
            self.set_status(f"Found {processes[0][1]} (PID {processes[0][0]})", "blue")
        elif len(items) > 1:
            self.combo_pid.set(items[0])
            self.set_status(f"{len(items)} DOSBox processes found, choose one", "orange")
        else:
            self.combo_pid.set("")
            self.set_status("DOSBox is not running", "red")

    def get_selected_pid(self):
        return int(self.combo_pid.get().strip().split(" - ")[0].strip())

    def open_process(self):
        """Opens the selected DOSBox process. Returns False and shows why if it fails."""
        try:
            pid = self.get_selected_pid()
        except ValueError:
            self.set_status("Select a DOSBox process or enter a valid PID", "red")
            return False

        self.close_process()
        self.h_process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h_process:
            self.set_status(f"Could not open PID {pid} (error {ctypes.get_last_error()})", "red")
            return False
        return True

    # ----- Search -----

    def on_mode_change(self):
        self.chk_case.config(state=tk.NORMAL if self.var_mode.get() == MODE_TEXT else tk.DISABLED)

    def build_pattern(self):
        text = self.var_pattern.get()
        if self.var_mode.get() == MODE_HEX:
            pattern = parse_hex_pattern(text)
            return pattern, build_regex(pattern)
        if not text:
            raise ValueError("Pattern is empty")
        pattern = list(text.encode("latin-1"))
        return pattern, build_regex(pattern, ignore_case=not self.var_case.get())

    def begin_work(self, message):
        self.tree.delete(*self.tree.get_children())
        self.results = []
        self.stop_event.clear()
        self.btn_search.config(state=tk.DISABLED)
        self.btn_auto.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.lbl_progress.config(text="")
        self.set_status(message, "blue")

    def report_progress(self, scanned, count, label):
        self.events.put(("progress", f"{scanned / (1024 * 1024):,.0f} MB scanned, {count} {label}"))

    def start_search(self):
        if self.worker and self.worker.is_alive():
            return

        try:
            pattern, regex = self.build_pattern()
        except (ValueError, UnicodeEncodeError) as e:
            self.set_status(f"Invalid pattern: {e}", "red")
            return

        try:
            location = parse_dos_location(self.var_location.get())
        except ValueError:
            self.set_status("Invalid location. Use segment:offset (0823:0010) or linear hex", "red")
            return

        if not self.open_process():
            return

        self.begin_work("Searching...")
        h = self.h_process

        def work():
            hits, scanned, cancelled = scan_process(
                h, regex, len(pattern), lambda s, c: self.report_progress(s, c, "hits"), self.stop_event)

            results = []
            for i, (address, region_addr) in enumerate(hits):
                if location is not None:
                    base = address - location
                    ok = base >= 0 and fingerprint_ok(h, base)
                    results.append({"address": address, "base": base, "ok": ok, "method": "manual"})
                elif i < MAX_AUTO_LOCATE and not self.stop_event.is_set():
                    self.events.put(("progress", f"Locating Base for hit {i + 1} of {min(len(hits), MAX_AUTO_LOCATE)}..."))
                    base = locate_base_for_hit(h, address, region_addr, self.stop_event)
                    results.append({"address": address, "base": base, "ok": base is not None, "method": "auto"})
                else:
                    results.append({"address": address, "base": None, "ok": False, "method": "skipped"})

            self.events.put(("search_done", results, scanned, cancelled or self.stop_event.is_set()))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def start_auto_find(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.open_process():
            return

        self.begin_work("Scanning for the start of DOS memory...")
        h = self.h_process

        def work():
            bases, scanned, cancelled = scan_for_bases(
                h, lambda s, c: self.report_progress(s, c, "found"), self.stop_event)
            results = [{"address": b, "base": b, "ok": True, "method": "auto"} for b in bases]
            self.events.put(("auto_done", results, scanned, cancelled))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop_search(self):
        self.stop_event.set()

    def poll_events(self):
        """Handles messages from the worker thread on the GUI thread."""
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    self.lbl_progress.config(text=event[1])
                elif event[0] in ("search_done", "auto_done"):
                    self.on_work_done(event[0], *event[1:])
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def on_work_done(self, kind, results, scanned, cancelled):
        self.btn_search.config(state=tk.NORMAL)
        self.btn_auto.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.lbl_progress.config(text=f"{scanned / (1024 * 1024):,.0f} MB scanned")

        # Verified results first, then by address
        self.results = sorted(results, key=lambda r: (not r["ok"], r["address"]))
        for i, r in enumerate(self.results):
            self.tree.insert("", tk.END, iid=str(i), values=self.row_values(r),
                             tags=("ok",) if r["ok"] else ("bad",))

        verified = sorted({r["base"] for r in self.results if r["ok"]})
        prefix = "Stopped. " if cancelled else ""

        if kind == "auto_done":
            if len(verified) == 1:
                self.set_status(prefix + f"Base found: 0x{verified[0]:X}  (double-click to copy)", "green")
            elif verified:
                self.set_status(prefix + f"{len(verified)} candidates found. Use a text search to pick the right one.",
                                "orange")
            else:
                self.set_status(prefix + "No DOS memory found. Is a program running in DOSBox?", "red")
        elif not results:
            self.set_status(prefix + "No matches. Try a different pattern.", "red")
        elif len(results) >= MAX_HITS:
            self.set_status(prefix + f"Stopped at {MAX_HITS} hits. Use a longer pattern.", "orange")
        elif len(verified) == 1:
            self.set_status(prefix + f"Base found: 0x{verified[0]:X}  (double-click to copy)", "green")
        elif len(verified) > 1:
            self.set_status(prefix + f"{len(verified)} different bases. Use a longer pattern to narrow it down.",
                            "orange")
        else:
            self.set_status(prefix + f"{len(results)} hits, but no Base could be verified.", "orange")

        if self.results:
            self.tree.selection_set("0")
            self.tree.see("0")

    @staticmethod
    def row_values(r):
        address = f"0x{r['address']:X}"
        if r["base"] is None:
            check = "✖ not found" if r["method"] == "auto" else "-"
            return address, "-", "-", check
        dos = format_dos_address(r["address"] - r["base"]) if r["address"] >= r["base"] else "-"
        check = "✔ verified" if r["ok"] else "✖ no match"
        return address, dos, f"0x{r['base']:X}", check

    # ----- Results -----

    def selected_result(self):
        sel = self.tree.selection()
        if not sel:
            return None
        index = int(sel[0])
        return self.results[index] if index < len(self.results) else None

    def update_preview(self):
        r = self.selected_result()
        if not r or not self.h_process:
            return
        data = read_bytes(self.h_process, r["address"], PREVIEW_BYTES)
        if not data:
            self.lbl_preview.config(text="(memory no longer readable)")
            return
        hex_text = " ".join(f"{b:02X}" for b in data[:16])
        ascii_text = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:16])
        self.lbl_preview.config(text=f"{hex_text}   {ascii_text}")

    def copy_text(self, text, what):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status(f"Copied {what}: {text}", "green")

    def copy_base(self):
        r = self.selected_result()
        if not r:
            return
        if r["base"] is None:
            self.set_status("No Base for this row.", "orange")
            return
        self.copy_text(f"0x{r['base']:X}", "Base")

    def copy_address(self):
        r = self.selected_result()
        if r:
            self.copy_text(f"0x{r['address']:X}", "address")

    # ----- Misc -----

    def close_process(self):
        if self.h_process:
            kernel32.CloseHandle(self.h_process)
            self.h_process = None

    def set_status(self, text, color="gray"):
        self.lbl_status.config(text=text, foreground=color)


if __name__ == "__main__":
    root = tk.Tk()
    BaseFinderApp(root)
    root.mainloop()