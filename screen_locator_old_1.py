"""
DOSBox-X Screen Locator

Finds where the picture you see in the DOSBox window lives in memory, together with its
video mode and line stride. Useful for games that scroll with hardware tricks or keep their
screen in an unusual place (Commander Keen and other EGA games).

How it works:
  1. The DOSBox window is captured and scaled back to the game resolution (e.g. 320x200).
  2. A few rows with lots of detail are picked. Their "fingerprint" is where neighbouring pixels
     change color. This does not depend on the palette, so the colors never need to be known.
  3. The whole DOSBox process is scanned for the same fingerprint, decoded both as 256-color
     chunky pixels (VGA 13h / Mode X) and as 16-color EGA planes (4 planes interleaved per
     address, as DOSBox stores them).
  4. For every hit the line stride is found from a second row, the full frame is decoded and
     compared with the screenshot. The match score is the share of pixels whose memory value
     maps consistently to the same screen color (checked in both directions).

Pause the game (or DOSBox-X: Alt+Pause) before capturing, so the screen and memory stay the same.
"""

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import mss
import numpy as np
from PIL import Image, ImageTk

import base_finder as bf

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixel coordinates on scaled displays
except Exception:
    pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

MODE_AUTO = "Auto (VGA 256 + EGA 16)"
MODE_VGA = "VGA 256 colors (chunky)"
MODE_EGA = "EGA 16 colors (planar)"
MODES = [MODE_AUTO, MODE_VGA, MODE_EGA]

SEGMENT = 64                # pixels per fingerprint segment (a multiple of 8, so EGA segments start on an address)
MIN_TRANSITIONS = 10        # a segment needs this many color changes...
MIN_REPEATS = 10            # ...and this many equal neighbours, so random data cannot match it
SEGMENTS_TO_TRY = 6
MAX_HITS_PER_SEGMENT = 60
CHUNK = 4 * 1024 * 1024
OVERLAP = 4096
PLANES = 4
GOOD_SCORE = 0.80
PREVIEW_SCALE = 1.5


# =====================================================================
# Window capture
# =====================================================================

def list_windows(pid):
    """Visible top-level windows of a process: list of (hwnd, title)."""
    found = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        win_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if win_pid.value != pid:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    # The game window first, the debugger last
    found.sort(key=lambda w: ("debug" in w[1].lower(), "dosbox" not in w[1].lower()))
    return found


def capture_client(hwnd):
    """Captures the client area (inside the frame and menu) of a window as an RGB array."""
    rect = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError("Could not read the window size")
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise OSError("The window is minimized")
    with mss.mss() as sct:
        shot = sct.grab({"left": pt.x, "top": pt.y, "width": width, "height": height})
    return np.frombuffer(shot.rgb, dtype=np.uint8).reshape((shot.height, shot.width, 3))


def downscale(rgb, width, height):
    """Samples the center of every game pixel, undoing DOSBox's scaling and aspect correction."""
    h, w = rgb.shape[:2]
    ys = ((np.arange(height) + 0.5) * h / height).astype(int)
    xs = ((np.arange(width) + 0.5) * w / width).astype(int)
    return rgb[ys][:, xs]


def color_labels(rgb):
    """Turns an RGB image into an array of color ids (same color = same id)."""
    keys = (rgb[..., 0].astype(np.int64) << 16) | (rgb[..., 1].astype(np.int64) << 8) | rgb[..., 2]
    _, labels = np.unique(keys, return_inverse=True)
    return labels.reshape(rgb.shape[:2])


# =====================================================================
# Fingerprints
# =====================================================================

def transitions(values):
    """1 where a pixel differs from the next one, else 0."""
    return (values[:-1] != values[1:]).astype(np.uint8)


def pick_segments(labels, count=SEGMENTS_TO_TRY, length=SEGMENT):
    """Picks detailed row segments (x0 a multiple of 8) from different parts of the screen."""
    height, width = labels.shape
    if width < length:
        return []
    diff = (labels[:, :-1] != labels[:, 1:]).astype(np.int32)
    csum = np.concatenate([np.zeros((height, 1), np.int32), np.cumsum(diff, axis=1)], axis=1)
    starts = np.arange(0, width - length + 1, 8)
    changes = csum[:, starts + length - 1] - csum[:, starts]
    repeats = (length - 1) - changes
    # The most useful segments have both color changes and runs of equal pixels
    quality = np.minimum(changes, repeats)

    order = np.argsort(quality, axis=None)[::-1]
    chosen = []
    for flat in order:
        y, i = divmod(int(flat), len(starts))
        if changes[y, i] < MIN_TRANSITIONS or repeats[y, i] < MIN_REPEATS:
            break
        if all(abs(y - cy) >= max(3, height // 20) for cy, _ in chosen):
            chosen.append((y, int(starts[i])))
        if len(chosen) >= count:
            break
    return chosen


def partner_row(labels, y0, x0, length=SEGMENT):
    """A nearby row whose segment at x0 has enough detail to measure the stride."""
    height = labels.shape[0]
    for d in (1, 2, 3, 4, 5, 6, 7, 8, -1, -2, -3, -4, -5, -6, -7, -8):
        y = y0 + d
        if 0 <= y < height and int(transitions(labels[y, x0:x0 + length]).sum()) >= 6:
            return y
    return None


# =====================================================================
# Memory decoding
# =====================================================================

def ega_pixels(raw, npix):
    """EGA pixels from interleaved plane bytes (4 bytes = 8 pixels, leftmost pixel in the top bit)."""
    n = len(raw) // PLANES
    planes = np.frombuffer(raw[:n * PLANES], dtype=np.uint8).reshape((n, PLANES))
    pixels = np.zeros(n * 8, dtype=np.uint8)
    for p in range(PLANES):
        pixels |= np.unpackbits(planes[:, p]) << p
    return pixels[:npix]


# Memory is addressed in "pixel units" so both modes work the same way:
#   VGA 256: pixel index = byte address (1 byte per pixel)
#   EGA 16:  pixel index = address / 4 * 8 + bit (8 pixels per 4 interleaved plane bytes)
# EGA games that scroll smoothly (Commander Keen) start the screen in the middle of a byte
# ("pixel panning"), so a pixel index does not have to be a multiple of 8.

def line_pixels(mode, stride):
    """Pixels per line in memory. For EGA the stride counts addresses (bytes per plane)."""
    return stride * 8 if mode == MODE_EGA else stride


def pixel_to_address(mode, pixel):
    """Byte address of a pixel index, plus the pixel offset inside that address (EGA panning)."""
    if mode == MODE_EGA:
        return pixel // 8 * PLANES, pixel % 8
    return pixel, 0


def read_pixels(read, mode, pixel, count):
    """Reads `count` pixels starting at a pixel index. Returns a uint8 array or None."""
    if count <= 0:
        return None
    if mode == MODE_EGA:
        addr, skip = pixel // 8 * PLANES, pixel % 8
        size = (skip + count + 7) // 8 * PLANES
        raw = read(addr, size)
        if not raw or len(raw) < size:
            return None
        return ega_pixels(raw, skip + count)[skip:]
    raw = read(pixel, count)
    if not raw or len(raw) < count:
        return None
    return np.frombuffer(raw, dtype=np.uint8)


def frame_from_pixels(pixels, width, height, line_px):
    """Cuts a (height, width) frame out of a pixel run that starts at the top-left pixel."""
    index = np.arange(height)[:, None] * line_px + np.arange(width)[None, :]
    return pixels[index]


def values_explain_screen(values, labels):
    """
    True if every memory value in the segment always shows the same screen color.
    Two memory values may share a color (palettes can repeat colors), but not the other way round.
    """
    values = np.asarray(values, dtype=np.int64)
    labels = np.asarray(labels, dtype=np.int64)
    if len(np.unique(labels)) < 2:
        return False
    return len(np.unique(values * 4096 + labels)) == len(np.unique(values))


def match_score(frame, labels):
    """
    Share of pixels whose memory value always shows the same screen color.
    1.0 = memory and screen show the same picture (in any palette). The reverse direction is
    checked loosely, because two palette entries may have the same color.
    """
    a = frame.ravel().astype(np.int64)
    b = labels.ravel().astype(np.int64)
    if len(np.unique(a)) < 2:
        return 0.0  # a blank memory area matches nothing
    base = int(b.max()) + 1
    uniq, counts = np.unique(a * base + b, return_counts=True)

    def consistency(keys):
        best = {}
        for k, c in zip(keys.tolist(), counts.tolist()):
            best[k] = max(best.get(k, 0), c)
        return sum(best.values()) / len(a)

    memory_to_screen = consistency(uniq // base)
    screen_to_memory = consistency(uniq % base)
    return memory_to_screen if screen_to_memory >= 0.6 else min(memory_to_screen, screen_to_memory)


def preview_colors(frame, rgb):
    """Colors the memory frame with the screen color each memory value appears with most."""
    lut = np.zeros((256, 3), dtype=np.uint8)
    flat_f = frame.ravel()
    flat_rgb = rgb.reshape(-1, 3)
    for v in np.unique(flat_f):
        sel = flat_rgb[flat_f == v]
        keys, counts = np.unique(sel, axis=0, return_counts=True)
        lut[v] = keys[np.argmax(counts)]
    return lut[frame]


# =====================================================================
# Search
# =====================================================================

class Search:
    """Scans the DOSBox process for the screen. Runs in a background thread."""

    def __init__(self, h_process, labels, rgb, modes, report, stop_event):
        self.h = h_process
        self.labels = labels
        self.rgb = rgb
        self.modes = modes
        self.report = report
        self.stop = stop_event
        self.height, self.width = labels.shape
        self.best_score = 0.0

    def read(self, addr, size):
        return bf.read_bytes(self.h, addr, size)

    def run(self):
        segments = pick_segments(self.labels)
        if not segments:
            return [], "The screen has too little detail. Capture a busier screen."

        hits = []   # (pixel index of screen pixel (x0, y0), mode, y0, x0)
        scanned = 0
        regions = list(bf.iter_readable_regions(self.h))
        total = sum(size for _, size in regions) * len(self.modes)
        patterns = [(y, x, transitions(self.labels[y, x:x + SEGMENT]).tobytes(), self.labels[y, x:x + SEGMENT])
                    for y, x in segments]

        for mode in self.modes:
            for region_addr, region_size in regions:
                pos = 0
                while pos < region_size:
                    if self.stop.is_set():
                        return [], "Stopped."
                    step = min(CHUNK, region_size - pos)
                    raw = self.read(region_addr + pos, min(step + OVERLAP, region_size - pos))
                    if raw:
                        self.find_in_chunk(raw, region_addr + pos, step, mode, patterns, hits)
                    pos += step
                    scanned += step
                    self.report(("progress", scanned / max(total, 1), len(hits)))

        self.report(("status", f"Verifying {len(hits)} candidates..."))
        return self.verify(hits), None

    def find_in_chunk(self, raw, chunk_addr, step, mode, patterns, hits):
        if mode == MODE_EGA:
            # Region and chunk addresses are page aligned, so plane groups start on a multiple of 4
            pixels = ega_pixels(raw, len(raw) // PLANES * 8)
            first_pixel, limit = chunk_addr // PLANES * 8, step // PLANES * 8
        else:
            pixels = np.frombuffer(raw, dtype=np.uint8)
            first_pixel, limit = chunk_addr, step
        stream = transitions(pixels).tobytes()

        for y0, x0, pattern, seg_labels in patterns:
            found = 0
            start = stream.find(pattern)
            while start != -1 and found < MAX_HITS_PER_SEGMENT:
                # The neighbour pattern matched; now check that the colors agree as well
                if start < limit and values_explain_screen(pixels[start:start + SEGMENT], seg_labels):
                    hits.append((first_pixel + start, mode, y0, x0))
                    found += 1
                start = stream.find(pattern, start + 1)

    def verify(self, hits):
        results = {}
        read = self.read
        for pixel, mode, y0, x0 in hits:
            if self.stop.is_set():
                break
            y1 = partner_row(self.labels, y0, x0)
            if y1 is None:
                continue
            want = transitions(self.labels[y1, x0:x0 + SEGMENT])
            lo, hi = (self.width // 8, 1024) if mode == MODE_EGA else (self.width, 4096)
            d = y1 - y0

            # One read covers the partner row for every stride we try
            near, far = pixel + d * line_pixels(mode, lo), pixel + d * line_pixels(mode, hi)
            first = min(near, far)
            block = read_pixels(read, mode, first, abs(far - near) + SEGMENT)
            if block is None:
                continue

            for stride in range(lo, hi + 1):
                line_px = line_pixels(mode, stride)
                at = pixel + d * line_px - first
                seg = block[at:at + SEGMENT]
                if len(seg) < SEGMENT or not np.array_equal(transitions(seg), want):
                    continue
                origin = pixel - y0 * line_px - x0
                key = (origin, mode, stride)
                if key in results:
                    continue
                px = read_pixels(read, mode, origin, (self.height - 1) * line_px + self.width)
                if px is None:
                    continue
                frame = frame_from_pixels(px, self.width, self.height, line_px)
                score = match_score(frame, self.labels)
                self.best_score = max(self.best_score, score)
                if score >= GOOD_SCORE:
                    addr, pan = pixel_to_address(mode, origin)
                    results[key] = {"addr": addr, "pan": pan, "mode": mode, "stride": stride,
                                    "score": score, "frame": frame}

        return sorted(results.values(), key=lambda r: -r["score"])


# =====================================================================
# GUI
# =====================================================================

class ScreenLocatorApp:
    COLUMNS = [
        ("addr", "Address", 170, tk.W),
        ("mode", "Mode", 190, tk.W),
        ("stride", "Stride", 70, tk.CENTER),
        ("pan", "Pan", 50, tk.CENTER),
        ("score", "Match", 70, tk.CENTER),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Screen Locator")
        self.root.geometry("1040x840")
        self.root.minsize(1000, 760)

        self.h_process = None
        self.screen_rgb = None
        self.labels = None
        self.results = []
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.preview_images = []

        self.setup_style()
        self.setup_ui()
        self.refresh_pids()
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

        top = ttk.Frame(main)
        top.pack(fill=tk.X)
        ttk.Label(top, text="PID:").pack(side=tk.LEFT)
        self.combo_pid = ttk.Combobox(top, width=22)
        self.combo_pid.pack(side=tk.LEFT, padx=4)
        self.combo_pid.bind("<<ComboboxSelected>>", lambda _e: self.refresh_windows())
        ttk.Button(top, text="Refresh", width=8, command=self.refresh_pids).pack(side=tk.LEFT)
        ttk.Label(top, text="Window:").pack(side=tk.LEFT, padx=(12, 0))
        self.combo_window = ttk.Combobox(top, width=38, state="readonly")
        self.combo_window.pack(side=tk.LEFT, padx=4)

        opts = ttk.Frame(main)
        opts.pack(fill=tk.X, pady=8)
        ttk.Label(opts, text="Mode:").pack(side=tk.LEFT)
        self.var_mode = tk.StringVar(value=MODE_AUTO)
        ttk.Combobox(opts, textvariable=self.var_mode, values=MODES, state="readonly",
                     width=26).pack(side=tk.LEFT, padx=4)
        ttk.Label(opts, text="Resolution:").pack(side=tk.LEFT, padx=(12, 0))
        self.var_w = tk.IntVar(value=320)
        self.var_h = tk.IntVar(value=200)
        ttk.Spinbox(opts, textvariable=self.var_w, from_=64, to=1024, increment=8, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Label(opts, text="x").pack(side=tk.LEFT)
        ttk.Spinbox(opts, textvariable=self.var_h, from_=64, to=768, increment=1, width=6).pack(side=tk.LEFT, padx=2)

        steps = ttk.Frame(main)
        steps.pack(fill=tk.X, pady=(0, 6))
        self.btn_capture = ttk.Button(steps, text="1. Capture screen", command=self.capture)
        self.btn_capture.pack(side=tk.LEFT)
        self.btn_load = ttk.Button(steps, text="or Load screenshot...", command=self.load_screenshot)
        self.btn_load.pack(side=tk.LEFT, padx=4)
        self.btn_search = ttk.Button(steps, text="2. Search memory", command=self.start_search, state=tk.DISABLED)
        self.btn_search.pack(side=tk.LEFT, padx=(16, 4))
        self.btn_stop = ttk.Button(steps, text="Stop", command=self.stop_event.set, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        ttk.Label(main, text="Pause the game first (in DOSBox-X: Alt+Pause). The picture must not change between "
                             "capture and search.\nMost exact: take a DOSBox-X screenshot (Capture menu, or Ctrl+F5) "
                             "and load it; it has the game's real pixels.",
                  foreground="gray", justify=tk.LEFT).pack(anchor=tk.W)

        # Previews
        previews = ttk.Frame(main)
        previews.pack(fill=tk.X, pady=8)
        pw, ph = int(320 * PREVIEW_SCALE), int(200 * PREVIEW_SCALE)
        left = ttk.LabelFrame(previews, text="Screen (captured)", padding=4)
        left.pack(side=tk.LEFT, padx=(0, 8))
        self.canvas_screen = tk.Canvas(left, width=pw, height=ph, bg="black", highlightthickness=0)
        self.canvas_screen.pack()
        right = ttk.LabelFrame(previews, text="Memory (selected result)", padding=4)
        right.pack(side=tk.LEFT)
        self.canvas_memory = tk.Canvas(right, width=pw, height=ph, bg="black", highlightthickness=0)
        self.canvas_memory.pack()

        # Results
        table = ttk.Frame(main)
        table.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS], show="headings", selectmode="browse")
        for col_id, heading, width, anchor in self.COLUMNS:
            self.tree.heading(col_id, text=heading)
            self.tree.column(col_id, width=width, anchor=anchor)
        vsb = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_selected())
        self.tree.bind("<Double-1>", lambda _e: self.copy_address())

        bottom = ttk.Frame(main)
        bottom.pack(fill=tk.X, pady=(8, 0))
        self.progress = ttk.Progressbar(bottom, length=200, mode="determinate")
        self.progress.pack(side=tk.LEFT)
        self.lbl_status = ttk.Label(bottom, text="Ready", foreground="gray")
        self.lbl_status.pack(side=tk.LEFT, padx=10)
        ttk.Button(bottom, text="Copy address", command=self.copy_address).pack(side=tk.RIGHT)

    # ----- Process and window -----

    def refresh_pids(self):
        processes = bf.find_dosbox_processes()
        items = [f"{pid} - {name}" for pid, name in processes]
        self.combo_pid["values"] = items
        self.combo_pid.set(items[0] if items else "")
        if not items:
            self.set_status("DOSBox is not running" if bf.psutil else "psutil not installed, enter PID manually",
                            "red")
        self.refresh_windows()

    def get_pid(self):
        return int(self.combo_pid.get().strip().split(" - ")[0].strip())

    def refresh_windows(self):
        try:
            windows = list_windows(self.get_pid())
        except ValueError:
            windows = []
        self.windows = windows
        self.combo_window["values"] = [title or f"(untitled {hwnd})" for hwnd, title in windows]
        if windows:
            self.combo_window.current(0)

    # ----- Steps -----

    def capture(self):
        if not getattr(self, "windows", None):
            self.refresh_windows()
        index = self.combo_window.current()
        if index < 0 or not self.windows:
            self.set_status("No DOSBox window found", "red")
            return
        try:
            width, height = int(self.var_w.get()), int(self.var_h.get())
            full = capture_client(self.windows[index][0])
        except (OSError, tk.TclError, ValueError) as e:
            self.set_status(f"Capture failed: {e}", "red")
            return

        self.set_screen(downscale(full, width, height),
                        f"Captured {full.shape[1]}x{full.shape[0]} -> {width}x{height}")

    def load_screenshot(self):
        """Loads a screenshot made by DOSBox-X itself: exact game pixels, no window scaling."""
        path = filedialog.askopenfilename(title="Load DOSBox-X screenshot",
                                          filetypes=[("Images", "*.png *.bmp"), ("All files", "*.*")])
        if not path:
            return
        try:
            rgb = np.array(Image.open(path).convert("RGB"))
        except OSError as e:
            self.set_status(f"Could not open the image: {e}", "red")
            return
        height, width = rgb.shape[:2]
        self.var_w.set(width)
        self.var_h.set(height)
        self.set_screen(rgb, f"Loaded {width}x{height} screenshot")

    def set_screen(self, rgb, what):
        self.screen_rgb = rgb
        self.labels = color_labels(rgb)
        self.show_image(self.canvas_screen, rgb, slot=0)
        self.canvas_memory.delete("all")
        colors = int(self.labels.max()) + 1
        segments = len(pick_segments(self.labels))
        note = "" if segments else " Too little detail here, try a busier screen."
        self.set_status(f"{what}, {colors} colors, {segments} usable rows.{note} Now press 'Search memory'.",
                        "green" if segments else "orange")
        self.btn_search.config(state=tk.NORMAL)

    def start_search(self):
        if self.labels is None or (self.worker and self.worker.is_alive()):
            return
        try:
            pid = self.get_pid()
        except ValueError:
            self.set_status("Select a DOSBox process", "red")
            return
        if self.h_process:
            bf.kernel32.CloseHandle(self.h_process)
        self.h_process = bf.kernel32.OpenProcess(bf.PROCESS_QUERY_INFORMATION | bf.PROCESS_VM_READ, False, pid)
        if not self.h_process:
            self.set_status(f"Could not open PID {pid}", "red")
            return

        mode = self.var_mode.get()
        modes = [MODE_VGA, MODE_EGA] if mode == MODE_AUTO else [mode]
        self.stop_event.clear()
        self.tree.delete(*self.tree.get_children())
        self.results = []
        self.btn_search.config(state=tk.DISABLED)
        self.btn_capture.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.progress["value"] = 0

        search = Search(self.h_process, self.labels, self.screen_rgb, modes, self.events.put, self.stop_event)

        def work():
            try:
                results, error = search.run()
            except Exception as e:  # keep the GUI alive whatever happens in the scan
                results, error = [], f"Search failed: {e}"
            self.events.put(("done", results, error, search.best_score))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    _, fraction, hits = event
                    self.progress["value"] = fraction * 100
                    self.set_status(f"Scanning... {fraction * 100:.0f}%  ({hits} fingerprint hits)", "blue")
                elif event[0] == "status":
                    self.set_status(event[1], "blue")
                elif event[0] == "done":
                    self.on_done(event[1], event[2], event[3])
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def on_done(self, results, error, best_score=0.0):
        self.worker = None
        self.btn_search.config(state=tk.NORMAL)
        self.btn_capture.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.progress["value"] = 100
        self.results = results
        for i, r in enumerate(results):
            self.tree.insert("", tk.END, iid=str(i), values=(
                f"0x{r['addr']:X}", r["mode"], r["stride"], r["pan"], f"{r['score'] * 100:.1f}%"))
        if error:
            self.set_status(error, "red")
        elif not results:
            near = f" Closest candidate matched {best_score * 100:.0f}%." if best_score > 0 else ""
            self.set_status(f"Not found.{near} Was the game paused? Is the resolution right? "
                            "Try a DOSBox-X screenshot.", "orange")
        else:
            best = results[0]
            self.set_status(f"Found {len(results)} match(es). Best: 0x{best['addr']:X}, {best['mode']}, "
                            f"stride {best['stride']}", "green")
            self.tree.selection_set("0")

    # ----- Results -----

    def selected(self):
        sel = self.tree.selection()
        return self.results[int(sel[0])] if sel else None

    def show_selected(self):
        r = self.selected()
        if r is None or self.screen_rgb is None:
            return
        self.show_image(self.canvas_memory, preview_colors(r["frame"], self.screen_rgb), slot=1)

    def show_image(self, canvas, rgb, slot):
        w, h = int(canvas["width"]), int(canvas["height"])
        img = Image.fromarray(rgb).resize((w, h), Image.NEAREST)
        photo = ImageTk.PhotoImage(img)
        while len(self.preview_images) <= slot:
            self.preview_images.append(None)
        self.preview_images[slot] = photo  # keep a reference or Tkinter drops the image
        canvas.delete("all")
        canvas.create_image(0, 0, anchor=tk.NW, image=photo)

    def copy_address(self):
        r = self.selected()
        if r is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(f"0x{r['addr']:X}")
        mode = "EGA 16 colors (planar)" if r["mode"] == MODE_EGA else "VGA 256 colors (13h / Mode X)"
        pan = f" (the picture starts {r['pan']} pixels into the first byte)" if r["pan"] else ""
        self.set_status(f"Copied 0x{r['addr']:X}. In the VGA viewer use Mode '{mode}', stride {r['stride']}{pan}.",
                        "green")

    def set_status(self, text, color="gray"):
        self.lbl_status.config(text=text, foreground=color)


if __name__ == "__main__":
    root = tk.Tk()
    ScreenLocatorApp(root)
    root.mainloop()