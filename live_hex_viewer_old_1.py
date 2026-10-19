"""
DOSBox-X Live Hex Viewer

Shows a region of DOSBox memory as a hex dump that refreshes continuously.
Bytes that change are highlighted so you can spot game values while playing.
"""

import ctypes
import ctypes.wintypes as wt
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

try:
    import psutil
except ImportError:
    psutil = None  # Auto PID detection disabled; manual PID entry still works

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wt.HANDLE

BYTES_PER_ROW = 16
HOT_SECONDS = 0.6    # bright highlight right after a change
WARM_SECONDS = 3.0   # softer highlight until the change fades out
REFRESH_OPTIONS_MS = ["50", "100", "250", "500", "1000"]

# Column layout of one row: "00000000   00 01 .. 07  08 .. 0F   ................"
OFFSET_DIGITS = 8
HEX_COL = OFFSET_DIGITS + 3
HEX_WIDTH = BYTES_PER_ROW * 3       # "XX " per byte, minus the trailing space, plus the extra gap after byte 7
ASCII_COL = HEX_COL + HEX_WIDTH + 3

HEADER = (
    "Offset".ljust(HEX_COL)
    + " ".join(f"{j:02X}" for j in range(8))
    + "  "
    + " ".join(f"{j:02X}" for j in range(8, 16))
    + "   ASCII"
)

COLORS = {
    "bg": "#1e1e1e",
    "fg": "#d4d4d4",
    "header": "#8a8a8a",
    "offset": "#6cb6ff",
    "ascii": "#9a9a9a",
    "hot_fg": "#ffffff",
    "hot_bg": "#c42b1c",
    "warm_fg": "#ffb86c",
    "unreadable": "#5a5a5a",
    "selected_bg": "#264f78",
}


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


def parse_hex(text):
    """Parses hex input such as '1A', '0x1A' or '0x1000 + 0x20'. Empty input returns 0."""
    total = 0
    for part in text.strip().replace(" ", "").split("+"):
        if part:
            total += int(part, 16)
    return total


def parse_offset(text):
    """
    Parses a DOS offset. Accepts plain hex ('11BA0', '0x11BA0') or segment:offset as shown
    by the DOSBox-X debugger ('11A9:0110' or '11A9:00000110'), which is segment * 16 + offset.
    """
    text = text.strip().replace(" ", "")
    if ":" in text:
        segment, offset = text.split(":", 1)
        return int(segment, 16) * 16 + int(offset, 16)
    return parse_hex(text)


def format_seg_off(linear):
    """Linear DOS address as segment:offset, or None above 1 MB."""
    if linear >= 0x100000:
        return None
    return f"{linear >> 4:04X}:{linear & 0xF:04X}"


def hex_col_of(byte_index):
    """Text column where the hex digits of a byte (0-15) start."""
    return HEX_COL + byte_index * 3 + (1 if byte_index >= 8 else 0)


def byte_at_column(col):
    """Maps a text column back to a byte index (0-15), or None if the column is not on a byte."""
    for j in range(BYTES_PER_ROW):
        start = hex_col_of(j)
        if start <= col < start + 2:
            return j
    if ASCII_COL <= col < ASCII_COL + BYTES_PER_ROW:
        return col - ASCII_COL
    return None


class LiveHexViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Live Hex Viewer")
        self.root.geometry("780x760")
        self.root.minsize(700, 400)

        self.h_process = None
        self.after_id = None
        self.paused = False

        self.base = 0
        self.view_offset = 0
        self.visible_rows = 32
        self.selected_offset = None

        # Change tracking for the bytes currently on screen
        self.prev_data = None
        self.changed_at = []

        self.text_font = tkfont.Font(family="Consolas", size=10)
        self.line_height = self.text_font.metrics("linespace")

        self.setup_ui()
        self.setup_keybindings()
        self.reset_tracking()
        self.refresh_pid_list()
        self.render_message("Not attached. Select a DOSBox process and press Attach.")

    # ----- Layout -----

    def setup_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # Connection row
        conn = ttk.Frame(main)
        conn.pack(fill=tk.X)

        ttk.Label(conn, text="PID:").pack(side=tk.LEFT)
        self.combo_pid = ttk.Combobox(conn, width=24)
        self.combo_pid.pack(side=tk.LEFT, padx=4)
        ttk.Button(conn, text="Refresh", width=8, command=self.refresh_pid_list).pack(side=tk.LEFT, padx=2)
        self.btn_attach = ttk.Button(conn, text="Attach", width=10, command=self.toggle_attach)
        self.btn_attach.pack(side=tk.LEFT, padx=4)

        self.btn_pause = ttk.Button(conn, text="Pause", width=8, command=self.toggle_pause)
        self.btn_pause.pack(side=tk.RIGHT, padx=2)
        self.var_refresh = tk.StringVar(value="100")
        ttk.Combobox(conn, textvariable=self.var_refresh, values=REFRESH_OPTIONS_MS,
                     state="readonly", width=5).pack(side=tk.RIGHT, padx=2)
        ttk.Label(conn, text="Refresh (ms):").pack(side=tk.RIGHT)

        # Address row
        addr = ttk.Frame(main)
        addr.pack(fill=tk.X, pady=6)

        self.var_base = tk.StringVar(value="0x0")
        self.var_offset = tk.StringVar(value="0x0")

        ttk.Label(addr, text="Base (hex):").pack(side=tk.LEFT)
        e_base = ttk.Entry(addr, textvariable=self.var_base, width=18)
        e_base.pack(side=tk.LEFT, padx=4)
        ttk.Label(addr, text="Offset (hex or seg:off):").pack(side=tk.LEFT, padx=(10, 0))
        e_off = ttk.Entry(addr, textvariable=self.var_offset, width=14)
        e_off.pack(side=tk.LEFT, padx=4)
        ttk.Button(addr, text="Go", width=6, command=self.go_to_address).pack(side=tk.LEFT, padx=4)
        for e in (e_base, e_off):
            e.bind("<Return>", lambda _e: self.go_to_address())

        ttk.Label(addr, text="Up/Down: row   PgUp/PgDn: page   Wheel: 3 rows",
                  foreground="gray").pack(side=tk.RIGHT)

        # Hex dump
        self.text = tk.Text(
            main,
            font=self.text_font,
            bg=COLORS["bg"],
            fg=COLORS["fg"],
            insertwidth=0,
            wrap=tk.NONE,
            width=80,
            height=34,
            padx=6,
            pady=4,
            cursor="arrow",
            relief=tk.FLAT,
            takefocus=0,
        )
        self.text.pack(fill=tk.BOTH, expand=True)

        self.text.tag_configure("header", foreground=COLORS["header"])
        self.text.tag_configure("offset", foreground=COLORS["offset"])
        self.text.tag_configure("ascii", foreground=COLORS["ascii"])
        self.text.tag_configure("unreadable", foreground=COLORS["unreadable"])
        self.text.tag_configure("warm", foreground=COLORS["warm_fg"])
        self.text.tag_configure("hot", foreground=COLORS["hot_fg"], background=COLORS["hot_bg"])
        self.text.tag_configure("selected", background=COLORS["selected_bg"])  # configured last = highest priority
        self.text.config(state=tk.DISABLED)

        self.text.bind("<Button-1>", self.on_click)
        self.text.bind("<Configure>", self.on_text_resize)
        self.text.bind("<MouseWheel>", self.on_mouse_wheel)
        self.text.bind("<Button-4>", lambda _e: self.scroll_rows(-3) or "break")
        self.text.bind("<Button-5>", lambda _e: self.scroll_rows(3) or "break")

        # Selected byte info
        info = ttk.Frame(main)
        info.pack(fill=tk.X, pady=(6, 0))
        self.lbl_info = ttk.Label(info, text="Click a byte to inspect it.", font=("Consolas", 9))
        self.lbl_info.pack(side=tk.LEFT)
        ttk.Button(info, text="Copy for Trainer", command=self.copy_selected_address).pack(side=tk.RIGHT)

        # Status bar
        self.lbl_status = ttk.Label(main, text="Ready", foreground="gray")
        self.lbl_status.pack(fill=tk.X, pady=(4, 0))

    def setup_keybindings(self):
        self.root.bind("<Up>", lambda e: self.on_nav_key(e, -1))
        self.root.bind("<Down>", lambda e: self.on_nav_key(e, 1))
        self.root.bind("<Prior>", lambda e: self.on_nav_key(e, -self.visible_rows))
        self.root.bind("<Next>", lambda e: self.on_nav_key(e, self.visible_rows))

    # ----- Process selection -----

    def refresh_pid_list(self):
        """Fills the PID dropdown with running DOSBox processes. Selects it automatically if only one is found."""
        if self.h_process:
            return  # do not change the selection while attached

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
        """Accepts either a dropdown item ('12345 - dosbox-x.exe') or a plain typed number."""
        text = self.combo_pid.get().strip()
        return int(text.split(" - ")[0].strip())

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
            self.base = parse_hex(self.var_base.get())
            self.view_offset = parse_offset(self.var_offset.get())
        except ValueError:
            self.set_status("Invalid base or offset (hex expected)", "red")
            return

        self.h_process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.h_process:
            self.set_status(f"Could not open PID {pid} (error {ctypes.get_last_error()})", "red")
            return

        self.btn_attach.config(text="Detach")
        self.set_status(f"Attached to PID {pid}", "green")
        self.reset_tracking()
        self.loop()

    def detach(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.h_process:
            kernel32.CloseHandle(self.h_process)
            self.h_process = None
        self.btn_attach.config(text="Attach")
        self.set_status("Detached", "gray")
        self.render_message("Not attached. Select a DOSBox process and press Attach.")

    # ----- Memory access -----

    def read_memory(self, address, size):
        """
        Reads `size` bytes. If the whole block cannot be read (for example it crosses into an
        unmapped page), falls back to row-by-row reads. Unreadable bytes are returned as None.
        """
        buf = (ctypes.c_char * size)()
        n = ctypes.c_size_t()
        if kernel32.ReadProcessMemory(self.h_process, ctypes.c_void_p(address), buf, size, ctypes.byref(n)):
            return list(buf.raw)

        result = []
        row_buf = (ctypes.c_char * BYTES_PER_ROW)()
        for start in range(0, size, BYTES_PER_ROW):
            ok = kernel32.ReadProcessMemory(self.h_process, ctypes.c_void_p(address + start),
                                            row_buf, BYTES_PER_ROW, ctypes.byref(n))
            result.extend(list(row_buf.raw) if ok else [None] * BYTES_PER_ROW)
        return result[:size]

    # ----- Update loop -----

    def loop(self):
        if not self.h_process:
            return
        if not self.paused:
            self.update_view()
        try:
            interval = int(self.var_refresh.get())
        except ValueError:
            interval = 100
        self.after_id = self.root.after(interval, self.loop)

    def update_view(self):
        if not self.h_process:
            return
        size = self.visible_rows * BYTES_PER_ROW
        data = self.read_memory(self.base + self.view_offset, size)

        now = time.monotonic()
        if self.prev_data is not None and len(self.prev_data) == len(data):
            for i, (old, new) in enumerate(zip(self.prev_data, data)):
                if old != new and old is not None and new is not None:
                    self.changed_at[i] = now
        self.prev_data = data

        self.render(data, now)
        self.update_info()

    def reset_tracking(self):
        """Forgets previous values, e.g. after scrolling, so moving the view is not shown as changes."""
        self.prev_data = None
        self.changed_at = [float("-inf")] * (self.visible_rows * BYTES_PER_ROW)

    # ----- Rendering -----

    def render_message(self, message):
        self.text.config(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", HEADER + "\n\n" + message)
        self.text.tag_add("header", "1.0", "1.end")
        self.text.config(state=tk.DISABLED)

    def render(self, data, now):
        lines = [HEADER]
        for r in range(self.visible_rows):
            chunk = data[r * BYTES_PER_ROW:(r + 1) * BYTES_PER_ROW]
            hex_parts = ["??" if b is None else f"{b:02X}" for b in chunk]
            hex_str = " ".join(hex_parts[:8]) + "  " + " ".join(hex_parts[8:])
            ascii_str = "".join("?" if b is None else (chr(b) if 32 <= b < 127 else ".") for b in chunk)
            offset = self.view_offset + r * BYTES_PER_ROW
            lines.append(f"{offset:0{OFFSET_DIGITS}X}   {hex_str}   {ascii_str}")

        t = self.text
        t.config(state=tk.NORMAL)
        t.delete("1.0", tk.END)
        t.insert("1.0", "\n".join(lines))
        t.tag_add("header", "1.0", "1.end")

        for r in range(self.visible_rows):
            line = r + 2  # line 1 is the header
            t.tag_add("offset", f"{line}.0", f"{line}.{OFFSET_DIGITS}")
            t.tag_add("ascii", f"{line}.{ASCII_COL}", f"{line}.end")

            for j in range(BYTES_PER_ROW):
                i = r * BYTES_PER_ROW + j
                value = data[i] if i < len(data) else None

                if value is None:
                    self.tag_byte(line, j, "unreadable")
                else:
                    age = now - self.changed_at[i]
                    if age < HOT_SECONDS:
                        self.tag_byte(line, j, "hot")
                    elif age < WARM_SECONDS:
                        self.tag_byte(line, j, "warm")

                if self.selected_offset == self.view_offset + i:
                    self.tag_byte(line, j, "selected")

        t.config(state=tk.DISABLED)

    def tag_byte(self, line, byte_index, tag):
        """Applies a tag to both the hex digits and the ASCII character of one byte."""
        hc = hex_col_of(byte_index)
        ac = ASCII_COL + byte_index
        self.text.tag_add(tag, f"{line}.{hc}", f"{line}.{hc + 2}")
        self.text.tag_add(tag, f"{line}.{ac}", f"{line}.{ac + 1}")

    # ----- Selected byte -----

    def on_click(self, event):
        index = self.text.index(f"@{event.x},{event.y}")
        line, col = (int(x) for x in index.split("."))
        row = line - 2
        byte_index = byte_at_column(col)
        if row < 0 or row >= self.visible_rows or byte_index is None:
            return "break"

        self.selected_offset = self.view_offset + row * BYTES_PER_ROW + byte_index
        if self.h_process:
            self.update_view()
        return "break"

    def update_info(self):
        if self.selected_offset is None:
            self.lbl_info.config(text="Click a byte to inspect it.")
            return

        address = self.base + self.selected_offset
        raw = self.read_memory(address, 4)
        if any(b is None for b in raw):
            self.lbl_info.config(text=f"Offset 0x{self.selected_offset:X}  |  unreadable")
            return

        byte = raw[0]
        word = raw[0] | (raw[1] << 8)
        dword = word | (raw[2] << 16) | (raw[3] << 24)
        seg_off = format_seg_off(self.selected_offset)
        where = f"0x{self.selected_offset:X}" + (f" ({seg_off})" if seg_off else "")
        self.lbl_info.config(
            text=(f"Offset {where}  |  Byte {byte:02X} ({byte})  |  "
                  f"Word {word:04X} ({word})  |  Dword {dword:08X} ({dword})")
        )

    def copy_selected_address(self):
        if self.selected_offset is None:
            self.set_status("No byte selected", "red")
            return
        text = f"0x{self.base:X} + 0x{self.selected_offset:X}"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status(f"Copied '{text}' to clipboard", "green")

    # ----- Navigation -----

    def go_to_address(self):
        typed = self.var_offset.get().strip()
        try:
            self.base = parse_hex(self.var_base.get())
            offset = parse_offset(typed)
        except ValueError:
            self.set_status("Invalid base or offset (hex, or segment:offset like 11A9:0110)", "red")
            return
        self.set_view_offset(offset)
        if ":" in typed:
            self.set_status(f"{typed} = 0x{offset:X}", "green")

    def set_view_offset(self, offset):
        self.view_offset = max(0, offset)
        self.var_offset.set(f"0x{self.view_offset:X}")
        self.reset_tracking()
        if self.h_process:
            self.update_view()

    def scroll_rows(self, rows):
        self.set_view_offset(self.view_offset + rows * BYTES_PER_ROW)

    def on_nav_key(self, event, rows):
        # Leave the arrow keys alone while typing in an input field or dropdown
        if isinstance(event.widget, (ttk.Entry, tk.Entry)):
            return None
        self.scroll_rows(rows)
        return "break"

    def on_mouse_wheel(self, event):
        self.scroll_rows(-3 if event.delta > 0 else 3)
        return "break"

    def on_text_resize(self, event):
        rows = max(4, (event.height - 8) // self.line_height - 1)
        if rows != self.visible_rows:
            self.visible_rows = rows
            self.reset_tracking()
            if self.h_process:
                self.update_view()

    # ----- Misc -----

    def toggle_pause(self):
        self.paused = not self.paused
        self.btn_pause.config(text="Resume" if self.paused else "Pause")
        self.set_status("Paused" if self.paused else "Live", "orange" if self.paused else "green")

    def set_status(self, text, color="gray"):
        self.lbl_status.config(text=text, foreground=color)


if __name__ == "__main__":
    root = tk.Tk()
    LiveHexViewerApp(root)
    root.mainloop()