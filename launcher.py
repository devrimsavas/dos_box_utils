"""
DOSBox Tools Launcher

A small control panel that starts each DOSBox utility in its own process.
The tools themselves are not changed; they only need to be in the same folder.
"""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import psutil
except ImportError:
    psutil = None  # DOSBox status strip disabled

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
RESULTS_DIR = os.path.join(BASE_DIR, "foundresults")

APP_TITLE = "DOSBox Tools"
APP_VERSION = "1.0"

# Tools grouped by the step of the workflow they belong to
SECTIONS = [
    ("Find values", [
        {
            "file": "live_hex_viewer.py",
            "title": "Live Hex Viewer",
            "description": "Live hex dump of DOSBox memory. Changed bytes light up. "
                           "Click a byte to inspect it and copy its address for the trainer.",
        },
        {
            "file": "comparesnap.py",
            "title": "Snapshot Diff",
            "description": "Compares two memory snapshots and lists every changed byte. "
                           "Filter by old/new value or change type, export to TXT or CSV.",
        },
    ]),
    ("Change values", [
        {
            "file": "memorytrainerautopid.py",
            "title": "Memory Trainer",
            "description": "Writes bytes to Base + Offset addresses. Inject once or freeze a value. "
                           "Numpad 1-4 trigger the rows.",
        },
    ]),
    ("View video memory", [
        {
            "file": "vgautilityautopid.py",
            "title": "VGA Live Viewer",
            "description": "Streams VGA mode 13h memory (320x200, 256 colors) directly from DOSBox. "
                           "Palette profiles, RGB gain and address navigation.",
        },
        {
            "file": "cgautilautopid.py",
            "title": "CGA Live Tuner",
            "description": "Decodes CGA mode 4 memory (320x200, 4 colors) live, "
                           "with a palette editor and a flicker filter.",
        },
        {
            "file": "livepreview.py",
            "title": "Live Preview",
            "description": "Shows a live capture of the DOSBox window. Work in progress.",
        },
    ]),
]

ABOUT_TEXT = f"""{APP_TITLE} {APP_VERSION}

A launcher for a set of Python tools that inspect and modify the memory
of a running DOSBox-X process.

Typical workflow
  1. Find a value: watch memory live in the Live Hex Viewer, or take two
     snapshots and compare them with Snapshot Diff.
  2. Change it: enter the address in the Memory Trainer and inject or
     freeze a new value.
  3. The VGA and CGA viewers decode video memory directly, which helps to
     understand how a game draws its screen.

The tools
  Live Hex Viewer    Live hex dump with change highlighting.
  Snapshot Diff      Byte-by-byte comparison of two memory dumps.
  Memory Trainer     Writes or freezes values in DOSBox memory.
  VGA Live Viewer    Mode 13h (320x200, 256 colors) memory viewer.
  CGA Live Tuner     Mode 4 (320x200, 4 colors) viewer with flicker filter.
  Live Preview       Live capture of the DOSBox window.

All tools detect the DOSBox process (PID) automatically.

Each tool runs as its own process, so closing or crashing one does not
affect the others. Error output is written to logs/<tool>.log.

Requirements: Windows, Python 3, numpy, opencv-python, pillow, mss, psutil
"""

COLORS = {
    "bg": "#f3f4f6",
    "header_bg": "#111827",
    "header_fg": "#f9fafb",
    "header_sub": "#9ca3af",
    "card": "#ffffff",
    "card_border": "#e5e7eb",
    "card_hover": "#93c5fd",
    "title": "#111827",
    "text": "#4b5563",
    "muted": "#9ca3af",
    "section": "#6b7280",
    "ok": "#16a34a",
    "off": "#9ca3af",
    "error": "#dc2626",
}


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


class ToolCard:
    """One clickable card in the launcher."""

    def __init__(self, parent, tool, launcher):
        self.tool = tool
        self.launcher = launcher
        self.path = os.path.join(BASE_DIR, tool["file"])
        self.exists = os.path.isfile(self.path)

        self.frame = tk.Frame(parent, bg=COLORS["card"], highlightthickness=1,
                              highlightbackground=COLORS["card_border"], padx=14, pady=12)

        tk.Label(self.frame, text=tool["title"], bg=COLORS["card"], fg=COLORS["title"],
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(fill=tk.X)

        tk.Label(self.frame, text=tool["description"], bg=COLORS["card"], fg=COLORS["text"],
                 font=("Segoe UI", 9), wraplength=300, justify=tk.LEFT, anchor="w").pack(fill=tk.X, pady=(4, 10))

        bottom = tk.Frame(self.frame, bg=COLORS["card"])
        bottom.pack(fill=tk.X, side=tk.BOTTOM)

        file_text = tool["file"] if self.exists else f"{tool['file']} (not found)"
        tk.Label(bottom, text=file_text, bg=COLORS["card"],
                 fg=COLORS["muted"] if self.exists else COLORS["error"],
                 font=("Consolas", 8)).pack(side=tk.LEFT)

        self.btn_open = ttk.Button(bottom, text="Open", style="Accent.TButton", command=self.open,
                                   state=tk.NORMAL if self.exists else tk.DISABLED)
        self.btn_open.pack(side=tk.RIGHT)

        self.lbl_badge = tk.Label(bottom, text="", bg=COLORS["card"], fg=COLORS["ok"], font=("Segoe UI", 8, "bold"))
        self.lbl_badge.pack(side=tk.RIGHT, padx=8)

        if self.exists:
            self.frame.bind("<Enter>", lambda _e: self.frame.config(highlightbackground=COLORS["card_hover"]))
            self.frame.bind("<Leave>", lambda _e: self.frame.config(highlightbackground=COLORS["card_border"]))

    def open(self):
        self.launcher.launch(self.tool)

    def set_running(self, count):
        if count == 0:
            self.lbl_badge.config(text="")
        elif count == 1:
            self.lbl_badge.config(text="● Running")
        else:
            self.lbl_badge.config(text=f"● Running ({count})")


class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("760x660")
        self.root.minsize(700, 560)
        self.root.configure(bg=COLORS["bg"])

        self.interpreter = find_interpreter()
        self.processes = []  # list of dicts: tool, proc, log, log_path
        self.cards = {}

        self.setup_style()
        self.setup_menu()
        self.setup_ui()

        self.poll_processes()
        self.poll_dosbox()

    # ----- Layout -----

    def setup_style(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(14, 4),
                        foreground="#ffffff", background="#2563eb", borderwidth=0)
        style.map("Accent.TButton",
                  background=[("disabled", "#cbd5e1"), ("active", "#1d4ed8")],
                  foreground=[("disabled", "#f8fafc")])

    def setup_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open tools folder", command=lambda: open_folder(BASE_DIR))
        file_menu.add_command(label="Open found results", command=lambda: open_folder(RESULTS_DIR))
        file_menu.add_command(label="Open logs", command=lambda: open_folder(LOG_DIR))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)

    def setup_ui(self):
        # Header
        header = tk.Frame(self.root, bg=COLORS["header_bg"], padx=20, pady=14)
        header.pack(fill=tk.X)

        tk.Label(header, text=APP_TITLE, bg=COLORS["header_bg"], fg=COLORS["header_fg"],
                 font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(header, text="Memory and video tools for DOSBox-X", bg=COLORS["header_bg"],
                 fg=COLORS["header_sub"], font=("Segoe UI", 9)).pack(anchor="w")

        self.lbl_dosbox = tk.Label(header, text="", bg=COLORS["header_bg"], fg=COLORS["off"],
                                   font=("Segoe UI", 9, "bold"))
        self.lbl_dosbox.place(relx=1.0, rely=0.5, anchor="e")

        # Scrollable body
        body_outer = tk.Frame(self.root, bg=COLORS["bg"])
        body_outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(body_outer, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(body_outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        body = tk.Frame(canvas, bg=COLORS["bg"], padx=20, pady=10)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(body_window, width=e.width))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        for section_title, tools in SECTIONS:
            tk.Label(body, text=section_title.upper(), bg=COLORS["bg"], fg=COLORS["section"],
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(12, 6))

            grid = tk.Frame(body, bg=COLORS["bg"])
            grid.pack(fill=tk.X)
            grid.columnconfigure(0, weight=1, uniform="cards")
            grid.columnconfigure(1, weight=1, uniform="cards")

            for i, tool in enumerate(tools):
                card = ToolCard(grid, tool, self)
                card.frame.grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0 if i % 2 == 0 else 6, 0), pady=3)
                self.cards[tool["file"]] = card

        # Status bar
        status = tk.Frame(self.root, bg=COLORS["card"], highlightthickness=1,
                          highlightbackground=COLORS["card_border"], padx=12, pady=5)
        status.pack(fill=tk.X, side=tk.BOTTOM)
        self.lbl_status = tk.Label(status, text=f"Python: {self.interpreter}", bg=COLORS["card"],
                                   fg=COLORS["muted"], font=("Segoe UI", 8), anchor="w")
        self.lbl_status.pack(fill=tk.X)

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
        self.set_status(f"Started {tool['title']}", COLORS["ok"])
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
                                COLORS["error"])

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
            self.lbl_dosbox.config(text="psutil not installed", fg=COLORS["off"])
        else:
            found = find_dosbox_processes()
            if not found:
                self.lbl_dosbox.config(text="● DOSBox not running", fg=COLORS["off"])
            elif len(found) == 1:
                pid, name = found[0]
                self.lbl_dosbox.config(text=f"● {name} running (PID {pid})", fg="#4ade80")
            else:
                self.lbl_dosbox.config(text=f"● {len(found)} DOSBox processes running", fg="#fbbf24")
        self.root.after(2000, self.poll_dosbox)

    # ----- Misc -----

    def set_status(self, text, color):
        self.lbl_status.config(text=text, fg=color)

    def show_about(self):
        win = tk.Toplevel(self.root)
        win.title(f"About {APP_TITLE}")
        win.configure(bg=COLORS["card"])
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text=ABOUT_TEXT, bg=COLORS["card"], fg=COLORS["title"], font=("Consolas", 9),
                 justify=tk.LEFT, padx=20, pady=16).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 14))

        win.grab_set()
        win.focus_set()


if __name__ == "__main__":
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()