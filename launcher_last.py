"""
DOSBox Tools Launcher

A small control panel that starts each DOSBox utility in its own process.
The tools themselves are not changed; they only need to be in the same folder.
"""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox

try:
    import psutil
except ImportError:
    psutil = None  # DOSBox status pill disabled

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
RESULTS_DIR = os.path.join(BASE_DIR, "foundresults")

APP_TITLE = "DOSBox Tools"
APP_VERSION = "1.2"

FONT = "Segoe UI"
MONO = "Consolas"

THEME = {
    "bg": "#0f172a",
    "card": "#1e293b",
    "card_hover": "#243449",
    "border": "#334155",
    "title": "#f8fafc",
    "text": "#94a3b8",
    "muted": "#64748b",
    "status_bg": "#0b1220",
    "ok": "#4ade80",
    "warn": "#fbbf24",
    "error": "#f87171",
    "gradient_from": "#4f46e5",
    "gradient_to": "#0ea5e9",
}

# Shown as a full-width card above the grid: the other tools need the Base it finds
FEATURED_TOOL = {
    "file": "base_finder.py",
    "title": "Base Finder",
    "code": "BF",
    "tag": "START HERE",
    "accent": "#ec4899",
    "accent_dark": "#db2777",
    "description": "Finds where DOS memory starts inside the DOSBox process: the Base address the other "
                   "tools need. Press Auto Find Base, or search for a text from the game; "
                   "the Base is located and verified automatically.",
}

# Each tool gets its own accent color and a short code shown in its icon tile
TOOLS = [
    {
        "file": "live_hex_viewer.py",
        "title": "Live Hex Viewer",
        "code": "HX",
        "tag": "FIND VALUES",
        "accent": "#22c55e",
        "accent_dark": "#16a34a",
        "description": "Live hex dump of DOSBox memory. Changed bytes light up. "
                       "Click a byte to inspect it and copy its address for the trainer.",
    },
    {
        "file": "comparesnap.py",
        "title": "Snapshot Diff",
        "code": "DF",
        "tag": "FIND VALUES",
        "accent": "#f59e0b",
        "accent_dark": "#d97706",
        "description": "Captures DOS memory straight from DOSBox before and after an event and lists "
                       "every changed byte. Filter by value or change type, export to TXT or CSV.",
    },
    {
        "file": "memorytrainerautopid.py",
        "title": "Memory Trainer",
        "code": "TR",
        "tag": "CHANGE VALUES",
        "accent": "#ef4444",
        "accent_dark": "#dc2626",
        "description": "Writes bytes to Base + Offset addresses. Inject once or freeze a value. "
                       "Numpad 1-4 trigger the rows.",
    },
    {
        "file": "livepreview.py",
        "title": "Live Preview",
        "code": "LP",
        "tag": "VIEW SCREEN",
        "accent": "#3b82f6",
        "accent_dark": "#2563eb",
        "description": "Shows a live capture of the DOSBox window. Work in progress.",
    },
    {
        "file": "vgautilityautopid.py",
        "title": "VGA Live Viewer",
        "code": "VG",
        "tag": "VIEW VIDEO MEMORY",
        "accent": "#a855f7",
        "accent_dark": "#9333ea",
        "description": "Streams VGA mode 13h memory (320x200, 256 colors) directly from DOSBox. "
                       "Palette profiles, RGB gain and address navigation.",
    },
    {
        "file": "cgautilautopid.py",
        "title": "CGA Live Tuner",
        "code": "CG",
        "tag": "VIEW VIDEO MEMORY",
        "accent": "#06b6d4",
        "accent_dark": "#0891b2",
        "description": "Decodes CGA mode 4 memory (320x200, 4 colors) live, "
                       "with a palette editor and a flicker filter.",
    },
    {
        "file": "input_finder.py",
        "title": "Input Finder",
        "code": "IN",
        "tag": "FIND INPUT",
        "accent": "#84cc16",
        "accent_dark": "#65a30d",
        "description": "Finds where a game stores its key states. Learns the background noise, then counts "
                       "which bytes go back and forth exactly as often as you tap a key.",
    },
]

ABOUT_TEXT = f"""{APP_TITLE} {APP_VERSION}

A launcher for a set of Python tools that inspect and modify the memory
of a running DOSBox-X process.

Typical workflow
  0. Find the Base: run the Base Finder once per DOSBox session. It locates
     DOS memory inside the DOSBox process.
  1. Find a value: watch memory live in the Live Hex Viewer, or take two
     snapshots and compare them with Snapshot Diff.
  2. Change it: enter the address in the Memory Trainer and inject or
     freeze a new value.
  3. The VGA and CGA viewers decode video memory directly, which helps to
     understand how a game draws its screen.

The tools
  Base Finder        Finds and verifies the Base address of DOS memory.
  Live Hex Viewer    Live hex dump with change highlighting.
  Snapshot Diff      Byte-by-byte comparison of two memory dumps.
  Memory Trainer     Writes or freezes values in DOSBox memory.
  Live Preview       Live capture of the DOSBox window.
  VGA Live Viewer    Mode 13h (320x200, 256 colors) memory viewer.
  CGA Live Tuner     Mode 4 (320x200, 4 colors) viewer with flicker filter.
  Input Finder       Finds where a game stores its key states.

All tools detect the DOSBox process (PID) automatically.

Each tool runs as its own process, so closing or crashing one does not
affect the others. Error output is written to logs/<tool>.log.

Requirements: Windows, Python 3, numpy, opencv-python, pillow, mss, psutil
"""


# =====================================================================
# Helpers
# =====================================================================

def find_interpreter():
    """Prefers the project's venv, and pythonw.exe so that no console window opens."""
    venv_scripts = os.path.join(BASE_DIR, "venv", "Scripts")
    for name in ("pythonw.exe", "python.exe"):
        candidate = os.path.join(venv_scripts, name)
        if os.path.isfile(candidate):
            return candidate

    if os.name == "nt":
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.isfile(pythonw):
            return pythonw
    return sys.executable


def find_dosbox_processes():
    if psutil is None:
        return []
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info["name"] or ""
        if "dosbox" in name.lower():
            found.append((proc.info["pid"], name))
    return found


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


def blend(color_a, color_b, t):
    """Mixes two #rrggbb colors. t=0 gives color_a, t=1 gives color_b."""
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def rounded_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class FlatButton(tk.Label):
    """A colored button that looks the same on every Windows theme."""

    def __init__(self, parent, text, command, color, hover_color, fg="#ffffff", **kwargs):
        super().__init__(parent, text=text, bg=color, fg=fg, cursor="hand2",
                         font=(FONT, 9, "bold"), padx=16, pady=6, **kwargs)
        self.command = command
        self.color = color
        self.hover_color = hover_color
        self.enabled = True
        self.bind("<Enter>", lambda _e: self.enabled and self.config(bg=self.hover_color))
        self.bind("<Leave>", lambda _e: self.enabled and self.config(bg=self.color))
        self.bind("<Button-1>", lambda _e: self.enabled and self.command())

    def disable(self, text=None):
        self.enabled = False
        self.config(bg=THEME["border"], fg=THEME["muted"], cursor="arrow")
        if text:
            self.config(text=text)


# =====================================================================
# Widgets
# =====================================================================

class Header(tk.Canvas):
    """Gradient header with the app title and a live DOSBox status pill."""

    HEIGHT = 96

    def __init__(self, parent):
        super().__init__(parent, height=self.HEIGHT, highlightthickness=0, bg=THEME["bg"])
        self.status_text = "Checking DOSBox..."
        self.status_color = THEME["muted"]
        self.bind("<Configure>", lambda _e: self.redraw())

    def set_status(self, text, color):
        self.status_text = text
        self.status_color = color
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.HEIGHT
        if w <= 1:
            return

        steps = 120
        for i in range(steps):
            x1 = w * i / steps
            x2 = w * (i + 1) / steps + 1
            self.create_rectangle(x1, 0, x2, h, outline="",
                                  fill=blend(THEME["gradient_from"], THEME["gradient_to"], i / (steps - 1)))

        self.create_text(28, 36, text=APP_TITLE, anchor="w", fill="#ffffff", font=(FONT, 20, "bold"))
        self.create_text(30, 66, text="Memory and video tools for DOSBox-X", anchor="w",
                         fill="#e0e7ff", font=(FONT, 10))

        # Status pill on the right
        pill_text = self.create_text(0, 0, text=self.status_text, anchor="w", font=(FONT, 9, "bold"))
        tx1, _, tx2, _ = self.bbox(pill_text)
        text_w = tx2 - tx1
        pill_w = text_w + 44
        x2 = w - 28
        x1 = x2 - pill_w
        y1, y2 = h / 2 - 16, h / 2 + 16
        rounded_rect(self, x1, y1, x2, y2, 16, fill=THEME["bg"], outline="")
        self.create_oval(x1 + 14, h / 2 - 5, x1 + 24, h / 2 + 5, fill=self.status_color, outline="")
        self.coords(pill_text, x1 + 32, h / 2)
        self.itemconfigure(pill_text, fill=THEME["title"])
        self.tag_raise(pill_text)


class ToolCard(tk.Frame):
    """One tool: colored stripe, icon tile, title, description and an Open button."""

    def __init__(self, parent, tool, launcher):
        super().__init__(parent, bg=THEME["card"], highlightthickness=1, highlightbackground=THEME["border"])
        self.tool = tool
        self.launcher = launcher
        self.path = os.path.join(BASE_DIR, tool["file"])
        self.exists = os.path.isfile(self.path)
        accent = tool["accent"]

        tk.Frame(self, bg=accent, width=5).pack(side=tk.LEFT, fill=tk.Y)

        self.content = tk.Frame(self, bg=THEME["card"], padx=16, pady=14)
        self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Icon tile + title + tag
        top = tk.Frame(self.content, bg=THEME["card"])
        top.pack(fill=tk.X)

        tk.Label(top, text=tool["code"], bg=accent, fg="#ffffff", font=(MONO, 13, "bold"),
                 width=3, pady=8).pack(side=tk.LEFT)

        title_box = tk.Frame(top, bg=THEME["card"])
        title_box.pack(side=tk.LEFT, padx=12, fill=tk.X, expand=True)
        tk.Label(title_box, text=tool["title"], bg=THEME["card"], fg=THEME["title"],
                 font=(FONT, 12, "bold"), anchor="w").pack(fill=tk.X)
        tk.Label(title_box, text=tool["tag"], bg=THEME["card"], fg=accent,
                 font=(FONT, 8, "bold"), anchor="w").pack(fill=tk.X)

        # Description wraps to the card width
        desc = tk.Label(self.content, text=tool["description"], bg=THEME["card"], fg=THEME["text"],
                        font=(FONT, 9), justify=tk.LEFT, anchor="nw")
        desc.pack(fill=tk.BOTH, expand=True, pady=(12, 12))
        desc.bind("<Configure>", lambda e: desc.config(wraplength=max(100, e.width - 4)))

        # Footer
        footer = tk.Frame(self.content, bg=THEME["card"])
        footer.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Label(footer, text=tool["file"] if self.exists else f"{tool['file']}  (missing)",
                 bg=THEME["card"], fg=THEME["muted"] if self.exists else THEME["error"],
                 font=(MONO, 8)).pack(side=tk.LEFT)

        self.btn_open = FlatButton(footer, "Open", self.open, accent, tool["accent_dark"])
        self.btn_open.pack(side=tk.RIGHT)
        if not self.exists:
            self.btn_open.disable("Missing")

        self.lbl_badge = tk.Label(footer, text="", bg=THEME["card"], fg=THEME["ok"], font=(FONT, 8, "bold"))
        self.lbl_badge.pack(side=tk.RIGHT, padx=10)

        if self.exists:
            self.bind("<Enter>", lambda _e: self.config(highlightbackground=self.tool["accent"]), add="+")
            self.bind("<Leave>", self.on_leave, add="+")

    def on_leave(self, _event):
        # Moving onto a child widget also fires <Leave>; keep the highlight while the pointer is inside the card
        x, y = self.winfo_pointerxy()
        under = self.winfo_containing(x, y)
        if under is not None and str(under).startswith(str(self)):
            return
        self.config(highlightbackground=THEME["border"])

    def open(self):
        self.launcher.launch(self.tool)

    def set_running(self, count):
        if count == 0:
            self.lbl_badge.config(text="")
        elif count == 1:
            self.lbl_badge.config(text="● RUNNING")
        else:
            self.lbl_badge.config(text=f"● RUNNING x{count}")


# =====================================================================
# Main window
# =====================================================================

class LauncherApp:
    COLUMNS = 2

    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("880x860")
        self.root.minsize(760, 560)
        self.root.configure(bg=THEME["bg"])

        self.interpreter = find_interpreter()
        self.processes = []  # dicts: tool, proc, log, log_path
        self.cards = {}

        self.setup_menu()
        self.setup_ui()

        self.poll_processes()
        self.poll_dosbox()

    def setup_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open Tools Folder", command=lambda: open_folder(BASE_DIR))
        file_menu.add_command(label="Open Found Results", command=lambda: open_folder(RESULTS_DIR))
        file_menu.add_command(label="Open Logs", command=lambda: open_folder(LOG_DIR))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        for tool in [FEATURED_TOOL] + TOOLS:
            tools_menu.add_command(label=tool["title"], command=lambda t=tool: self.launch(t))
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def setup_ui(self):
        self.header = Header(self.root)
        self.header.pack(fill=tk.X)

        # Status bar (packed before the body so it always stays at the bottom)
        status = tk.Frame(self.root, bg=THEME["status_bg"], padx=16, pady=6)
        status.pack(fill=tk.X, side=tk.BOTTOM)
        self.lbl_status = tk.Label(status, text="Ready", bg=THEME["status_bg"], fg=THEME["muted"],
                                   font=(FONT, 9), anchor="w")
        self.lbl_status.pack(side=tk.LEFT)
        in_venv = os.path.join(BASE_DIR, "venv") in self.interpreter
        tk.Label(status, text=f"Python: {'venv' if in_venv else 'system'} ({os.path.basename(self.interpreter)})",
                 bg=THEME["status_bg"], fg=THEME["muted"], font=(MONO, 8)).pack(side=tk.RIGHT)

        # Scrollable body, so the cards still fit on small screens
        body_outer = tk.Frame(self.root, bg=THEME["bg"])
        body_outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(body_outer, bg=THEME["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(body_outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        grid = tk.Frame(canvas, bg=THEME["bg"], padx=24, pady=20)
        grid_window = canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(grid_window, width=e.width))
        self.root.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        for c in range(self.COLUMNS):
            grid.columnconfigure(c, weight=1, uniform="col")

        # Featured card spans the full width on the first row
        featured = ToolCard(grid, FEATURED_TOOL, self)
        featured.grid(row=0, column=0, columnspan=self.COLUMNS, sticky="nsew", padx=8, pady=8)
        self.cards[FEATURED_TOOL["file"]] = featured

        rows = (len(TOOLS) + self.COLUMNS - 1) // self.COLUMNS
        for r in range(1, rows + 1):
            # weight 0 keeps cards at their natural height (no clipped footers); uniform makes all rows equal
            grid.rowconfigure(r, weight=0, uniform="row")

        for i, tool in enumerate(TOOLS):
            card = ToolCard(grid, tool, self)
            r, c = divmod(i, self.COLUMNS)
            # A lone card on the last row spans the full width so the grid does not look lopsided
            span = self.COLUMNS if (i == len(TOOLS) - 1 and len(TOOLS) % self.COLUMNS) else 1
            card.grid(row=r + 1, column=c, columnspan=span, sticky="nsew", padx=8, pady=8)
            self.cards[tool["file"]] = card

    # ----- Launching -----

    def launch(self, tool):
        path = os.path.join(BASE_DIR, tool["file"])
        if not os.path.isfile(path):
            messagebox.showerror("File not found", f"Could not find:\n{path}")
            return

        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, os.path.splitext(tool["file"])[0] + ".log")

        try:
            log = open(log_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                [self.interpreter, path],
                cwd=BASE_DIR,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            messagebox.showerror("Launch failed", f"Could not start {tool['title']}:\n{e}")
            return

        self.processes.append({"tool": tool, "proc": proc, "log": log, "log_path": log_path})
        self.set_status(f"Started {tool['title']}", THEME["ok"])
        self.update_badges()

    def poll_processes(self):
        """Checks every second which tools are still running and reports the ones that failed."""
        still_running = []
        for entry in self.processes:
            code = entry["proc"].poll()
            if code is None:
                still_running.append(entry)
                continue

            entry["log"].close()
            if code != 0:
                rel = os.path.relpath(entry["log_path"], BASE_DIR)
                self.set_status(f"{entry['tool']['title']} exited with an error (code {code}). See {rel}",
                                THEME["error"])

        if len(still_running) != len(self.processes):
            self.processes = still_running
            self.update_badges()

        self.root.after(1000, self.poll_processes)

    def update_badges(self):
        counts = {}
        for entry in self.processes:
            counts[entry["tool"]["file"]] = counts.get(entry["tool"]["file"], 0) + 1
        for file, card in self.cards.items():
            card.set_running(counts.get(file, 0))

    # ----- DOSBox status -----

    def poll_dosbox(self):
        if psutil is None:
            self.header.set_status("psutil not installed", THEME["muted"])
        else:
            found = find_dosbox_processes()
            if not found:
                self.header.set_status("DOSBox not running", THEME["error"])
            elif len(found) == 1:
                pid, name = found[0]
                self.header.set_status(f"{name}  •  PID {pid}", THEME["ok"])
            else:
                self.header.set_status(f"{len(found)} DOSBox processes running", THEME["warn"])
        self.root.after(2000, self.poll_dosbox)

    # ----- Misc -----

    def set_status(self, text, color):
        self.lbl_status.config(text=text, fg=color)

    def show_about(self):
        win = tk.Toplevel(self.root)
        win.title(f"About {APP_TITLE}")
        win.configure(bg=THEME["card"])
        win.resizable(False, False)
        win.transient(self.root)

        tk.Frame(win, bg=THEME["gradient_from"], height=6).pack(fill=tk.X)
        tk.Label(win, text=ABOUT_TEXT, bg=THEME["card"], fg=THEME["title"], font=(MONO, 9),
                 justify=tk.LEFT, padx=24, pady=18).pack()
        btn_row = tk.Frame(win, bg=THEME["card"], pady=14)
        btn_row.pack(fill=tk.X)
        FlatButton(btn_row, "Close", win.destroy, THEME["gradient_from"], "#4338ca").pack()

        win.grab_set()
        win.focus_set()


if __name__ == "__main__":
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()