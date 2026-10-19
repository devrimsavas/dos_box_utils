import ctypes
import queue
import threading
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np
import dosbox_session
from PIL import Image, ImageTk

try:
    import psutil
except ImportError:
    psutil = None  # Auto PID detection disabled; manual PID entry still works

try:
    import base_finder as bf  # optional: enables the Find Base button
except Exception:
    bf = None

# Windows API constants
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# CGA graphics modes. Both use 80 bytes per line in real CGA memory:
#   mode 4: 320x200, 4 colors, 2 bits per pixel (BASIC: SCREEN 1)
#   mode 6: 640x200, 2 colors, 1 bit per pixel  (BASIC: SCREEN 2)
CGA_VRAM_OFFSET = "B800:0000"   # real CGA video memory = linear 0xB8000
BYTES_PER_LINE = 80             # default stride
CGA_BANK = 0x2000               # interlaced: odd lines start 8 KB after the even lines
LINES = 200
MAX_STRIDE = 1024
LAYOUT_INTERLACED = "Interlaced (CGA video memory)"
LAYOUT_LINEAR = "Linear (game back buffer)"
LAYOUTS = [LAYOUT_INTERLACED, LAYOUT_LINEAR]
MODE_CGA4 = "4 colors (320x200)"
MODE_CGA2 = "2 colors (640x200)"
MODES = [MODE_CGA4, MODE_CGA2]


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


def parse_address_input(addr_str):
    """
    Adds up hex parts separated by '+'. Each part can be plain hex ('0x2AC5E3B5040', 'B8000')
    or segment:offset ('B800:0000' = 0xB8000), so 'Base + B800:0000' works.
    """
    addr_str = addr_str.strip().replace(" ", "")
    total = 0
    for part in addr_str.split("+"):
        if not part:
            continue
        if ":" in part:
            seg, off = part.split(":", 1)
            total += int(seg, 16) * 16 + int(off, 16)
        else:
            total += int(part, 16)
    return total


def line_offsets(stride, interlaced):
    """Byte offset of each of the 200 lines from the start of the screen."""
    y = np.arange(LINES)
    if interlaced:
        return (y & 1) * CGA_BANK + (y >> 1) * stride
    return y * stride


def bytes_needed(stride, interlaced):
    """How many bytes one frame spans in memory (80 bytes of pixels per line)."""
    return int(line_offsets(stride, interlaced).max()) + BYTES_PER_LINE


def decode_cga(data, mode=MODE_CGA4, stride=BYTES_PER_LINE, interlaced=True):
    """
    Decodes 200 CGA lines of 80 bytes each, `stride` bytes apart.
      interlaced: real CGA video memory, even lines from 0x0000, odd lines from 0x2000.
      linear:     all lines one after another, as in many games' back buffers in normal RAM.
    Returns color indices 0-3: a 200x320 array (4 colors) or a 200x640 array (2 colors, as 0 and 3).
    """
    rows = data[line_offsets(stride, interlaced)[:, None] + np.arange(BYTES_PER_LINE)[None, :]]
    if mode == MODE_CGA2:
        return np.unpackbits(rows, axis=1) * 3  # 1 bit per pixel; shown with palette colors 0 and 3
    frame = np.zeros((LINES, 320), dtype=np.uint8)
    for i in range(4):
        frame[:, i::4] = (rows >> (6 - i * 2)) & 0x03
    return frame


def decode_cga_interlaced(data):
    """Real CGA video memory in mode 4, 80 bytes per line. Returns a 200x320 array of color indices (0-3)."""
    return decode_cga(data, MODE_CGA4, BYTES_PER_LINE, interlaced=True)


def decode_cga_linear(data):
    """Mode 4 lines one after another (16000 bytes), like a game's back buffer in normal RAM."""
    return decode_cga(data, MODE_CGA4, BYTES_PER_LINE, interlaced=False)


class CGALiveTunerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X CGA Live Tuner & Palette Editor")
        self.root.geometry("1100x760")

        self.h_process = None
        self.base = 0
        self.current_target = 0
        self.last_valid_target = 0
        self.delay_ms = 16
        self.running = False
        self.events = queue.Queue()
        self.worker = None

        self.stride = BYTES_PER_LINE
        self.buf_size = bytes_needed(MAX_STRIDE, interlaced=False)  # large enough for any stride
        self.c_buffer = (ctypes.c_char * self.buf_size)()
        self.bytes_read = ctypes.c_size_t()

        # Flicker filter: keeps the last N reads and shows the majority value per pixel
        self.filter_size = 3
        self.history = deque(maxlen=self.filter_size)

        # Default CGA Palette 1 (High Intensity): Black, Cyan, Magenta, White (RGB)
        self.palette_rgb = [
            [0, 0, 0],        # Color 0
            [0, 255, 255],    # Color 1
            [255, 0, 255],    # Color 2
            [255, 255, 255],  # Color 3
        ]

        self.setup_ui()
        self.setup_keybindings()
        self.refresh_pid_list()
        self.poll_events()

    def setup_ui(self):
        # Left Panel: Controls & Palette Sliders
        control_frame = ttk.Frame(self.root, padding=10)
        control_frame.pack(side=tk.LEFT, fill=tk.Y)

        # Connection Group
        conn_group = ttk.LabelFrame(control_frame, text="Process Connection", padding=5)
        conn_group.pack(fill=tk.X, pady=5)

        ttk.Label(conn_group, text="DOSBox-X PID:").grid(row=0, column=0, sticky=tk.W)
        pid_row = ttk.Frame(conn_group)
        pid_row.grid(row=0, column=1, padx=5, pady=2, sticky=tk.EW)
        self.combo_pid = ttk.Combobox(pid_row, width=18)
        self.combo_pid.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.combo_pid.bind("<<ComboboxSelected>>", lambda _e: self.load_saved_base(), add="+")
        ttk.Button(pid_row, text="Refresh", width=8, command=self.refresh_pid_list).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(conn_group, text="Base (hex):").grid(row=1, column=0, sticky=tk.W)
        base_row = ttk.Frame(conn_group)
        base_row.grid(row=1, column=1, padx=5, pady=2, sticky=tk.EW)
        self.base_entry = ttk.Entry(base_row, width=17)
        self.base_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.btn_find_base = ttk.Button(base_row, text="Find", width=6, command=self.find_base,
                                        state=tk.NORMAL if bf else tk.DISABLED)
        self.btn_find_base.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(conn_group, text="Offset:").grid(row=2, column=0, sticky=tk.W)
        offset_row = ttk.Frame(conn_group)
        offset_row.grid(row=2, column=1, padx=5, pady=2, sticky=tk.EW)
        self.addr_entry = ttk.Entry(offset_row, width=17)
        self.addr_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.addr_entry.insert(0, CGA_VRAM_OFFSET)
        ttk.Button(offset_row, text="VRAM", width=6, command=self.reset_to_vram).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(conn_group, text="Layout:").grid(row=3, column=0, sticky=tk.W)
        self.var_layout = tk.StringVar(value=LAYOUT_INTERLACED)
        layout_combo = ttk.Combobox(conn_group, textvariable=self.var_layout, values=LAYOUTS,
                                    state="readonly", width=26)
        layout_combo.grid(row=3, column=1, padx=5, pady=2, sticky=tk.EW)
        layout_combo.bind("<<ComboboxSelected>>", lambda _e: self.history.clear())

        ttk.Label(conn_group, text="Mode:").grid(row=4, column=0, sticky=tk.W)
        self.var_mode = tk.StringVar(value=MODE_CGA4)
        mode_combo = ttk.Combobox(conn_group, textvariable=self.var_mode, values=MODES,
                                  state="readonly", width=26)
        mode_combo.grid(row=4, column=1, padx=5, pady=2, sticky=tk.EW)
        mode_combo.bind("<<ComboboxSelected>>", lambda _e: self.history.clear())

        ttk.Label(conn_group, text="Stride:").grid(row=5, column=0, sticky=tk.W)
        stride_row = ttk.Frame(conn_group)
        stride_row.grid(row=5, column=1, padx=5, pady=2, sticky=tk.EW)
        self.var_stride = tk.IntVar(value=BYTES_PER_LINE)
        stride_box = ttk.Spinbox(stride_row, textvariable=self.var_stride, from_=1, to=MAX_STRIDE, width=6,
                                 command=self.on_stride_change)
        stride_box.pack(side=tk.LEFT)
        stride_box.bind("<Return>", lambda _e: self.on_stride_change())
        stride_box.bind("<FocusOut>", lambda _e: self.on_stride_change())
        ttk.Label(stride_row, text="bytes per line (80 = CGA, keys [ ])",
                  foreground="gray").pack(side=tk.LEFT, padx=(6, 0))

        self.btn_connect = ttk.Button(conn_group, text="Connect", command=self.toggle_connection)
        self.btn_connect.grid(row=6, column=0, columnspan=2, pady=5)

        # Navigation Group
        nav_group = ttk.LabelFrame(control_frame, text="Address Navigation (Keys: W/S, A/D, Z/X)", padding=5)
        nav_group.pack(fill=tk.X, pady=5)

        btn_row1 = ttk.Frame(nav_group)
        btn_row1.pack(fill=tk.X, pady=2)
        ttk.Button(btn_row1, text="-800 B (S)", width=10, command=lambda: self.adjust_addr(-800)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row1, text="+800 B (W)", width=10, command=lambda: self.adjust_addr(800)).pack(side=tk.LEFT, padx=2)

        btn_row2 = ttk.Frame(nav_group)
        btn_row2.pack(fill=tk.X, pady=2)
        ttk.Button(btn_row2, text="-80 B (A)", width=10, command=lambda: self.adjust_addr(-80)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row2, text="+80 B (D)", width=10, command=lambda: self.adjust_addr(80)).pack(side=tk.LEFT, padx=2)

        btn_row3 = ttk.Frame(nav_group)
        btn_row3.pack(fill=tk.X, pady=2)
        ttk.Button(btn_row3, text="-1 B (Z)", width=10, command=lambda: self.adjust_addr(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row3, text="+1 B (X)", width=10, command=lambda: self.adjust_addr(1)).pack(side=tk.LEFT, padx=2)

        # Flicker Filter Group
        filter_group = ttk.LabelFrame(control_frame, text="Flicker Filter (1 = off)", padding=5)
        filter_group.pack(fill=tk.X, pady=5)

        self.lbl_filter = ttk.Label(filter_group, text=f"Frames: {self.filter_size}", width=10)
        self.lbl_filter.pack(side=tk.LEFT)
        self.scale_filter = tk.Scale(
            filter_group,
            from_=1,
            to=9,
            resolution=2,  # 1, 3, 5, 7, 9 (odd sizes reduce ties)
            orient=tk.HORIZONTAL,
            showvalue=False,
            length=150,
            command=self.on_filter_change,
        )
        self.scale_filter.set(self.filter_size)
        self.scale_filter.pack(side=tk.LEFT, padx=5)

        # Palette Sliders Group
        palette_group = ttk.LabelFrame(control_frame, text="CGA Palette Colors (RGB)", padding=5)
        palette_group.pack(fill=tk.BOTH, expand=True, pady=5)

        self.color_previews = []
        color_labels = ["Color 0 (Background)", "Color 1 (Cyan)", "Color 2 (Magenta)", "Color 3 (White)"]

        for idx, name in enumerate(color_labels):
            # 2 x 2 grid, so all four colors fit on smaller screens
            box = ttk.LabelFrame(palette_group, text=name, padding=3)
            box.grid(row=idx // 2, column=idx % 2, padx=2, pady=2, sticky=tk.NSEW)

            preview = tk.Canvas(box, width=30, height=20, relief=tk.RIDGE, bd=1)
            preview.grid(row=0, column=0, rowspan=3, padx=5)
            self.color_previews.append(preview)

            for c_idx, c_name in enumerate(["R", "G", "B"]):
                ttk.Label(box, text=c_name).grid(row=c_idx, column=1, padx=2)
                scale = tk.Scale(
                    box,
                    from_=0,
                    to=255,
                    orient=tk.HORIZONTAL,
                    showvalue=False,
                    length=110,
                    command=lambda val, col=idx, ch=c_idx: self.on_palette_slider_change(col, ch, val),
                )
                scale.set(self.palette_rgb[idx][c_idx])
                scale.grid(row=c_idx, column=2, padx=2)

            self.update_preview_box(idx)

        # Right Panel: Display Screen
        display_frame = ttk.Frame(self.root, padding=10)
        display_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.lbl_status = ttk.Label(display_frame, text="Status: Disconnected | Addr: 0x0")
        self.lbl_status.pack(anchor=tk.W, pady=2)

        self.canvas_screen = tk.Canvas(display_frame, bg="black", width=640, height=400)
        self.canvas_screen.pack(fill=tk.BOTH, expand=True)

    def setup_keybindings(self):
        # Global key press binding
        self.root.bind("<KeyPress>", self.on_key_press)

    def on_key_press(self, event):
        # Do not navigate if typing inside an Entry widget
        if isinstance(event.widget, (ttk.Entry, tk.Entry)):
            return

        key = event.char.lower()
        if key == "w":
            self.adjust_addr(800)
        elif key == "s":
            self.adjust_addr(-800)
        elif key == "d":
            self.adjust_addr(80)
        elif key == "a":
            self.adjust_addr(-80)
        elif key == "x":
            self.adjust_addr(1)
        elif key == "z":
            self.adjust_addr(-1)
        elif key == "]":
            self.set_stride(self.stride + 1)
        elif key == "[":
            self.set_stride(self.stride - 1)

    def on_stride_change(self):
        try:
            self.set_stride(int(self.var_stride.get()))
        except (tk.TclError, ValueError):
            pass  # half-typed value; keep the current stride

    def set_stride(self, stride):
        stride = max(1, min(MAX_STRIDE, stride))
        if stride != self.stride:
            self.history.clear()  # frames with different strides do not mix
        self.stride = stride
        if self.var_stride.get() != stride:
            self.var_stride.set(stride)

    def on_palette_slider_change(self, color_idx, channel_idx, val):
        self.palette_rgb[color_idx][channel_idx] = int(val)
        self.update_preview_box(color_idx)

    def update_preview_box(self, color_idx):
        r, g, b = self.palette_rgb[color_idx]
        hex_color = f"#{r:02x}{g:02x}{b:02x}"
        self.color_previews[color_idx].configure(bg=hex_color)

    def on_filter_change(self, val):
        size = int(float(val))
        if size == self.filter_size:
            return
        self.filter_size = size
        # Rebuild the history with the new size, keeping the most recent frames
        self.history = deque(self.history, maxlen=size)
        self.lbl_filter.config(text=f"Frames: {size}")

    def refresh_pid_list(self):
        """Fills the PID dropdown with running DOSBox processes. Selects it automatically if only one is found."""
        if psutil is None:
            self.combo_pid["values"] = []
            self.lbl_status.config(text="Status: psutil not installed, enter PID manually")
            return

        processes = find_dosbox_processes()
        items = [f"{pid} - {name}" for pid, name in processes]
        self.combo_pid["values"] = items

        if len(items) == 1:
            self.combo_pid.set(items[0])
            self.lbl_status.config(text=f"Status: Found {processes[0][1]} (PID {processes[0][0]})")
        elif len(items) > 1:
            self.combo_pid.set(items[0])
            self.lbl_status.config(text=f"Status: {len(items)} DOSBox processes found, choose one")
        else:
            self.combo_pid.set("")
            self.lbl_status.config(text="Status: DOSBox is not running")
        self.load_saved_base()

    def load_saved_base(self):
        """Fills in the Base saved by the Base Finder for the selected DOSBox process."""
        try:
            pid = self.get_selected_pid()
        except ValueError:
            return
        base = dosbox_session.load_base(pid)
        if base is not None:
            self.base_entry.delete(0, tk.END)
            self.base_entry.insert(0, f"0x{base:X}")
            self.lbl_status.config(text=f"Status: Base 0x{base:X} loaded from the Base Finder")

    def save_found_base(self, base):
        """Shares a Base found by this tool with the other tools."""
        try:
            dosbox_session.save_base(self.get_selected_pid(), base)
        except ValueError:
            pass

    def get_selected_pid(self):
        """Accepts either a dropdown item ('12345 - dosbox-x.exe') or a plain typed number."""
        text = self.combo_pid.get().strip()
        return int(text.split(" - ")[0].strip())

    def adjust_addr(self, delta):
        if self.running:
            self.current_target += delta
            self.history.clear()  # do not mix frames from different addresses
            self.lbl_status.config(text=f"Status: Streaming | {self.describe_target()}")

    def describe_target(self):
        """Shows the address as an offset from the Base, plus segment:offset when it is in DOS memory."""
        offset = self.current_target - self.base
        if self.base and 0 <= offset < 0x100000:
            return f"Offset 0x{offset:X} ({offset >> 4:04X}:{offset & 0xF:04X})"
        return f"Addr {hex(self.current_target)}"

    def reset_to_vram(self):
        """Jumps back to the real CGA video memory (B800:0000) with the interlaced layout."""
        self.addr_entry.delete(0, tk.END)
        self.addr_entry.insert(0, CGA_VRAM_OFFSET)
        self.var_layout.set(LAYOUT_INTERLACED)
        self.set_stride(BYTES_PER_LINE)
        if self.running:
            self.current_target = self.base + parse_address_input(CGA_VRAM_OFFSET)
            self.history.clear()

    def find_base(self):
        if not bf or (self.worker and self.worker.is_alive()):
            return
        try:
            pid = self.get_selected_pid()
        except ValueError:
            self.lbl_status.config(text="Status: select a DOSBox process first")
            return
        self.btn_find_base.config(state=tk.DISABLED)
        self.lbl_status.config(text="Status: Finding Base...")

        def work():
            h = bf.kernel32.OpenProcess(bf.PROCESS_QUERY_INFORMATION | bf.PROCESS_VM_READ, False, pid)
            if not h:
                self.events.put((None, f"could not open PID {pid}"))
                return
            try:
                bases, _, _ = bf.scan_for_bases(h, lambda *a: None, threading.Event())
            finally:
                bf.kernel32.CloseHandle(h)
            if len(bases) == 1:
                self.events.put((bases[0], None))
            else:
                self.events.put((None, f"{len(bases)} candidates" if bases else "no DOS memory found"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def poll_events(self):
        try:
            while True:
                base, error = self.events.get_nowait()
                self.worker = None
                self.btn_find_base.config(state=tk.NORMAL)
                if base is None:
                    self.lbl_status.config(text=f"Status: Base not found ({error})")
                else:
                    self.base_entry.delete(0, tk.END)
                    self.base_entry.insert(0, f"0x{base:X}")
                    self.save_found_base(base)
                    self.lbl_status.config(text=f"Status: Base found: 0x{base:X}")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def toggle_connection(self):
        if not self.running:
            try:
                dosbox_pid = self.get_selected_pid()
                base_text = self.base_entry.get().strip()
                self.base = parse_address_input(base_text) if base_text else 0
                self.current_target = self.base + parse_address_input(self.addr_entry.get().strip())
                self.last_valid_target = self.current_target
            except Exception as e:
                messagebox.showerror("Input Error", f"Invalid PID, Base or Offset: {e}")
                return

            self.h_process = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, dosbox_pid
            )

            if not self.h_process:
                err = ctypes.get_last_error()
                messagebox.showerror("Error", f"Failed to open process! Error code: {err}")
                return

            self.history.clear()
            self.running = True
            self.btn_connect.config(text="Disconnect")
            self.root.after(self.delay_ms, self.update_frame)
        else:
            self.running = False
            if self.h_process:
                kernel32.CloseHandle(self.h_process)
                self.h_process = None
            self.btn_connect.config(text="Connect")
            self.lbl_status.config(text="Status: Disconnected | Addr: 0x0")

    def frame_size(self):
        """Bytes to read for one frame with the current layout and stride."""
        return bytes_needed(self.stride, self.var_layout.get() != LAYOUT_LINEAR)

    def decode_frame(self, raw_bytes):
        """Converts raw CGA bytes into color indices (0-3), using the chosen mode, layout and stride."""
        data = np.frombuffer(raw_bytes, dtype=np.uint8)
        interlaced = self.var_layout.get() != LAYOUT_LINEAR
        if len(data) < bytes_needed(self.stride, interlaced):
            return np.zeros((LINES, 640 if self.var_mode.get() == MODE_CGA2 else 320), dtype=np.uint8)
        return decode_cga(data, self.var_mode.get(), self.stride, interlaced)

    def filter_flicker(self, frame):
        """
        Picks the most frequent color index per pixel over the last N reads.
        A sprite that is missing in a few reads stays visible because it wins the majority.
        On a tie, the most recent read wins.
        """
        if self.history and self.history[-1].shape != frame.shape:
            self.history.clear()  # the mode changed between reads
        self.history.append(frame)
        if len(self.history) == 1:
            return frame

        stack = np.stack(self.history)  # (N, 200, 320)

        # Count how many frames each color index (0-3) appears in, per pixel
        counts = np.stack([(stack == k).sum(axis=0) for k in range(4)]).astype(np.float32)

        # Tie breaker: give the latest frame's value an extra half vote
        latest = self.history[-1]
        for k in range(4):
            counts[k] += 0.5 * (latest == k)

        return counts.argmax(axis=0).astype(np.uint8)

    def update_frame(self):
        if not self.running:
            return

        size = self.frame_size()
        success = kernel32.ReadProcessMemory(
            self.h_process,
            ctypes.c_void_p(self.current_target),
            self.c_buffer,
            size,
            ctypes.byref(self.bytes_read),
        )

        if success:
            self.last_valid_target = self.current_target
            index_frame = self.decode_frame(self.c_buffer.raw[:size])
            stable_frame = self.filter_flicker(index_frame)
            palette_arr = np.array(self.palette_rgb, dtype=np.uint8)
            rgb_frame = palette_arr[stable_frame]

            # Resize to fit canvas
            c_w = max(320, self.canvas_screen.winfo_width())
            c_h = max(200, self.canvas_screen.winfo_height())
            scaled_frame = cv2.resize(rgb_frame, (c_w, c_h), interpolation=cv2.INTER_NEAREST)

            img = Image.fromarray(scaled_frame)
            self.tk_img = ImageTk.PhotoImage(image=img)
            self.canvas_screen.delete("all")
            self.canvas_screen.create_image(0, 0, anchor=tk.NW, image=self.tk_img)

            self.lbl_status.config(text=f"Status: Streaming | {self.describe_target()} | stride {self.stride}")
        else:
            self.current_target = self.last_valid_target
            self.lbl_status.config(text=f"Status: Memory Read Error! Reverted to {self.describe_target()}")

        self.root.after(self.delay_ms, self.update_frame)


if __name__ == "__main__":
    root = tk.Tk()
    app = CGALiveTunerApp(root)
    root.mainloop()